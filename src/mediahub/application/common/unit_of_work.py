"""The transaction boundary.

A unit of work groups every repository touched by one use case into a single
atomic operation. Use cases follow one shape, always::

    async with self._unit_of_work() as uow:
        item = await uow.media.get(media_id)
        ...
        await uow.commit()

Leaving the ``async with`` block without committing rolls back. That single
rule is what keeps partial writes out of the system: a use case that raises
halfway through changes nothing.

Repositories are reached *through* the unit of work rather than injected
individually, which guarantees they all share one transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import TracebackType

    from mediahub.domain.download.repository import DownloadJobRepository
    from mediahub.domain.media.repository import MediaRepository


class UnitOfWork(Protocol):
    """An atomic scope over every repository a use case needs."""

    @property
    def media(self) -> MediaRepository:
        """Return the media repository bound to this transaction."""
        ...

    @property
    def download_jobs(self) -> DownloadJobRepository:
        """Return the download job repository bound to this transaction."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction and expose the bound repositories."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless :meth:`commit` already succeeded, then release."""
        ...

    async def commit(self) -> None:
        """Make every staged change durable."""
        ...

    async def rollback(self) -> None:
        """Discard every staged change."""
        ...


class UnitOfWorkFactory(Protocol):
    """Creates a fresh :class:`UnitOfWork` per use case invocation.

    Units of work are never shared or reused: one call, one transaction.
    """

    def __call__(self) -> UnitOfWork:
        """Return a new, unopened unit of work."""
        ...
