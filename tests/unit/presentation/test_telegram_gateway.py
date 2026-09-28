"""The poll loop's operational contracts: it proves it is alive, it stops politely,
and nothing an update triggers ever holds it up.

The first two exist because of how the process is run, not because of what it
does. A container healthcheck can only watch a file this loop touches, and Docker
gives a stopping container a fixed grace period - a drain that ignores either
number looks fine in tests and fails only in production. The third is about
people: a probe of one person's link must not delay reading the next person's
message, or the ``/cancel`` that was meant for it.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.presentation.telegram import gateway as gateway_module
from mediahub.presentation.telegram.gateway import (
    DRAIN_TIMEOUT_SECONDS,
    HANDLER_CONCURRENCY,
    SETTLE_TIMEOUT_SECONDS,
    TelegramGateway,
)
from tests.support.telegram_fakes import FakeMessenger, message_update

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


class RecordingHandlers:
    """Stands in for the handlers: counts intents, records how it was drained.

    ``hold`` makes every handling wait until released, which is how a slow probe
    is simulated without a network. ``fail`` makes handling raise, standing in
    for a bug the real handlers did not catch.
    """

    def __init__(self, *, hold: bool = False, fail: bool = False) -> None:
        self.handled = 0
        self.started = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.drain_timeouts: list[float] = []
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.fail = fail

    async def handle(self, intent: Any) -> None:
        del intent
        self.started += 1
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await self.release.wait()
            if self.fail:
                message = "a bug in a handler"
                raise RuntimeError(message)
            self.handled += 1
        finally:
            self.in_flight -= 1

    async def drain(self, *, timeout: float = 30.0) -> None:
        self.drain_timeouts.append(timeout)


class BrokenMessenger(FakeMessenger):
    """A transport that cannot reach Telegram."""

    async def get_updates(
        self, *, offset: int | None = None, timeout: int = 30
    ) -> Sequence[Mapping[str, Any]]:
        del offset, timeout
        message = "telegram is unreachable"
        raise ConnectionError(message)


class TestHeartbeat:
    async def test_a_successful_poll_creates_the_file(self, tmp_path: Path) -> None:
        beat = tmp_path / "heartbeat"
        gateway = TelegramGateway(FakeMessenger(), RecordingHandlers(), heartbeat_path=beat)

        await gateway.poll_once()

        assert beat.exists()

    async def test_every_poll_refreshes_the_modification_time(self, tmp_path: Path) -> None:
        beat = tmp_path / "heartbeat"
        beat.touch()
        stale = 1_000_000_000
        os.utime(beat, (stale, stale))
        gateway = TelegramGateway(FakeMessenger(), RecordingHandlers(), heartbeat_path=beat)

        await gateway.poll_once()

        assert beat.stat().st_mtime > stale + 1

    async def test_a_failed_poll_leaves_the_file_alone(self, tmp_path: Path) -> None:
        """A dead transport must look dead to the healthcheck."""
        beat = tmp_path / "heartbeat"
        gateway = TelegramGateway(BrokenMessenger(), RecordingHandlers(), heartbeat_path=beat)

        with pytest.raises(ConnectionError):
            await gateway.poll_once()

        assert not beat.exists()

    async def test_an_unwritable_heartbeat_never_stops_the_loop(self, tmp_path: Path) -> None:
        """A deployment mistake costs a warning, not the bot."""
        beat = tmp_path / "missing-directory" / "heartbeat"
        handlers = RecordingHandlers()
        messenger = FakeMessenger(batches=[[message_update("hello")]])
        gateway = TelegramGateway(messenger, handlers, heartbeat_path=beat)

        handled = await gateway.poll_once()
        await gateway.settle()

        assert handled == 1
        assert handlers.handled == 1
        assert not beat.exists()

    async def test_no_path_means_no_heartbeat(self, tmp_path: Path) -> None:
        gateway = TelegramGateway(FakeMessenger(), RecordingHandlers())

        await gateway.poll_once()

        assert list(tmp_path.iterdir()) == []


class TestServingIsOffTheLoop:
    """A slow handler must cost the person who sent it, and nobody else."""

    async def test_poll_once_returns_while_a_handler_is_still_running(self) -> None:
        handlers = RecordingHandlers(hold=True)
        messenger = FakeMessenger(batches=[[message_update("slow link", update_id=1)]])
        gateway = TelegramGateway(messenger, handlers)

        handled = await asyncio.wait_for(gateway.poll_once(), timeout=1)
        await asyncio.sleep(0)  # let the serving task start; the loop did not wait for it

        assert handled == 1
        assert handlers.started == 1
        assert handlers.handled == 0, "the handler is still waiting, the loop is not"
        assert gateway.pending_updates == 1

        handlers.release.set()
        await gateway.settle()
        assert handlers.handled == 1
        assert gateway.pending_updates == 0

    async def test_the_next_poll_is_not_delayed_by_the_previous_batch(self) -> None:
        handlers = RecordingHandlers(hold=True)
        messenger = FakeMessenger(
            batches=[
                [message_update("first", update_id=1)],
                [message_update("/cancel", update_id=2)],
            ]
        )
        gateway = TelegramGateway(messenger, handlers)

        await gateway.poll_once()
        await asyncio.wait_for(gateway.poll_once(), timeout=1)
        await asyncio.sleep(0)

        assert messenger.poll_calls == [None, 2]
        assert handlers.started == 2, "the second update was read while the first still ran"
        handlers.release.set()
        await gateway.settle()

    async def test_admission_is_bounded(self) -> None:
        handlers = RecordingHandlers(hold=True)
        burst = [
            message_update(f"link {n}", update_id=n) for n in range(1, 3 * HANDLER_CONCURRENCY)
        ]
        gateway = TelegramGateway(FakeMessenger(batches=[burst]), handlers)

        accepted = await gateway.poll_once()
        await asyncio.sleep(0)

        assert accepted == len(burst)
        assert handlers.in_flight == HANDLER_CONCURRENCY
        handlers.release.set()
        await gateway.settle()
        assert handlers.handled == len(burst)
        assert handlers.peak_in_flight == HANDLER_CONCURRENCY

    async def test_a_handler_that_raises_takes_nothing_else_down(self) -> None:
        handlers = RecordingHandlers(fail=True)
        messenger = FakeMessenger(
            batches=[[message_update("a", update_id=1), message_update("b", update_id=2)]]
        )
        gateway = TelegramGateway(messenger, handlers)

        await gateway.poll_once()
        await gateway.settle()

        assert handlers.started == 2
        assert gateway.pending_updates == 0
        # And the loop itself is intact: the next poll happens with the right offset.
        await gateway.poll_once()
        assert messenger.poll_calls[-1] == 3

    async def test_settle_reports_zero_when_there_was_nothing_to_wait_for(self) -> None:
        gateway = TelegramGateway(FakeMessenger(), RecordingHandlers())

        assert await gateway.settle() == 0.0


class TestShutdown:
    async def test_run_drains_with_the_full_grace_budget(self) -> None:
        handlers = RecordingHandlers()
        gateway = TelegramGateway(FakeMessenger(), handlers)
        gateway.stop()

        await gateway.run()

        assert handlers.drain_timeouts == [DRAIN_TIMEOUT_SECONDS]

    async def test_a_stuck_handler_is_cancelled_and_the_downloads_still_get_their_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shutdown waits briefly for replies, then gives the rest to running downloads."""
        # Short-circuit the settle budget so the test does not wait ten seconds.
        monkeypatch.setattr(gateway_module, "SETTLE_TIMEOUT_SECONDS", 0.05)
        handlers = RecordingHandlers(hold=True)
        messenger = FakeMessenger(batches=[[message_update("stuck", update_id=1)]])
        gateway = TelegramGateway(messenger, handlers)
        await gateway.poll_once()
        await asyncio.sleep(0)
        assert handlers.started == 1
        gateway.stop()

        await asyncio.wait_for(gateway.run(), timeout=5)

        assert gateway.pending_updates == 0, "the stuck handler was cancelled"
        assert handlers.handled == 0
        assert len(handlers.drain_timeouts) == 1
        assert handlers.drain_timeouts[0] > DRAIN_TIMEOUT_SECONDS - SETTLE_TIMEOUT_SECONDS

    def test_the_settle_budget_leaves_most_of_the_grace_period_to_downloads(self) -> None:
        assert SETTLE_TIMEOUT_SECONDS <= DRAIN_TIMEOUT_SECONDS / 4

    def test_the_drain_budget_fits_inside_the_containers_grace_period(self) -> None:
        """The number in code and the number in compose must agree, in this order."""
        compose = (REPO_ROOT / "docker-compose.pi.yml").read_text(encoding="utf-8")
        match = re.search(r"stop_grace_period:\s*(\d+)s", compose)

        assert match is not None, "the telegram service must declare stop_grace_period"
        grace_seconds = int(match.group(1))
        assert grace_seconds > DRAIN_TIMEOUT_SECONDS
        # Anything shorter abandons real downloads for no reason.
        assert DRAIN_TIMEOUT_SECONDS >= 45
