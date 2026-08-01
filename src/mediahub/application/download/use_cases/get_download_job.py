"""Use case: read a single download job."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.application.download.dto import DownloadJobSummary
from mediahub.domain.download.errors import DownloadJobNotFoundError
from mediahub.domain.download.value_objects import JobId

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.dto import GetDownloadJobQuery


class GetDownloadJob:
    """Fetch one job by identifier, including its progress and last error."""

    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work = unit_of_work

    async def execute(self, request: GetDownloadJobQuery) -> DownloadJobSummary:
        """Return the requested job.

        Args:
            request: The query naming the job.

        Returns:
            A summary of the job.

        Raises:
            DownloadJobNotFoundError: If no job exists with that identifier.
        """
        job_id = JobId(request.job_id)

        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(job_id)

        if job is None:
            raise DownloadJobNotFoundError(job_id)
        return DownloadJobSummary.from_entity(job)
