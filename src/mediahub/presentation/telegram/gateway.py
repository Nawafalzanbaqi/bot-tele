"""The long-poll loop.

Deliberately dull. It fetches updates, parses them, hands each to a handler and
never lets one bad update stop the loop - a gateway that dies on a malformed
message is a gateway that is down until someone notices.

Long-polling is the default transport because a self-hosted appliance should
not require opening a port in someone's home router
(``docs/architecture/12-telegram-architecture.md`` §12.7).

Two safeguards that matter more than they look:

* **Offset discipline.** The offset advances past every update the loop has
  *accepted*, whether or not handling it succeeded. Advancing only on success
  turns one poison update into an infinite retry loop.
* **De-duplication.** Telegram redelivers after a network blip, and a restart
  can replay. A bounded ring of recently-seen ids means one message never
  becomes two downloads.

And one about time: **nothing an update triggers runs on the poll loop.** Each
update is served by its own task. Handling a link means probing it, which is a
network round-trip to the source of up to thirty seconds - and while the loop
awaited that, no other update was read. A second person's link waited, and so
did ``/cancel`` for the download the first person wanted stopped. Admission is
bounded so a burst of a hundred updates becomes a queue, not a hundred probes.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import TYPE_CHECKING, Final, Protocol

from loguru import logger

from mediahub.presentation.telegram.updates import parse_update

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from mediahub.presentation.telegram.api import TelegramMessenger
    from mediahub.presentation.telegram.updates import Intent

DEFAULT_POLL_TIMEOUT_SECONDS: Final[int] = 30
SEEN_CAPACITY: Final[int] = 512
BACKOFF_SECONDS: Final[float] = 5.0

_MESSAGE_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {"message_id", "from", "chat", "date", "sender_chat", "message_thread_id", "edit_date"}
)
"""Fields every message carries. Excluded from the "what was in it" log line,
which is meant to show the *content* fields - photo, sticker, voice - only."""

HANDLER_CONCURRENCY: Final[int] = 8
"""How many updates may be *served* at once.

Serving is cheap - authorise, probe, reply, and hand any download to the
handlers' own admission control - so this bounds probes in flight, not
downloads. Eight is more than one household sends and small enough that a
pasted list of fifty links does not open fifty connections from a board that
is also someone's DNS server.
"""

SETTLE_TIMEOUT_SECONDS: Final[float] = 10.0
"""How long a stopping gateway waits for updates still being served.

A probe has a thirty-second budget but almost always answers in a few seconds;
waiting the full budget would eat the drain window that running downloads need
more. Whatever is still being served after this is cancelled - the person gets
no reply to that one message, and sends it again when the bot is back.
"""

DRAIN_TIMEOUT_SECONDS: Final[float] = 55.0
"""How long a stopping gateway waits for running acquisitions.

Just under the container's ``stop_grace_period`` (60 s in ``docker-compose.pi.yml``),
which is the whole budget: anything still running when Docker's grace expires is
killed mid-upload. The previous default of 30 s left half the budget unused and
abandoned any download that needed it, contradicting the promise in
``presentation/telegram/__main__.py`` that a deploy lets a download finish.
"""


class UpdateHandlers(Protocol):
    """What the gateway needs from whoever serves updates.

    Two methods, and only these two: the gateway hands an intent over and, at
    shutdown, asks for running work to finish. Naming the contract keeps the
    loop testable with a recording stand-in and keeps it from reaching into
    the handlers for anything else.
    """

    async def handle(self, intent: Intent) -> None:
        """Serve one parsed update."""
        ...

    async def drain(self, *, timeout: float = ...) -> None:  # noqa: ASYNC109 - a budget, not an asyncio deadline
        """Wait for running acquisitions to finish, within ``timeout`` seconds."""
        ...


class TelegramGateway:
    """Polls Telegram and feeds updates to the handlers."""

    __slots__ = (
        "_admission",
        "_handlers",
        "_heartbeat",
        "_heartbeat_failed",
        "_messenger",
        "_offset",
        "_poll_timeout",
        "_seen",
        "_serving",
        "_stopping",
    )

    def __init__(
        self,
        messenger: TelegramMessenger,
        handlers: UpdateHandlers,
        *,
        poll_timeout_seconds: int = DEFAULT_POLL_TIMEOUT_SECONDS,
        heartbeat_path: Path | None = None,
    ) -> None:
        """Bind the gateway to its transport and handlers.

        Args:
            messenger: The transport to poll and reply through.
            handlers: Where parsed intents go.
            poll_timeout_seconds: Long-poll duration.
            heartbeat_path: A file whose modification time is refreshed after
                every successful poll. It is the only liveness signal this
                process has: it has no HTTP surface, and a healthcheck that
                opens the database from a fresh process passes while the loop
                is dead. ``None`` disables it.
        """
        self._messenger = messenger
        self._handlers = handlers
        self._poll_timeout = poll_timeout_seconds
        self._heartbeat = heartbeat_path
        self._heartbeat_failed = False
        self._offset: int | None = None
        self._seen: deque[int] = deque(maxlen=SEEN_CAPACITY)
        self._stopping = asyncio.Event()
        self._serving: set[asyncio.Task[None]] = set()
        self._admission = asyncio.Semaphore(HANDLER_CONCURRENCY)

    @property
    def pending_updates(self) -> int:
        """Return how many accepted updates are still being served."""
        return len(self._serving)

    async def run(self) -> None:
        """Poll until :meth:`stop` is called, then drain in-flight work.

        Shutdown is two waits inside one budget: first for updates still being
        served (short - a reply or a probe), then for the downloads they
        started, which get whatever is left of the container's grace period.
        """
        logger.info("Telegram gateway started")
        try:
            while not self._stopping.is_set():
                await self._cycle()
        finally:
            spent = await self.settle(budget_seconds=SETTLE_TIMEOUT_SECONDS)
            await self._handlers.drain(timeout=max(1.0, DRAIN_TIMEOUT_SECONDS - spent))
            logger.info("Telegram gateway stopped")

    def stop(self) -> None:
        """Ask the loop to finish after the current poll."""
        self._stopping.set()

    async def settle(self, *, budget_seconds: float | None = None) -> float:
        """Wait for every accepted update to finish being served.

        Returns the seconds spent waiting - exactly zero when there was nothing
        to wait for, so a caller sharing a budget across two waits can subtract
        it. Anything still running when ``budget_seconds`` expires is cancelled.
        """
        if not self._serving:
            return 0.0
        started = time.monotonic()
        pending = set(self._serving)
        _done, late = await asyncio.wait(pending, timeout=budget_seconds)
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)
            logger.bind(count=len(late)).warning("Cancelled updates still being served at shutdown")
        return time.monotonic() - started

    async def poll_once(self) -> int:
        """Fetch one batch and start serving it. Returns how many were accepted.

        Serving happens on separate tasks, so this returns as soon as the batch
        has been read and dispatched; :meth:`settle` waits for the results.
        Exposed so the loop's behaviour can be tested without running it.
        """
        updates = await self._messenger.get_updates(offset=self._offset, timeout=self._poll_timeout)
        self._beat()
        handled = 0
        for raw in updates:
            intent = parse_update(raw)
            if intent is None:
                self._advance(raw)
                self._note_ignored(raw)
                continue

            self._offset = intent.update_id + 1
            if intent.update_id in self._seen:
                logger.bind(update_id=intent.update_id).debug("Ignored duplicate update")
                continue
            self._seen.append(intent.update_id)

            self._serve(intent)
            handled += 1
        return handled

    def _serve(self, intent: Intent) -> None:
        """Hand one update to the handlers on its own task."""
        task = asyncio.create_task(self._serve_one(intent))
        self._serving.add(task)
        task.add_done_callback(self._serving.discard)

    async def _serve_one(self, intent: Intent) -> None:
        """Serve one update inside the admission bound, and never raise.

        The handlers already turn every failure into a reply. This second net
        exists because a task nobody awaits swallows its exception silently,
        and a serving bug would otherwise vanish without a log line.
        """
        async with self._admission:
            try:
                await self._handlers.handle(intent)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).bind(update_id=intent.update_id).error(
                    "Serving an update failed outside the handlers"
                )

    async def _cycle(self) -> None:
        """Run one poll, absorbing transport failures."""
        try:
            await self.poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Telegram being unreachable is weather, not an incident. Back off
            # and keep the loop alive; the API and any running downloads are
            # unaffected.
            logger.opt(exception=True).warning("Polling failed; backing off")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=BACKOFF_SECONDS)

    def _beat(self) -> None:
        """Refresh the heartbeat file, if one is configured.

        A failure here must never take the loop down - a read-only or missing
        directory is a deployment mistake worth one warning, not a dead bot.
        """
        if self._heartbeat is None:
            return
        try:
            self._heartbeat.touch()
        except OSError:
            if not self._heartbeat_failed:
                self._heartbeat_failed = True
                logger.opt(exception=True).warning("Could not refresh the heartbeat file")
            return
        self._heartbeat_failed = False

    def _advance(self, raw: object) -> None:
        """Move past an update the gateway does not understand."""
        if isinstance(raw, dict):
            update_id = raw.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                self._offset = update_id + 1

    @staticmethod
    def _note_ignored(raw: object) -> None:
        """Say that an update was dropped, and what shape it had.

        Shape only - the update's *kind* (edited message, channel post, member
        change) and, for a message, the names of its content fields (sticker,
        photo, voice). Never the content: a caption or a text may carry a URL
        with a token in it. Without this line a message the bot could not read
        left no trace at all, and "I sent it and nothing happened" had nothing
        to be checked against.
        """
        if not isinstance(raw, dict):
            logger.bind(shape=type(raw).__name__).info("Ignored an update that was not an object")
            return
        kinds = sorted(key for key in raw if key != "update_id")
        message = raw.get("message")
        content: list[str] = []
        if isinstance(message, dict):
            content = sorted(
                key for key in message if key not in _MESSAGE_ENVELOPE_KEYS and message.get(key)
            )
        logger.bind(update_id=raw.get("update_id"), kinds=kinds, content=content).info(
            "Ignored an update the gateway does not handle"
        )
