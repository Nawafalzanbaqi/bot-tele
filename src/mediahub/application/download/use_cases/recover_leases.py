"""Use case: take back work whose owner is gone.

This is the whole of crash recovery. A worker that dies stops renewing its
lease; once the lease lapses the job is nobody's, and this sweep returns it to
the queue where it is claimed and resumed from its last checkpoint. Nothing
detects the crash - the absence of a renewal *is* the detection
(``docs/architecture/10-worker-architecture.md`` §10.6).

Two modes, same mechanism:

* **Expired leases**, from any worker. The attempt already charged at claim
  stays charged: a job that reliably kills its worker must run out of budget
  rather than take the system down with it.
* **This worker's own leases**, expired or not, released at startup. A restarted
  process has the same identity as the one that died, so waiting a full lease
  period to recover its own work would be a needless stall on every restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.download.dto import LeaseRecovery
from mediahub.application.download.use_cases.fail_job import requeue_or_leave_failed
from mediahub.domain.download.enums import JobStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from mediahub.application.common.ports import Clock, EventPublisher
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.queue import JobQueue, Lease, WorkerId
    from mediahub.domain.common.events import DomainEvent

EXPIRED_LEASE_REASON: Final[str] = "[lease_expired] the worker holding this job stopped reporting"
OWN_LEASE_REASON: Final[str] = "[worker_restarted] the worker holding this job was restarted"


class RecoverLeases:
    """Return jobs whose lease has lapsed - or whose worker restarted - to the queue."""

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

    async def execute(self, *, worker: WorkerId | None = None) -> LeaseRecovery:
        """Reclaim leases and put their jobs back where a worker can find them.

        Args:
            worker: When given, reclaim every lease held by that identity
                regardless of expiry - the startup sweep. When omitted, reclaim
                only leases that have lapsed - the crash sweep.

        Returns:
            Which jobs went back to the queue, and which had no budget left.
        """
        now = self._clock.now()
        if worker is None:
            leases = await self._queue.reclaim_expired(now=now)
            reason = EXPIRED_LEASE_REASON
        else:
            leases = await self._queue.release_owned_by(worker, now=now)
            reason = OWN_LEASE_REASON

        if not leases:
            return LeaseRecovery()

        recovery = await self._requeue_all(leases, reason=reason, now=now)
        logger.bind(
            worker=None if worker is None else str(worker),
            reclaimed=len(recovery.reclaimed),
            abandoned=len(recovery.abandoned),
        ).warning("Recovered leases from a worker that stopped reporting")
        return recovery

    async def _requeue_all(
        self,
        leases: Sequence[Lease],
        *,
        reason: str,
        now: datetime,
    ) -> LeaseRecovery:
        """Put every reclaimed job back, in one transaction.

        One transaction for the sweep rather than one per job: a partial sweep
        would leave some jobs running with no lease, which is the one state the
        queue cannot recover from on its own.
        """
        reclaimed: list[UUID] = []
        abandoned: list[UUID] = []
        events: list[DomainEvent] = []

        async with self._unit_of_work() as uow:
            for lease in leases:
                job = await uow.download_jobs.get(lease.job_id)
                if job is None or job.status is not JobStatus.RUNNING:
                    # A lease only ever covers a running job. Anything else has
                    # already been settled or released by its own worker.
                    continue
                if requeue_or_leave_failed(job, reason=reason, now=now):
                    reclaimed.append(lease.job_id.value)
                else:
                    abandoned.append(lease.job_id.value)
                await uow.download_jobs.save(job)
                events.extend(job.pull_events())
            await uow.commit()

        await self._event_publisher.publish(events)
        return LeaseRecovery(reclaimed=tuple(reclaimed), abandoned=tuple(abandoned))
