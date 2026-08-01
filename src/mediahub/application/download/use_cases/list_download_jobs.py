"""Use case: read a page of download jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.application.download.dto import DownloadJobSummary
from mediahub.domain.common.pagination import Page
from mediahub.domain.download.repository import DownloadJobFilter
from mediahub.domain.media.value_objects import MediaId

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.dto import ListDownloadJobsQuery


class ListDownloadJobs:
    """Browse the job queue, optionally narrowed by state, priority or item.

    This is the operator's window onto what MediaHub is doing and what it has
    tried, so the projection includes progress and the last failure reason.
    """

    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work = unit_of_work

    async def execute(self, request: ListDownloadJobsQuery) -> Page[DownloadJobSummary]:
        """Return one page of matching jobs, newest first.

        Args:
            request: Filters plus the requested window.

        Returns:
            A page of summaries, carrying the total match count.
        """
        filters = DownloadJobFilter(
            status=request.status,
            priority=request.priority,
            media_id=None if request.media_id is None else MediaId(request.media_id),
        )

        async with self._unit_of_work() as uow:
            page = await uow.download_jobs.find_many(filters=filters, page=request.page)

        return Page(
            items=[DownloadJobSummary.from_entity(job) for job in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )
