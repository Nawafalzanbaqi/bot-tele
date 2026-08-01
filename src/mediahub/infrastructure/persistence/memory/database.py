"""The in-memory store backing the memory adapters.

Two dictionaries keyed by identifier, plus enough copy-on-write to give real
transactional semantics: a unit of work operates on a private snapshot and only
publishes it back on ``commit``. A use case that raises halfway therefore
leaves the store untouched, exactly as a rolled-back SQL transaction would.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from uuid import UUID

    from mediahub.domain.download.entities import DownloadJob
    from mediahub.domain.media.entities import MediaItem


@dataclass(slots=True)
class InMemoryTables:
    """A consistent snapshot of every table.

    Attributes:
        media: Media items keyed by identifier.
        download_jobs: Download jobs keyed by identifier.
    """

    media: dict[UUID, MediaItem] = field(default_factory=dict)
    download_jobs: dict[UUID, DownloadJob] = field(default_factory=dict)

    def copy(self) -> InMemoryTables:
        """Return a deep copy, isolating callers from later mutations."""
        return InMemoryTables(
            media=deepcopy(self.media),
            download_jobs=deepcopy(self.download_jobs),
        )


class InMemoryDatabase:
    """Process-wide storage shared by every in-memory unit of work.

    One instance per container. Units of work check out a snapshot, mutate it
    freely, and commit it back atomically.
    """

    __slots__ = ("_tables",)

    def __init__(self) -> None:
        """Start with empty tables."""
        self._tables = InMemoryTables()

    def checkout(self) -> InMemoryTables:
        """Return a private snapshot for one unit of work to mutate."""
        return self._tables.copy()

    def publish(self, tables: InMemoryTables) -> None:
        """Atomically replace the committed state with ``tables``."""
        self._tables = tables

    def peek(self) -> InMemoryTables:
        """Return the committed tables **without** copying them.

        For readers that scan and never write - the queue's claim, which looks
        at every job on every poll and would otherwise deep-copy the whole store
        once a second. Mutating what this returns corrupts committed state; use
        :meth:`checkout` for anything that intends to change something.
        """
        return self._tables

    def clear(self) -> None:
        """Drop everything. Test helper; never called in production paths."""
        self._tables = InMemoryTables()
