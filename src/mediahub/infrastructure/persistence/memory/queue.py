"""In-memory job queue adapter.

Implements :class:`~mediahub.application.download.queue.JobQueue` over the same
:class:`~mediahub.infrastructure.persistence.memory.database.InMemoryDatabase`
the repositories use, plus one table of its own for the scheduling mechanics -
lease, checkpoint, cancellation flag and last observation.

Three decisions worth keeping in mind when reading it:

* **Claiming reads committed state directly, not a unit of work.** The claim is
  a single atomic step, exactly as the SQL adapter's conditional ``UPDATE`` will
  be (``docs/architecture/09-queue-architecture.md`` §9.4). Wrapping it in a
  transaction that a caller could extend would let two workers win the same row.
* **One lock guards every operation.** SQLite serialises writers; so does this,
  for the same reason and with the same effect - the loser of a race sees no
  work rather than duplicate work.
* **Ownership is checked on every write.** A worker whose lease has been
  reclaimed gets :class:`~mediahub.application.download.errors.LeaseLostError`
  and must stop, which is what prevents a stalled worker from writing over the
  one that took its job.

Ordering matches what the SQL adapter must do: highest priority first, then
oldest first. A test that relies on order has to fail when the real adapter
would return something different.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from mediahub.application.download.errors import LeaseLostError
from mediahub.application.download.queue import (
    Checkpoint,
    ClaimedJob,
    Lease,
    LeaseState,
)
from mediahub.domain.download.enums import JobStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from uuid import UUID

    from mediahub.application.download.failures import FailureReport
    from mediahub.application.download.queue import StageProgress, WorkerId
    from mediahub.domain.download.entities import DownloadJob
    from mediahub.domain.download.value_objects import JobId
    from mediahub.infrastructure.persistence.memory.database import InMemoryDatabase


@dataclass(slots=True)
class _QueueEntry:
    """The scheduling mechanics of one job.

    Attributes:
        lease: The current owner, when the job is leased.
        checkpoint: What the job has completed.
        cancel_requested: Whether someone asked it to stop.
        available_at: The earliest it may be claimed; ``None`` means now.
        closed: Whether it reached a terminal state and must never run again.
        progress: The most recent observation, kept for diagnostics.
        failure: Why it ended, when it ended badly.
    """

    lease: Lease | None = None
    checkpoint: Checkpoint = field(default_factory=Checkpoint)
    cancel_requested: bool = False
    available_at: datetime | None = None
    closed: bool = False
    progress: StageProgress | None = None
    failure: FailureReport | None = None


class InMemoryJobQueue:
    """Dictionary-backed queue with real lease semantics."""

    __slots__ = ("_database", "_entries", "_lock")

    def __init__(self, database: InMemoryDatabase) -> None:
        """Bind the queue to the store the repositories commit into."""
        self._database = database
        self._entries: dict[UUID, _QueueEntry] = {}
        self._lock = threading.Lock()

    # -- Claiming ------------------------------------------------------------

    async def claim(
        self,
        *,
        worker: WorkerId,
        lease_seconds: float,
        now: datetime,
    ) -> ClaimedJob | None:
        """Take the highest-priority due job, or return ``None``."""
        with self._lock:
            candidate = self._next_due(now)
            if candidate is None:
                return None
            entry = self._entry_for(candidate.id.value)
            entry.lease = Lease(
                job_id=candidate.id,
                owner=worker,
                acquired_at=now,
                expires_at=now + timedelta(seconds=lease_seconds),
            )
            entry.available_at = None
            return ClaimedJob(
                lease=entry.lease,
                checkpoint=entry.checkpoint,
                cancel_requested=entry.cancel_requested,
            )

    def _next_due(self, now: datetime) -> DownloadJob | None:
        """Return the job that should run next, if any.

        Queued, not leased, not gated by a backoff and not asked to stop -
        highest priority first, oldest first within a priority.
        """
        due = [
            job
            for job in self._database.peek().download_jobs.values()
            if job.status is JobStatus.QUEUED and self._is_due(job.id.value, now)
        ]
        if not due:
            return None
        due.sort(key=lambda job: (-job.priority.weight, job.created_at, job.id.value))
        return due[0]

    def _is_due(self, job_uuid: UUID, now: datetime) -> bool:
        """Return whether the queue's own state permits claiming this job."""
        entry = self._entries.get(job_uuid)
        if entry is None:
            return True
        if entry.closed or entry.cancel_requested:
            return False
        if entry.lease is not None and not entry.lease.is_expired(now):
            return False
        return entry.available_at is None or entry.available_at <= now

    # -- Holding -------------------------------------------------------------

    async def extend_lease(
        self,
        lease: Lease,
        *,
        lease_seconds: float,
        now: datetime,
    ) -> LeaseState:
        """Renew the lease and report the cancellation flag."""
        with self._lock:
            entry = self._owned(lease)
            entry.lease = lease.extended_to(now + timedelta(seconds=lease_seconds))
            return LeaseState(lease=entry.lease, cancel_requested=entry.cancel_requested)

    async def save_checkpoint(self, lease: Lease, checkpoint: Checkpoint) -> None:
        """Persist what the job has completed so far."""
        with self._lock:
            self._owned(lease).checkpoint = checkpoint

    async def record_progress(self, lease: Lease, progress: StageProgress) -> None:
        """Record the latest observation of the job."""
        with self._lock:
            self._owned(lease).progress = progress

    # -- Settling ------------------------------------------------------------

    async def release(
        self,
        lease: Lease,
        *,
        now: datetime,
        available_at: datetime | None = None,
    ) -> None:
        """Give the job back, claimable at ``available_at`` or immediately."""
        del now
        with self._lock:
            entry = self._owned(lease)
            entry.lease = None
            entry.available_at = available_at

    async def close(
        self,
        lease: Lease,
        *,
        now: datetime,
        failure: FailureReport | None = None,
    ) -> None:
        """Drop the lease on a job that has reached a terminal state."""
        del now
        with self._lock:
            entry = self._owned(lease)
            entry.lease = None
            entry.available_at = None
            entry.closed = True
            entry.failure = failure

    async def request_cancellation(self, job_id: JobId, *, now: datetime) -> bool:
        """Ask a job to stop at its next checkpoint."""
        del now
        with self._lock:
            entry = self._entry_for(job_id.value)
            if entry.closed:
                return False
            entry.cancel_requested = True
            return True

    # -- Recovery ------------------------------------------------------------

    async def reclaim_expired(self, *, now: datetime) -> Sequence[Lease]:
        """Take back every lease that has lapsed."""
        with self._lock:
            return self._take_back(
                lambda lease: lease.is_expired(now),
            )

    async def release_owned_by(self, worker: WorkerId, *, now: datetime) -> Sequence[Lease]:
        """Take back every lease held by ``worker``, expired or not."""
        del now
        with self._lock:
            return self._take_back(lambda lease: lease.is_held_by(worker))

    def _take_back(self, matches: Callable[[Lease], bool]) -> Sequence[Lease]:
        """Clear every lease the predicate selects and return them."""
        reclaimed: list[Lease] = []
        for entry in self._entries.values():
            lease = entry.lease
            if lease is None or not matches(lease):
                continue
            entry.lease = None
            entry.available_at = None
            reclaimed.append(lease)
        return tuple(reclaimed)

    # -- Diagnostics ---------------------------------------------------------

    async def state_of(self, job_id: JobId) -> ClaimedJob | None:
        """Return the queue's view of one job, or ``None`` if it has no state."""
        with self._lock:
            entry = self._entries.get(job_id.value)
            if entry is None or entry.lease is None:
                return None
            return ClaimedJob(
                lease=entry.lease,
                checkpoint=entry.checkpoint,
                cancel_requested=entry.cancel_requested,
            )

    def checkpoint_of(self, job_id: JobId) -> Checkpoint:
        """Return the durable checkpoint of a job. Test and diagnostic helper."""
        with self._lock:
            entry = self._entries.get(job_id.value)
            return Checkpoint() if entry is None else entry.checkpoint

    def progress_of(self, job_id: JobId) -> StageProgress | None:
        """Return the last observation recorded. Test and diagnostic helper."""
        with self._lock:
            entry = self._entries.get(job_id.value)
            return None if entry is None else entry.progress

    def failure_of(self, job_id: JobId) -> FailureReport | None:
        """Return why a job ended, when it ended badly. Diagnostic helper."""
        with self._lock:
            entry = self._entries.get(job_id.value)
            return None if entry is None else entry.failure

    # -- Internals -----------------------------------------------------------

    def _entry_for(self, job_uuid: UUID) -> _QueueEntry:
        """Return the entry for a job, creating an empty one on first sight.

        Entries are lazy on purpose: a freshly requested job needs no queue row
        to be claimable, so nothing has to be kept in step with the catalogue.
        """
        entry = self._entries.get(job_uuid)
        if entry is None:
            entry = _QueueEntry()
            self._entries[job_uuid] = entry
        return entry

    def _owned(self, lease: Lease) -> _QueueEntry:
        """Return the entry ``lease`` still owns, or refuse the write.

        This is the guarantee the whole design rests on: a worker that lost its
        lease cannot write to the job another worker is now running.
        """
        entry = self._entries.get(lease.job_id.value)
        current = None if entry is None else entry.lease
        if entry is None or current is None or current.owner != lease.owner:
            raise LeaseLostError(lease.job_id, None if current is None else current.owner)
        return entry
