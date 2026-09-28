"""The poll loop's two operational contracts: it proves it is alive, and it stops politely.

Both exist because of how the process is run, not because of what it does. A
container healthcheck can only watch a file this loop touches, and Docker gives
a stopping container a fixed grace period - a drain that ignores either number
looks fine in tests and fails only in production.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.presentation.telegram.gateway import DRAIN_TIMEOUT_SECONDS, TelegramGateway
from tests.support.telegram_fakes import FakeMessenger, message_update

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


class RecordingHandlers:
    """Stands in for the handlers: counts intents, records how it was drained."""

    def __init__(self) -> None:
        self.handled = 0
        self.drain_timeouts: list[float] = []

    async def handle(self, intent: Any) -> None:
        del intent
        self.handled += 1

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

        assert handled == 1
        assert handlers.handled == 1
        assert not beat.exists()

    async def test_no_path_means_no_heartbeat(self, tmp_path: Path) -> None:
        gateway = TelegramGateway(FakeMessenger(), RecordingHandlers())

        await gateway.poll_once()

        assert list(tmp_path.iterdir()) == []


class TestShutdown:
    async def test_run_drains_with_the_full_grace_budget(self) -> None:
        handlers = RecordingHandlers()
        gateway = TelegramGateway(FakeMessenger(), handlers)
        gateway.stop()

        await gateway.run()

        assert handlers.drain_timeouts == [DRAIN_TIMEOUT_SECONDS]

    def test_the_drain_budget_fits_inside_the_containers_grace_period(self) -> None:
        """The number in code and the number in compose must agree, in this order."""
        compose = (REPO_ROOT / "docker-compose.pi.yml").read_text(encoding="utf-8")
        match = re.search(r"stop_grace_period:\s*(\d+)s", compose)

        assert match is not None, "the telegram service must declare stop_grace_period"
        assert DRAIN_TIMEOUT_SECONDS < int(match.group(1))
        # Anything shorter abandons real downloads for no reason.
        assert DRAIN_TIMEOUT_SECONDS >= 45
