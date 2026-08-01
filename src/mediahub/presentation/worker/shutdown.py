"""Turning a signal into an orderly stop.

The contract with the orchestrator is short (``docs/architecture/10-worker-architecture.md``
§10.7):

1. ``SIGTERM`` arrives; stop claiming immediately;
2. let the current stage finish, checkpoint it and release the lease;
3. exit.

Two details are worth stating because both are easy to get wrong.

**The drain grace must be shorter than the orchestrator's kill timeout.** If it
is not, the process is killed while it is still politely winding down and the
graceful path never actually runs - a silent misconfiguration that looks like
nothing at all until a deploy loses in-flight work.

**A second signal means "now".** An operator who sends ``SIGTERM`` twice is
telling you the polite path is not working, and refusing to listen is how
someone ends up reaching for ``SIGKILL`` - which is safe here, but leaves jobs
stalled for a lease period rather than continuing immediately on the next
worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

SIGNAL_NAMES = ("SIGTERM", "SIGINT")


class ShutdownController:
    """Watches for stop requests and reports what kind they were."""

    __slots__ = ("_immediate", "_on_stop", "_requested")

    def __init__(self, on_stop: Callable[[], None] | None = None) -> None:
        """Create a controller that calls ``on_stop`` the first time it fires."""
        self._requested = asyncio.Event()
        self._immediate = False
        self._on_stop = on_stop

    @property
    def requested(self) -> bool:
        """Return whether a stop has been asked for."""
        return self._requested.is_set()

    @property
    def immediate(self) -> bool:
        """Return whether the caller insisted, by asking twice."""
        return self._immediate

    def request(self) -> None:
        """Ask the process to stop; asking again escalates.

        Idempotent in effect: the drain runs once, and a repeat request only
        shortens how long the process is prepared to wait for it.
        """
        if self._requested.is_set():
            self._immediate = True
            logger.warning("Second stop request; giving up on the graceful drain")
            return
        self._requested.set()
        logger.info("Stop requested; draining")
        if self._on_stop is not None:
            self._on_stop()

    async def wait(self) -> None:
        """Block until a stop is requested."""
        await self._requested.wait()

    def install(self) -> None:  # pragma: no cover - process wiring
        """Route ``SIGTERM`` and ``SIGINT`` to :meth:`request`.

        Signals that the platform does not have, and loops that cannot register
        handlers (Windows, and any thread that is not the main one), are skipped
        rather than treated as a failure: the controller is still usable
        programmatically, which is how the tests drive it.
        """
        loop = asyncio.get_running_loop()
        for name in SIGNAL_NAMES:
            number = getattr(signal, name, None)
            if number is None:
                continue
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(number, self.request)
