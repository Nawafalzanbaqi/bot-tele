"""Use case: take the next due job and start an attempt on it.

The claim is where the queue and the aggregate meet. The queue guarantees that
exactly one worker wins the row; the aggregate records what that means in
business terms - the job is running, and an attempt has been spent.

**An attempt is consumed at claim time, not at failure.** A worker that dies
without reporting has still used one, otherwise a job that reliably kills its
worker would be retried forever (``docs/architecture/09-queue-architecture.md``
§9.4). The counter-rule - a *graceful* release must not charge an attempt - is
honoured by :mod:`mediahub.application.download.use_cases.release_job`, which
never calls this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.domain.download.enums import JobStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock, EventPublisher
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.queue import ClaimedJob, JobQueue, WorkerId

DEFAULT_LEASE_SECONDS: Final[float] = 120.0
"""Long enough to survive a slow stage, short enough that a crash is noticed."""


class ClaimJob:
    """Claim one job and mark it as running."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        event_publisher: EventPublisher,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        """Wire the use case to its ports."""
        self._queue = queue
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._event_publisher = event_publisher
        self._lease_seconds = lease_seconds

    async def execute(self, *, worker: WorkerId) -> ClaimedJob | None:
        """Take the highest-priority due job, if there is one.

        Args:
            worker: The identity claiming the work.

        Returns:
            The claim, carrying the lease, any checkpoint left by a previous
            attempt, and the attempt number - or ``None`` when nothing is due.
            A queue row whose job has vanished or already finished is surrendered
            rather than executed, and reported as "nothing due".
        """
        now = self._clock.now()
        claimed = await self._queue.claim(worker=worker, lease_seconds=self._lease_seconds, now=now)
        if claimed is None:
            return None

        bound = logger.bind(job_id=str(claimed.job_id), worker=str(worker))

        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(claimed.job_id)
            if job is None or job.status is not JobStatus.QUEUED:
                # Missing or already settled: close rather than release, so the
                # loop cannot spin against a row that can never succeed.
                await self._queue.close(claimed.lease, now=now)
                bound.bind(status=None if job is None else job.status.value).warning(
                    "Claimed a job that cannot be started; lease surrendered"
                )
                return None

            job.start(now=now)
            await uow.download_jobs.save(job)
            await uow.commit()

        await self._event_publisher.publish(job.pull_events())
        bound.bind(attempt=job.attempts, resuming=not claimed.checkpoint.is_fresh).info(
            "Claimed download job"
        )
        return claimed.with_attempt(job.attempts)
