"""Use case: publish how far a job has got.

Progress lands in two places, because two different questions are being asked:

* the **queue record** answers "what is this worker doing right now?" - stage,
  speed, ETA. It is telemetry: losing an update costs nothing.
* the **aggregate** answers "how far along is my download?" for anyone reading
  the job later, and is therefore part of the job's durable state.

Callers throttle. A progress callback fires per chunk, and writing every one of
them to a database on an SD card would destroy the card
(``docs/architecture/05-component-communication.md`` §5.9). Throttling belongs to
the caller because only the caller knows how many jobs are reporting at once;
this use case simply writes what it is given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.domain.download.enums import JobStatus
from mediahub.domain.download.value_objects import DownloadProgress

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.queue import JobQueue, Lease, StageProgress


class ReportJobProgress:
    """Record the latest observation of a running job."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
    ) -> None:
        """Wire the use case to its ports."""
        self._queue = queue
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def execute(self, lease: Lease, progress: StageProgress) -> None:
        """Write ``progress`` to the queue and to the job.

        Args:
            lease: The lease held by the caller.
            progress: What was observed.

        Raises:
            LeaseLostError: If the caller no longer owns the job. Reported
                rather than swallowed: a worker whose lease has gone must stop,
                and a heartbeat is not the only thing that can notice.
        """
        await self._queue.record_progress(lease, progress)

        now = self._clock.now()
        async with self._unit_of_work() as uow:
            job = await uow.download_jobs.get(lease.job_id)
            if job is None or job.status is not JobStatus.RUNNING:
                # Nothing to attach the observation to. Not an error: a job can
                # settle between a stage emitting an update and this write.
                return
            job.report_progress(_as_domain_progress(progress), now=now)
            await uow.download_jobs.save(job)
            await uow.commit()


def _as_domain_progress(progress: StageProgress) -> DownloadProgress:
    """Translate a stage observation into the aggregate's progress value.

    The announced total is raised to the transferred count when a source
    under-declares its size. The bytes on disk are the fact; the declaration is
    a hint from an untrusted party, and a hint must never be able to make a
    truthful observation unrepresentable.
    """
    total = progress.total_bytes
    if total is not None:
        total = max(total, progress.transferred_bytes)
    return DownloadProgress(downloaded_bytes=progress.transferred_bytes, total_bytes=total)
