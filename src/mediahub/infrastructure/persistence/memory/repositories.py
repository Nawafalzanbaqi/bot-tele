"""In-memory repository adapters.

Implement :class:`~mediahub.domain.media.repository.MediaRepository` and
:class:`~mediahub.domain.download.repository.DownloadJobRepository` over the
dictionaries in :mod:`~mediahub.infrastructure.persistence.memory.database`.

Ordering matches the SQL adapters exactly - newest first, by ``created_at``
then by identifier as a stable tiebreaker - because a test that relies on order
must fail when the real adapter would return something different.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.domain.common.pagination import Page
from mediahub.domain.download.enums import JobStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.domain.common.pagination import PageRequest
    from mediahub.domain.download.entities import DownloadJob
    from mediahub.domain.download.repository import DownloadJobFilter
    from mediahub.domain.download.value_objects import JobId
    from mediahub.domain.media.entities import MediaItem
    from mediahub.domain.media.repository import MediaFilter
    from mediahub.domain.media.value_objects import MediaId, SourceUrl
    from mediahub.infrastructure.persistence.memory.database import InMemoryTables


def _paginate[TItem](items: Sequence[TItem], page: PageRequest) -> Page[TItem]:
    """Apply a window to an already-ordered sequence."""
    window = items[page.offset : page.offset + page.limit]
    return Page(items=window, total=len(items), limit=page.limit, offset=page.offset)


class InMemoryMediaRepository:
    """Dictionary-backed media repository bound to one unit of work."""

    __slots__ = ("_tables",)

    def __init__(self, tables: InMemoryTables) -> None:
        """Bind the repository to a unit of work's private snapshot."""
        self._tables = tables

    async def add(self, media: MediaItem) -> None:
        """Stage a newly registered item for insertion."""
        self._tables.media[media.id.value] = media

    async def save(self, media: MediaItem) -> None:
        """Stage the current state of an already-known item."""
        self._tables.media[media.id.value] = media

    async def get(self, media_id: MediaId) -> MediaItem | None:
        """Return the item with this identifier, or ``None``."""
        return self._tables.media.get(media_id.value)

    async def find_by_source_url(self, source_url: SourceUrl) -> MediaItem | None:
        """Return the item catalogued for this URL, or ``None``."""
        target = str(source_url)
        for item in self._tables.media.values():
            if str(item.source_url) == target:
                return item
        return None

    async def find_many(self, *, filters: MediaFilter, page: PageRequest) -> Page[MediaItem]:
        """Return one page of items matching ``filters``, newest first."""
        search = filters.search.casefold() if filters.search else None
        matches = [
            item
            for item in self._tables.media.values()
            if (filters.status is None or item.status is filters.status)
            and (filters.media_type is None or item.media_type is filters.media_type)
            and (search is None or search in str(item.title).casefold())
        ]
        matches.sort(key=lambda item: (item.created_at, item.id.value), reverse=True)
        return _paginate(matches, page)

    async def delete(self, media_id: MediaId) -> None:
        """Stage removal of an item. Deleting an unknown id is a no-op."""
        self._tables.media.pop(media_id.value, None)


class InMemoryDownloadJobRepository:
    """Dictionary-backed download job repository bound to one unit of work."""

    __slots__ = ("_tables",)

    def __init__(self, tables: InMemoryTables) -> None:
        """Bind the repository to a unit of work's private snapshot."""
        self._tables = tables

    async def add(self, job: DownloadJob) -> None:
        """Stage a newly requested job for insertion."""
        self._tables.download_jobs[job.id.value] = job

    async def save(self, job: DownloadJob) -> None:
        """Stage the current state of an already-known job."""
        self._tables.download_jobs[job.id.value] = job

    async def get(self, job_id: JobId) -> DownloadJob | None:
        """Return the job with this identifier, or ``None``."""
        return self._tables.download_jobs.get(job_id.value)

    async def find_active_for_media(self, media_id: MediaId) -> DownloadJob | None:
        """Return the queued or running job for a media item, or ``None``."""
        for job in self._tables.download_jobs.values():
            if job.media_id == media_id and job.status.is_active:
                return job
        return None

    async def find_many(
        self, *, filters: DownloadJobFilter, page: PageRequest
    ) -> Page[DownloadJob]:
        """Return one page of jobs matching ``filters``, newest first."""
        matches = [
            job
            for job in self._tables.download_jobs.values()
            if (filters.status is None or job.status is filters.status)
            and (filters.priority is None or job.priority is filters.priority)
            and (filters.media_id is None or job.media_id == filters.media_id)
        ]
        matches.sort(key=lambda job: (job.created_at, job.id.value), reverse=True)
        return _paginate(matches, page)

    async def claim_next_queued(self, *, limit: int) -> Sequence[DownloadJob]:
        """Return up to ``limit`` queued jobs, highest priority first.

        Single-process by definition, so no locking is involved; the SQL
        adapter provides the real atomicity guarantee.
        """
        queued = [
            job for job in self._tables.download_jobs.values() if job.status is JobStatus.QUEUED
        ]
        queued.sort(key=lambda job: (-job.priority.weight, job.created_at))
        return queued[:limit]

    async def delete(self, job_id: JobId) -> None:
        """Stage removal of a job. Deleting an unknown id is a no-op."""
        self._tables.download_jobs.pop(job_id.value, None)
