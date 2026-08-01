"""Shows progress in a chat without tripping Telegram's flood limits.

Two problems have to be solved at once.

**Rate.** The download engine reports progress from a worker thread, many times
a second. Telegram tolerates roughly one edit per second per chat and answers
sustained abuse with a flood wait. So the callback does not send anything: it
records the latest state, and a separate task edits the message on a timer.

**Threads.** The engine's callback is synchronous and runs off the event loop,
so it cannot await an HTTP call. Recording into a slot and letting an async
task drain it keeps the two worlds apart with no cross-thread scheduling.

The presenter also refuses to send an edit when the rendered text has not
changed - Telegram rejects identical edits, and a rejected edit is a wasted
request and a log line about nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.delivery.ports import DeliveryProgress
from mediahub.presentation.telegram.formatters import render_delivery_progress, render_progress

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from typing import Any

    from mediahub.application.download.ports import DownloadProgress
    from mediahub.presentation.telegram.api import TelegramMessenger

DEFAULT_INTERVAL_SECONDS: Final[float] = 3.0


class ProgressPresenter:
    """Edits one message to reflect the latest progress, at a bounded rate."""

    __slots__ = (
        "_chat_id",
        "_interval",
        "_last_text",
        "_latest",
        "_lock",
        "_markup",
        "_message_id",
        "_messenger",
        "_stopped",
        "_title",
    )

    def __init__(
        self,
        messenger: TelegramMessenger,
        *,
        chat_id: str,
        message_id: int,
        title: str,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> None:
        """Bind the presenter to the message it will keep up to date."""
        self._messenger = messenger
        self._chat_id = chat_id
        self._message_id = message_id
        self._title = title
        self._interval = max(0.0, interval_seconds)
        self._markup = reply_markup
        self._latest: DownloadProgress | DeliveryProgress | None = None
        self._last_text: str | None = None
        self._lock = threading.Lock()
        self._stopped = asyncio.Event()

    def report(self, progress: DownloadProgress) -> None:
        """Record the latest download progress. Safe to call from any thread.

        This is the engine's ``ProgressCallback``. It must never block and must
        never raise: an exception here would abort the download it is merely
        describing.
        """
        with self._lock:
            self._latest = progress

    def report_delivery(self, progress: DeliveryProgress) -> None:
        """Record the latest upload progress. Safe to call from any thread.

        A separate channel because the two halves are genuinely different, and
        because on a domestic connection the upload is usually the slower one -
        showing "downloading, 100%" for four minutes while a file uploads is
        the sort of thing that makes a product feel broken while working.
        """
        with self._lock:
            self._latest = progress

    async def run(self) -> None:
        """Edit the message until :meth:`stop` is called."""
        while not self._stopped.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
            await self._flush()

    async def stop(self) -> None:
        """Stop the loop after one final edit."""
        self._stopped.set()
        await self._flush()

    async def _flush(self) -> None:
        """Send an edit if there is anything new to say."""
        with self._lock:
            progress = self._latest
        if progress is None:
            return

        text = (
            render_delivery_progress(progress, title=self._title)
            if isinstance(progress, DeliveryProgress)
            else render_progress(progress, title=self._title)
        )
        if text == self._last_text:
            return

        try:
            await self._messenger.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
                reply_markup=self._markup,
            )
        except Exception:
            # A failed progress edit must never fail the download. Telegram
            # rejects identical edits and rate-limits bursts; both are noise.
            logger.opt(exception=True).debug("Progress edit failed; continuing")
        else:
            self._last_text = text
