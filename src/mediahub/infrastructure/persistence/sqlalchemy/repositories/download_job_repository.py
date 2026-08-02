"""SQLAlchemy adapter for :class:`~mediahub.domain.download.repository.DownloadJobRepository`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from mediahub.domain.common.pagination import Page
from mediahub.domain.download.enums import JobStatus
from mediahub.infrastructure.persistence.sqlalchemy.mappers import (
    apply_job_to_model,
    job_to_domain,
    job_to_model,
)
from mediahub.infrastructure.persistence.sqlalchemy.models import DownloadJobModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from mediahub.domain.common.pagination import PageRequest
    from mediahub.domain.download.entities import DownloadJob
    from mediahub.domain.download.repository import DownloadJobFilter
    from mediahub.domain.download.value_objects import JobId
    from mediahub.domain.media.value_objects import MediaId

ACTIVE_STATUSES = (JobStatus.QUEUED.value, JobStatus.RUNNING.value)
"""States that occupy queue or worker capacity, mirrored by the partial index."""


class SqlAlchemyDownloadJobRepository:
    """Stores and retrieves download job aggregates in PostgreSQL."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to the unit of work's session."""
        self._session = session

    async def add(self, job: DownloadJob) -> None:
        """Insert a newly requested job, within the caller's transaction.

        Flushed rather than merely staged. Without a ``relationship`` between
        the models - which this schema deliberately does not have - SQLAlchemy
        has no dependency to order a flush by, and it emits the two inserts in
        mapper order: ``download_jobs`` before ``media_items``. A caller that
        catalogues an item and queues work for it in one transaction would then
        fail on a foreign key that is perfectly satisfied, because the row it
        points at is one statement away from existing.

        Flushing here makes the order the caller's, which is the order that is
        actually correct. Nothing is committed - a later exception still rolls
        the whole unit of work back.
        """
        self._session.add(job_to_model(job))
        await self._session.flush()

    async def save(self, job: DownloadJob) -> None:
        """Stage the current state of an already-known job."""
        row = await self._session.get(DownloadJobModel, job.id.value)
        if row is None:
            self._session.add(job_to_model(job))
            return
        apply_job_to_model(job, row)

    async def get(self, job_id: JobId) -> DownloadJob | None:
        """Return the job with this identifier, or ``None``."""
        row = await self._session.get(DownloadJobModel, job_id.value)
        return None if row is None else job_to_domain(row)

    async def find_active_for_media(self, media_id: MediaId) -> DownloadJob | None:
        """Return the queued or running job for a media item, or ``None``."""
        statement = select(DownloadJobModel).where(
            DownloadJobModel.media_id == media_id.value,
            DownloadJobModel.status.in_(ACTIVE_STATUSES),
        )
        row = (await self._session.execute(statement)).scalars().first()
        return None if row is None else job_to_domain(row)

    async def find_many(
        self, *, filters: DownloadJobFilter, page: PageRequest
    ) -> Page[DownloadJob]:
        """Return one page of jobs matching ``filters``, newest first."""
        clauses = _filter_clauses(filters)

        total = (
            await self._session.execute(
                select(func.count()).select_from(DownloadJobModel).where(*clauses)
            )
        ).scalar_one()

        statement = (
            select(DownloadJobModel)
            .where(*clauses)
            .order_by(DownloadJobModel.created_at.desc(), DownloadJobModel.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        rows = (await self._session.execute(statement)).scalars().all()

        return Page(
            items=[job_to_domain(row) for row in rows],
            total=total,
            limit=page.limit,
            offset=page.offset,
        )

    async def claim_next_queued(self, *, limit: int) -> Sequence[DownloadJob]:
        """Atomically take up to ``limit`` queued jobs, highest priority first.

        Uses ``FOR UPDATE SKIP LOCKED``: rows claimed here stay locked for the
        rest of the surrounding transaction, so a second worker polling
        concurrently skips straight past them instead of blocking or receiving
        duplicates.
        """
        statement = (
            select(DownloadJobModel)
            .where(DownloadJobModel.status == JobStatus.QUEUED.value)
            .order_by(DownloadJobModel.priority_weight.desc(), DownloadJobModel.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self._session.execute(statement)).scalars().all()
        return [job_to_domain(row) for row in rows]

    async def delete(self, job_id: JobId) -> None:
        """Stage removal of a job. Deleting an unknown id is a no-op."""
        await self._session.execute(
            delete(DownloadJobModel).where(DownloadJobModel.id == job_id.value)
        )


def _filter_clauses(filters: DownloadJobFilter) -> list[ColumnElement[bool]]:
    """Build the WHERE clauses shared by the page query and its count query."""
    clauses: list[ColumnElement[bool]] = []
    if filters.status is not None:
        clauses.append(DownloadJobModel.status == filters.status.value)
    if filters.priority is not None:
        clauses.append(DownloadJobModel.priority == filters.priority.value)
    if filters.media_id is not None:
        clauses.append(DownloadJobModel.media_id == filters.media_id.value)
    return clauses
