"""Use case: stop a running job because someone asked it to stop.

Cancellation arrives as a flag, not as a signal. Whoever wants the job stopped
sets it; the worker notices at its next heartbeat or stage boundary and calls
this. There is deliberately no forced kill: terminating a worker mid-write risks
partial files, orphaned subprocesses and half-written rows, and the sweeper
would then have to clean up a mess that cooperative cancellation avoids entirely
(``docs/architecture/09-queue-architecture.md`` §9.7).

Acknowledgement is terminal. A cancelled job is never resumed - asking again
creates a new job, which keeps history honest about what was actually run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.dto import JobSettlement
from mediahub.domain.download.errors import DownloadJobNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock, EventPublisher
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.queue import ClaimedJob, JobQueue


class AcknowledgeCancellation:
    """Confirm that a running job has stopped on request."""

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
        """Mark the job cancelled and surrender its lease.

        Args:
            claimed: The claim held by the caller.

        Returns:
            How the job now stands. A job already cancelled through another
            route is reported as it is rather than transitioned twice.

        Raises:
            DownloadJobNotFoundError: If the job vanished mid-flight.
            LeaseLostError: If the caller no longer owns the job.
        """
        now = self._clock.now()

        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(claimed.job_id)
            if job is None:
                raise DownloadJobNotFoundError(claimed.job_id)
            if not job.is_terminal:
                job.cancel(now=now)
                await uow.download_jobs.save(job)
                await uow.commit()

        await self._event_publisher.publish(job.pull_events())
        await self._queue.close(claimed.lease, now=now)

        logger.bind(
            job_id=str(claimed.job_id),
            attempt=claimed.attempt,
            status=job.status.value,
        ).info("Acknowledged cancellation of download job")
        return JobSettlement(job_id=claimed.job_id.value, status=job.status)
