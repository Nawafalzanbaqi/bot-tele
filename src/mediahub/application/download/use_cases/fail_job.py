"""Use case: report that an attempt failed, and decide what happens next.

The decision is not made here. It is read off two things the domain already
owns: the failure's :class:`~mediahub.domain.download.enums.FailureKind`, set by
whichever adapter understood the error, and the job's own
:class:`~mediahub.domain.download.value_objects.RetryPolicy`. This use case
applies them; the worker that called it knows neither.

The rules, restated because they are the ones most often eroded
(``docs/architecture/07-download-pipeline.md`` §7.7):

* **Permanent and policy failures never retry.** Retrying a DRM error three
  times with exponential backoff wastes an hour and teaches the user that the
  product is unreliable.
* **A transient failure retries until the budget is spent**, then stops and
  stays failed - the state a human is expected to look at.
* **A provider that stated a delay outranks any calculated backoff.** Guessing
  when the other side has told you the answer is self-inflicted damage.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.dto import JobSettlement
from mediahub.domain.download.errors import DownloadJobNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mediahub.application.common.ports import Clock, EventPublisher
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.failures import FailureReport
    from mediahub.application.download.queue import ClaimedJob, JobQueue
    from mediahub.domain.download.entities import DownloadJob


def requeue_or_leave_failed(job: DownloadJob, *, reason: str, now: datetime) -> bool:
    """Record a failed attempt and return the job to the queue if it may retry.

    Shared by the failure path and the lease-recovery sweep, because they are
    the same rule seen from two directions: an attempt ended badly, and the
    budget decides whether there is another one.

    Args:
        job: The job that was interrupted or failed.
        reason: Operator-facing explanation, kept on the job.
        now: Current time.

    Returns:
        ``True`` if the job is queued again, ``False`` if its budget is spent
        and it stays failed - the state that needs a human.
    """
    job.fail(reason=reason, now=now)
    if not job.can_retry:
        return False
    job.requeue(now=now)
    return True


class FailJob:
    """Record why an attempt failed and either retry it or stop."""

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

    async def execute(self, claimed: ClaimedJob, failure: FailureReport) -> JobSettlement:
        """Settle the job according to how it failed.

        Args:
            claimed: The claim held by the caller.
            failure: The typed report. Never an exception, never a string: the
                classification is the retry decision.

        Returns:
            How the job now stands, including when it may next be claimed.

        Raises:
            DownloadJobNotFoundError: If the job vanished mid-flight.
            LeaseLostError: If the caller no longer owns the job.
        """
        now = self._clock.now()
        reason = f"[{failure.code}] {failure.message}"

        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(claimed.job_id)
            if job is None:
                raise DownloadJobNotFoundError(claimed.job_id)

            # Settled underneath us - cancelled through the API while the stage
            # was still running. The outcome that reached the user first is the
            # one that stands, so there is nothing to write.
            settled_already = job.is_terminal
            if not settled_already:
                if failure.is_retryable:
                    retrying = requeue_or_leave_failed(job, reason=reason, now=now)
                else:
                    # Permanent or policy: recorded once, never requeued.
                    job.fail(reason=reason, now=now)
                    retrying = False
                await uow.download_jobs.save(job)
                await uow.commit()

        if settled_already:
            await self._queue.close(claimed.lease, now=now, failure=failure)
            return JobSettlement(
                job_id=claimed.job_id.value, status=job.status, failure_code=failure.code
            )

        available_at = self._available_at(job, failure, now=now) if retrying else None
        await self._event_publisher.publish(job.pull_events())

        if retrying:
            await self._queue.release(claimed.lease, now=now, available_at=available_at)
        else:
            await self._queue.close(claimed.lease, now=now, failure=failure)

        logger.bind(
            job_id=str(claimed.job_id),
            attempt=claimed.attempt,
            kind=failure.kind.value,
            code=failure.code,
            stage=None if failure.stage is None else failure.stage.value,
            retrying=retrying,
        ).warning("Download job attempt failed")

        return JobSettlement(
            job_id=claimed.job_id.value,
            status=job.status,
            retrying=retrying,
            available_at=available_at,
            failure_code=failure.code,
        )

    @staticmethod
    def _available_at(
        job: DownloadJob,
        failure: FailureReport,
        *,
        now: datetime,
    ) -> datetime:
        """Return when the job may be claimed again.

        A delay supplied by the other side always wins; otherwise the job's own
        retry policy decides, using the attempt that is about to be made.
        """
        if failure.retry_after_seconds is not None:
            return now + timedelta(seconds=failure.retry_after_seconds)
        return now + timedelta(seconds=job.retry_policy.delay_for(job.attempts + 1))
