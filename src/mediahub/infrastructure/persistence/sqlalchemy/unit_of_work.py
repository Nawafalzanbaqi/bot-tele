"""SQLAlchemy unit of work.

Implements :class:`~mediahub.application.common.unit_of_work.UnitOfWork` over
one :class:`~sqlalchemy.ext.asyncio.AsyncSession`. The session opens on
``__aenter__`` and always closes on ``__aexit__``; anything not committed is
rolled back, so an exception can never leave half a use case persisted.

Both repositories share the one session, which is what makes a write to
``media`` and a write to ``download_jobs`` a single atomic operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from mediahub.infrastructure.persistence.sqlalchemy.repositories.download_job_repository import (
    SqlAlchemyDownloadJobRepository,
)
from mediahub.infrastructure.persistence.sqlalchemy.repositories.media_repository import (
    SqlAlchemyMediaRepository,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import TracebackType

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from mediahub.domain.download.repository import DownloadJobRepository
    from mediahub.domain.media.repository import MediaRepository


class SessionNotStartedError(RuntimeError):
    """A unit of work was used before ``async with`` was entered."""

    def __init__(self) -> None:
        """Initialise the error with a fixed, actionable message."""
        super().__init__("The unit of work must be entered with 'async with' before use.")


class SqlAlchemyUnitOfWork:
    """A database transaction spanning every repository a use case touches."""

    __slots__ = ("_download_jobs", "_media", "_session", "_session_factory")

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the unit of work to a session factory."""
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._media: SqlAlchemyMediaRepository | None = None
        self._download_jobs: SqlAlchemyDownloadJobRepository | None = None

    @property
    def media(self) -> MediaRepository:
        """Return the media repository bound to this transaction."""
        if self._media is None:
            raise SessionNotStartedError
        return self._media

    @property
    def download_jobs(self) -> DownloadJobRepository:
        """Return the download job repository bound to this transaction."""
        if self._download_jobs is None:
            raise SessionNotStartedError
        return self._download_jobs

    async def __aenter__(self) -> Self:
        """Open a session and bind both repositories to it."""
        self._session = self._session_factory()
        self._media = SqlAlchemyMediaRepository(self._session)
        self._download_jobs = SqlAlchemyDownloadJobRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back anything uncommitted, then always close the session."""
        del exc_type, exc, traceback
        if self._session is None:
            return
        try:
            await self._session.rollback()
        finally:
            await self._session.close()
            self._session = None
            self._media = None
            self._download_jobs = None

    async def commit(self) -> None:
        """Flush and commit every staged change."""
        if self._session is None:
            raise SessionNotStartedError
        await self._session.commit()

    async def rollback(self) -> None:
        """Discard every staged change."""
        if self._session is None:
            raise SessionNotStartedError
        await self._session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    """Creates one :class:`SqlAlchemyUnitOfWork` per use case invocation.

    Satisfies :class:`~mediahub.application.common.unit_of_work.UnitOfWorkFactory`.
    """

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the factory to the process-wide session factory."""
        self._session_factory = session_factory

    def __call__(self) -> SqlAlchemyUnitOfWork:
        """Return a new, unopened unit of work."""
        return SqlAlchemyUnitOfWork(self._session_factory)
