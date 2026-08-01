"""In-memory acquisition journal.

Implements :class:`~mediahub.application.download.journal.AcquisitionJournal`.

**Everything here is lost when the process exits**, which is honest about what
it is: the interim record described in the journal port's docstring, standing in
until the Catalogue's custody model provides a durable one. The port is what
matters - swapping this for a table changes one line in the container.

Entries are bounded per principal so that a long-running gateway cannot grow
without limit; the oldest are dropped first.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.download.journal import JournalEntry

DEFAULT_CAPACITY: Final[int] = 200


class InMemoryAcquisitionJournal:
    """Keeps recent entries per principal, newest last."""

    __slots__ = ("_capacity", "_entries", "_lock")

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        """Create an empty journal bounded at ``capacity`` per principal."""
        self._capacity = max(1, capacity)
        self._entries: dict[str, deque[JournalEntry]] = {}
        self._lock = threading.Lock()

    async def record(self, entry: JournalEntry) -> None:
        """Append one entry, evicting the oldest once the bound is reached."""
        with self._lock:
            bucket = self._entries.setdefault(entry.principal, deque(maxlen=self._capacity))
            bucket.append(entry)

    async def recent(self, principal: str, *, limit: int = 10) -> Sequence[JournalEntry]:
        """Return a principal's most recent entries, newest first."""
        with self._lock:
            bucket = self._entries.get(principal)
            if not bucket:
                return ()
            return tuple(reversed(list(bucket)[-limit:]))

    def clear(self) -> None:
        """Drop everything. Test helper; never called in production paths."""
        with self._lock:
            self._entries.clear()
