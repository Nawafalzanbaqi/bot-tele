"""Use case: report that an attempt finished successfully.

Ordering is the whole content of this use case, and it must not be
"optimised": the job is committed as succeeded **first**, and the lease is
surrendered afterwards.

Committing first means a crash in between leaves a durably successful job whose
lease simply expires - harmless. Surrendering first would leave a window where
the job looks abandoned while it is in fact finished, and a reclaim would run it
a second time. One order risks a tidy-up; the other risks doing the work twice
(``docs/architecture/07-download-pipeline.md`` §7.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.dto import JobSettlement
from mediahub.application.download.errors import LeaseLostError
from mediahub.domain.download.errors import DownloadJobNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock, EventPublisher
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.queue import ClaimedJob, JobQueue


class CompleteJob:
    """Close a job that did everything it was asked to do."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        event_publisher: EventPublisher,
    ) -> None:
        """Wire the use case to its ports."""
        self._queue = queue
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._event_publisher = event_publisher

    async def execute(self, claimed: ClaimedJob) -> JobSettlement:
        """Mark the job succeeded and release its lease.

        Args:
            claimed: The claim held by the caller.

        Returns:
            How the job now stands.

        Raises:
            DownloadJobNotFoundError: If the job vanished mid-flight.
            InvalidJobTransitionError: If the job is not running.
        """
        now = self._clock.now()

        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(claimed.job_id)
            if job is None:
                raise DownloadJobNotFoundError(claimed.job_id)
            job.complete(now=now)
            await uow.download_jobs.save(job)
            await uow.commit()

        await self._event_publisher.publish(job.pull_events())
        await self._close_quietly(claimed)

        logger.bind(
            job_id=str(claimed.job_id),
            attempt=claimed.attempt,
            bytes=job.progress.downloaded_bytes,
        ).info("Completed download job")
        return JobSettlement(job_id=claimed.job_id.value, status=job.status)

    async def _close_quietly(self, claimed: ClaimedJob) -> None:
        """Surrender the lease, tolerating one that has already gone.

        The job is durably successful by this point. Losing the lease now is a
        tidiness problem, not a correctness one, and raising would turn a
        finished job into a reported failure.
        """
        try:
            await self._queue.close(claimed.lease, now=self._clock.now())
        except LeaseLostError:
            logger.bind(job_id=str(claimed.job_id)).warning(
                "Lease was already reclaimed when closing a completed job"
            )
