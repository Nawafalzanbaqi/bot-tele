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
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.presentation.telegram.updates import parse_update

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.presentation.telegram.api import TelegramMessenger
    from mediahub.presentation.telegram.handlers import TelegramHandlers

DEFAULT_POLL_TIMEOUT_SECONDS: Final[int] = 30
SEEN_CAPACITY: Final[int] = 512
BACKOFF_SECONDS: Final[float] = 5.0


class TelegramGateway:
    """Polls Telegram and feeds updates to the handlers."""

    __slots__ = ("_handlers", "_messenger", "_offset", "_poll_timeout", "_seen", "_stopping")

    def __init__(
        self,
        messenger: TelegramMessenger,
        handlers: TelegramHandlers,
        *,
        poll_timeout_seconds: int = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> None:
        """Bind the gateway to its transport and handlers."""
        self._messenger = messenger
        self._handlers = handlers
        self._poll_timeout = poll_timeout_seconds
        self._offset: int | None = None
        self._seen: deque[int] = deque(maxlen=SEEN_CAPACITY)
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        """Poll until :meth:`stop` is called, then drain in-flight work."""
        logger.info("Telegram gateway started")
        try:
            while not self._stopping.is_set():
                await self._cycle()
        finally:
            await self._handlers.drain()
            logger.info("Telegram gateway stopped")

    def stop(self) -> None:
        """Ask the loop to finish after the current poll."""
        self._stopping.set()

    async def poll_once(self) -> int:
        """Fetch and dispatch one batch. Returns how many were handled.

        Exposed so the loop's behaviour can be tested without running it.
        """
        updates = await self._messenger.get_updates(offset=self._offset, timeout=self._poll_timeout)
        handled = 0
        for raw in updates:
            intent = parse_update(raw)
            if intent is None:
                self._advance(raw)
                continue

            self._offset = intent.update_id + 1
            if intent.update_id in self._seen:
                logger.bind(update_id=intent.update_id).debug("Ignored duplicate update")
                continue
            self._seen.append(intent.update_id)

            await self._handlers.handle(intent)
            handled += 1
        return handled

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

    def _advance(self, raw: object) -> None:
        """Move past an update the gateway does not understand."""
        if isinstance(raw, dict):
            update_id = raw.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                self._offset = update_id + 1
