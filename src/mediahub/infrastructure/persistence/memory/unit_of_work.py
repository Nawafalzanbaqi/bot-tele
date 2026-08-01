"""In-memory unit of work.

Implements :class:`~mediahub.application.common.unit_of_work.UnitOfWork` with
snapshot isolation: entering checks out a private copy of every table, and only
:meth:`InMemoryUnitOfWork.commit` publishes it back. Leaving the block without
committing discards the copy - the same observable behaviour as a rolled-back
SQL transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from mediahub.infrastructure.persistence.memory.repositories import (
    InMemoryDownloadJobRepository,
    InMemoryMediaRepository,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import TracebackType

    from mediahub.domain.download.repository import DownloadJobRepository
    from mediahub.domain.media.repository import MediaRepository
    from mediahub.infrastructure.persistence.memory.database import (
        InMemoryDatabase,
        InMemoryTables,
    )


class NotStartedError(RuntimeError):
    """A unit of work was used before ``async with`` was entered."""

    def __init__(self) -> None:
        """Initialise the error with a fixed, actionable message."""
        super().__init__("The unit of work must be entered with 'async with' before use.")


class InMemoryUnitOfWork:
    """Snapshot-isolated transaction over :class:`InMemoryDatabase`."""

    __slots__ = ("_database", "_download_jobs", "_media", "_tables")

    def __init__(self, database: InMemoryDatabase) -> None:
        """Bind the unit of work to the process-wide store."""
        self._database = database
        self._tables: InMemoryTables | None = None
        self._media: InMemoryMediaRepository | None = None
        self._download_jobs: InMemoryDownloadJobRepository | None = None

    @property
    def media(self) -> MediaRepository:
        """Return the media repository bound to this transaction."""
        if self._media is None:
            raise NotStartedError
        return self._media

    @property
    def download_jobs(self) -> DownloadJobRepository:
        """Return the download job repository bound to this transaction."""
        if self._download_jobs is None:
            raise NotStartedError
        return self._download_jobs

    async def __aenter__(self) -> Self:
        """Check out a private snapshot and bind the repositories to it."""
        self._tables = self._database.checkout()
        self._media = InMemoryMediaRepository(self._tables)
        self._download_jobs = InMemoryDownloadJobRepository(self._tables)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Discard anything not committed and release the snapshot."""
        del exc_type, exc, traceback
        await self.rollback()

    async def commit(self) -> None:
        """Publish the snapshot back to the store, atomically."""
        if self._tables is None:
            raise NotStartedError
        self._database.publish(self._tables)
        # Re-check out so any further work in this scope sees committed state.
        self._tables = self._database.checkout()
        self._media = InMemoryMediaRepository(self._tables)
        self._download_jobs = InMemoryDownloadJobRepository(self._tables)

    async def rollback(self) -> None:
        """Drop the snapshot without publishing it."""
        self._tables = None
        self._media = None
        self._download_jobs = None
