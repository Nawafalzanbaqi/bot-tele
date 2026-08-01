"""Cooperative cancellation.

There is no safe way to kill work mid-write and guarantee no corruption, so
cancellation is cooperative everywhere in MediaHub: a caller *requests* it, and
the operation acknowledges it at its next checkpoint
(``docs/architecture/09-queue-architecture.md`` §9.7).

:class:`CancellationToken` is the read side, held by the operation.
:class:`CancellationSource` is the write side, held by the caller. Separating
them means an adapter cannot cancel its own work, which keeps the control flow
one-directional and obvious.

The implementation is thread-safe because long-running engine work runs in a
worker thread while the caller lives on the event loop.
"""

from __future__ import annotations

import threading
from enum import StrEnum
from typing import Protocol, runtime_checkable


class CancellationReason(StrEnum):
    """Why an operation was asked to stop.

    Attributes:
        REQUESTED: A user or an operator asked for it.
        TIMEOUT: A deadline elapsed.
        SHUTDOWN: The process is draining.
    """

    REQUESTED = "requested"
    TIMEOUT = "timeout"
    SHUTDOWN = "shutdown"


@runtime_checkable
class CancellationToken(Protocol):
    """The read side of a cancellation request."""

    @property
    def cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        ...

    @property
    def reason(self) -> CancellationReason | None:
        """Return why cancellation was requested, if it was."""
        ...

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancellation is requested or ``timeout`` elapses.

        Returns:
            ``True`` if cancellation was requested.
        """
        ...


class CancellationSource:
    """The write side of a cancellation request.

    Create one per operation, pass :attr:`token` to the operation, and call
    :meth:`cancel` to ask it to stop. Cancelling twice is harmless; the first
    reason wins, so a user request is not overwritten by a later timeout.
    """

    __slots__ = ("_event", "_lock", "_reason")

    def __init__(self) -> None:
        """Create an uncancelled source."""
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: CancellationReason | None = None

    @property
    def token(self) -> CancellationToken:
        """Return the read-only view to hand to the operation."""
        return self

    @property
    def cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._event.is_set()

    @property
    def reason(self) -> CancellationReason | None:
        """Return why cancellation was requested, if it was."""
        return self._reason

    def cancel(self, reason: CancellationReason = CancellationReason.REQUESTED) -> None:
        """Request cancellation. Idempotent; the first reason is kept."""
        with self._lock:
            if self._reason is None:
                self._reason = reason
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancellation is requested or ``timeout`` elapses."""
        return self._event.wait(timeout)


class NullCancellation:
    """A token that is never cancelled.

    Used as the default so operations need no ``if token is not None`` branch.
    """

    __slots__ = ()

    @property
    def cancelled(self) -> bool:
        """Return ``False``; this token is never cancelled."""
        return False

    @property
    def reason(self) -> CancellationReason | None:
        """Return ``None``; this token is never cancelled."""
        return None

    def wait(self, timeout: float | None = None) -> bool:
        """Sleep for ``timeout`` and report that nothing was cancelled."""
        if timeout:
            threading.Event().wait(timeout)
        return False
