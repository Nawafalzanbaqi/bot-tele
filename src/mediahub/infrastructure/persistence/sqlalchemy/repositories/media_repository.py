"""SQLAlchemy adapter for :class:`~mediahub.domain.media.repository.MediaRepository`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from mediahub.domain.common.pagination import Page
from mediahub.infrastructure.persistence.sqlalchemy.mappers import (
    apply_media_to_model,
    media_to_domain,
    media_to_model,
)
from mediahub.infrastructure.persistence.sqlalchemy.models import MediaItemModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from mediahub.domain.common.pagination import PageRequest
    from mediahub.domain.media.entities import MediaItem
    from mediahub.domain.media.repository import MediaFilter
    from mediahub.domain.media.value_objects import MediaId, SourceUrl


class SqlAlchemyMediaRepository:
    """Stores and retrieves media aggregates in PostgreSQL."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to the unit of work's session."""
        self._session = session

    async def add(self, media: MediaItem) -> None:
        """Insert a newly registered item, within the caller's transaction.

        Flushed rather than merely staged, so that inserts happen in the order
        the caller made them. See
        :meth:`~mediahub.infrastructure.persistence.sqlalchemy.repositories.download_job_repository.SqlAlchemyDownloadJobRepository.add`
        for why the default order is not the safe one.
        """
        self._session.add(media_to_model(media))
        await self._session.flush()

    async def save(self, media: MediaItem) -> None:
        """Stage the current state of an already-known item.

        Falls back to an insert when the row is missing, which keeps ``save``
        usable for aggregates rebuilt outside this session.
        """
        row = await self._session.get(MediaItemModel, media.id.value)
        if row is None:
            self._session.add(media_to_model(media))
            return
        apply_media_to_model(media, row)

    async def get(self, media_id: MediaId) -> MediaItem | None:
        """Return the item with this identifier, or ``None``."""
        row = await self._session.get(MediaItemModel, media_id.value)
        return None if row is None else media_to_domain(row)

    async def find_by_source_url(self, source_url: SourceUrl) -> MediaItem | None:
        """Return the item catalogued for this URL, or ``None``."""
        statement = select(MediaItemModel).where(MediaItemModel.source_url == str(source_url))
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return None if row is None else media_to_domain(row)

    async def find_many(self, *, filters: MediaFilter, page: PageRequest) -> Page[MediaItem]:
        """Return one page of items matching ``filters``, newest first."""
        clauses = _filter_clauses(filters)

        total = (
            await self._session.execute(
                select(func.count()).select_from(MediaItemModel).where(*clauses)
            )
        ).scalar_one()

        statement = (
            select(MediaItemModel)
            .where(*clauses)
            .order_by(MediaItemModel.created_at.desc(), MediaItemModel.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        rows = (await self._session.execute(statement)).scalars().all()

        return Page(
            items=[media_to_domain(row) for row in rows],
            total=total,
            limit=page.limit,
            offset=page.offset,
        )

    async def delete(self, media_id: MediaId) -> None:
        """Stage removal of an item. Deleting an unknown id is a no-op."""
        await self._session.execute(
            delete(MediaItemModel).where(MediaItemModel.id == media_id.value)
        )


def _filter_clauses(filters: MediaFilter) -> list[ColumnElement[bool]]:
    """Build the WHERE clauses shared by the page query and its count query.

    Sharing them is what keeps ``total`` consistent with ``items``; two
    separately maintained filter lists drift and produce impossible pagination.
    """
    clauses: list[ColumnElement[bool]] = []
    if filters.status is not None:
        clauses.append(MediaItemModel.status == filters.status.value)
    if filters.media_type is not None:
        clauses.append(MediaItemModel.media_type == filters.media_type.value)
    if filters.search:
        clauses.append(MediaItemModel.title.ilike(f"%{filters.search}%"))
    return clauses
