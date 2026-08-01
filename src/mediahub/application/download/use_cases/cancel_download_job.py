"""Use case: stop a download job that has not finished."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.dto import DownloadJobSummary
from mediahub.domain.download.errors import DownloadJobNotFoundError
from mediahub.domain.download.value_objects import JobId

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock, EventPublisher
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.dto import CancelDownloadJobCommand


class CancelDownloadJob:
    """Cancel a queued or running job.

    Cancellation is recorded as state and announced as an event. When a worker
    exists it will observe the cancelled state - or the event - and abandon the
    transfer; nothing about this use case changes then, which is the point of
    keeping intent and execution separate.
    """

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        event_publisher: EventPublisher,
    ) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._event_publisher = event_publisher

    async def execute(self, request: CancelDownloadJobCommand) -> DownloadJobSummary:
        """Cancel the job and announce the change.

        Args:
            request: The command naming the job.

        Returns:
            A summary of the cancelled job.

        Raises:
            DownloadJobNotFoundError: If no job exists with that identifier.
            InvalidJobTransitionError: If the job already reached an end state.
        """
        job_id = JobId(request.job_id)

        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(job_id)
            if job is None:
                raise DownloadJobNotFoundError(job_id)

            job.cancel(now=self._clock.now())
            await uow.download_jobs.save(job)
            await uow.commit()

        await self._event_publisher.publish(job.pull_events())
        logger.bind(job_id=str(job_id), media_id=str(job.media_id)).info("Cancelled download job")
        return DownloadJobSummary.from_entity(job)
