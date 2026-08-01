"""Use case: hand a running job back, unharmed, because we are stopping.

This is what a graceful shutdown does with work in flight. It is *not* a
failure: nothing went wrong, the process is going away. So the attempt consumed
at claim time is refunded and the job returns to the queue immediately, where
the next worker picks it up and resumes from its checkpoint.

Explicitly releasing rather than letting the lease lapse turns a deploy from
"every in-flight job stalls for two minutes" into "jobs continue immediately on
the new worker" (``docs/architecture/10-worker-architecture.md`` §10.7).
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


class ReleaseJob:
    """Return an interrupted job to the queue without charging an attempt."""

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
        """Put the job back on the queue, claimable immediately.

        Args:
            claimed: The claim held by the caller.

        Returns:
            How the job now stands.

        Raises:
            DownloadJobNotFoundError: If the job vanished mid-flight.
            InvalidJobTransitionError: If the job is not running.
            LeaseLostError: If the caller no longer owns the job.
        """
        now = self._clock.now()

        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(claimed.job_id)
            if job is None:
                raise DownloadJobNotFoundError(claimed.job_id)
            job.release(now=now)
            await uow.download_jobs.save(job)
            await uow.commit()

        await self._event_publisher.publish(job.pull_events())
        await self._queue.release(claimed.lease, now=now, available_at=None)

        logger.bind(
            job_id=str(claimed.job_id),
            attempt=claimed.attempt,
            completed_stages=len(claimed.checkpoint.completed_stages),
        ).info("Released download job for another worker")
        return JobSettlement(job_id=claimed.job_id.value, status=job.status, retrying=True)
