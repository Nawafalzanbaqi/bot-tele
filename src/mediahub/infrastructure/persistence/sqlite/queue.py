"""Durable job queue over the SQLite system of record.

Implements :class:`~mediahub.application.download.queue.JobQueue` against the
same database the repositories commit into, which is what makes enqueueing
transactional: a job becomes claimable the instant the transaction that created
it commits, with no outbox and no second store to fall out of step
(``docs/architecture/09-queue-architecture.md`` §9.1).

Four decisions carry the correctness of this module.

**The claim is one statement.** Not ``SELECT`` a candidate and then ``UPDATE``
it. Two workers running that pair can both read the same row before either
writes, and both would win. Worse, on WAL the second statement of a transaction
that began by reading cannot always take the write lock: SQLite answers
``SQLITE_BUSY_SNAPSHOT`` and - unlike an ordinary lock conflict - **does not
invoke the busy handler**, because the snapshot the reader saw is already stale.
``busy_timeout`` cannot help. So the claim is a single conditional ``INSERT ...
SELECT ... ON CONFLICT DO UPDATE ... RETURNING``: SQLite takes the write lock
before the statement reads anything, the loser of a race sees no work rather
than duplicate work, and there is no window to lose an update in.

**The queue row is optional.** A job that has never been claimed has no row
here, which reads as "claimable, nothing completed". Requesting a download
therefore writes one table, not two, and nothing has to keep the queue in step
with the catalogue. That is why the claim is an upsert rather than an update.

**Ownership is a predicate, not a check.** Every write carries ``AND
lease_owner = :owner`` in its ``WHERE``. There is no read-then-verify, so
there is no moment between verifying and writing for the lease to be lost in.
Nothing updated means the lease is gone, and the caller is told so.

**Taking a lease back reads before it writes, deliberately.** SQLite's
``RETURNING`` on an ``UPDATE`` reports the row *after* the update, so a
statement that clears a lease returns the nulls it just wrote - not the lease it
took back, which is what the caller needs. The read therefore happens in its own
transaction and each row is then cleared by a compare-and-swap on the exact
lease that was read. A lease renewed in the gap simply does not match and is
left alone, so the report is of what was actually reclaimed rather than what was
intended.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast

from sqlalchemy import Boolean, String, bindparam, literal, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediahub.application.download.errors import LeaseLostError
from mediahub.application.download.queue import (
    Checkpoint,
    ClaimedJob,
    JobStage,
    Lease,
    LeaseState,
    WorkerId,
)
from mediahub.domain.download.enums import JobStatus
from mediahub.domain.download.value_objects import JobId
from mediahub.infrastructure.persistence.sqlalchemy.models import (
    DownloadJobModel,
    JobQueueModel,
)
from mediahub.infrastructure.persistence.sqlite.types import UtcDateTime

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy import ColumnElement, CursorResult, Row, Table
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from mediahub.application.download.failures import FailureReport
    from mediahub.application.download.queue import StageProgress

STAGE_SEPARATOR: Final[str] = ","
"""Joins completed stages into one column.

Stage values are lowercase identifiers containing no commas, so the column stays
readable from the ``sqlite3`` shell - which on a headless device is the triage
tool that is actually installed."""


# --------------------------------------------------------------------------- #
# The claim statement                                                          #
# --------------------------------------------------------------------------- #


def _claim_statement() -> Any:
    """Build the single statement that atomically takes the next due job.

    Kept as one construct so the ordering rule, the due rule and the write live
    together: a candidate that this ``SELECT`` would not return can never be
    claimed, because there is no second statement that could pick a different
    one.
    """
    owner = bindparam("owner", type_=String)
    now = bindparam("now", type_=UtcDateTime)
    expires = bindparam("expires", type_=UtcDateTime)

    candidate = (
        select(
            DownloadJobModel.id,
            owner,
            now,
            expires,
            # A Python-side column default is applied to `INSERT ... VALUES`,
            # not to `INSERT ... SELECT`. Without these two literals the
            # not-null columns would be handed NULL.
            literal(False, Boolean),
            literal(False, Boolean),
        )
        .select_from(
            DownloadJobModel.__table__.outerjoin(
                JobQueueModel.__table__,
                JobQueueModel.job_id == DownloadJobModel.id,
            )
        )
        .where(
            DownloadJobModel.status == JobStatus.QUEUED.value,
            # No row at all means never claimed, which is claimable.
            JobQueueModel.job_id.is_(None)
            | (
                JobQueueModel.closed.is_(False)
                & JobQueueModel.cancel_requested.is_(False)
                # Unleased, or leased to someone whose lease has run out.
                & (JobQueueModel.lease_owner.is_(None) | (JobQueueModel.lease_expires_at <= now))
                # Past any backoff a previous failure asked for.
                & (JobQueueModel.available_at.is_(None) | (JobQueueModel.available_at <= now))
            ),
        )
        # Highest priority first, then oldest first, then by id so that two jobs
        # created in the same tick still have one defined winner.
        .order_by(
            DownloadJobModel.priority_weight.desc(),
            DownloadJobModel.created_at,
            DownloadJobModel.id,
        )
        .limit(1)
    )

    # Built against the table rather than the mapped class: this is a Core
    # statement, and routing it through the ORM's bulk-insert path adds a
    # rewrite step that an `INSERT ... SELECT` has no use for.
    queue = cast("Table", JobQueueModel.__table__)
    insert = sqlite_insert(queue).from_select(
        [
            queue.c.job_id,
            queue.c.lease_owner,
            queue.c.lease_acquired_at,
            queue.c.lease_expires_at,
            queue.c.cancel_requested,
            queue.c.closed,
        ],
        candidate,
    )
    return insert.on_conflict_do_update(
        index_elements=[JobQueueModel.job_id],
        set_={
            "lease_owner": insert.excluded.lease_owner,
            "lease_acquired_at": insert.excluded.lease_acquired_at,
            "lease_expires_at": insert.excluded.lease_expires_at,
            # Claiming clears the backoff: the wait it described is over.
            "available_at": None,
        },
    ).returning(
        JobQueueModel.job_id,
        JobQueueModel.lease_acquired_at,
        JobQueueModel.lease_expires_at,
        JobQueueModel.completed_stages,
        JobQueueModel.resume_token,
        JobQueueModel.checkpoint_updated_at,
        JobQueueModel.cancel_requested,
    )


CLAIM: Final[Any] = _claim_statement()


# --------------------------------------------------------------------------- #
# The adapter                                                                  #
# --------------------------------------------------------------------------- #


class SqliteJobQueue:
    """Lease-bearing work queue that survives the process holding it."""

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the queue to the session factory of the system of record."""
        self._session_factory = session_factory

    # -- Claiming ------------------------------------------------------------

    async def claim(
        self,
        *,
        worker: WorkerId,
        lease_seconds: float,
        now: datetime,
    ) -> ClaimedJob | None:
        """Take the highest-priority due job, or return ``None``."""
        expires = now + timedelta(seconds=lease_seconds)
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(CLAIM, {"owner": str(worker), "now": now, "expires": expires})
            ).first()
        if row is None:
            return None
        return ClaimedJob(
            lease=Lease(
                job_id=JobId(row.job_id),
                owner=worker,
                acquired_at=row.lease_acquired_at,
                expires_at=row.lease_expires_at,
            ),
            checkpoint=_checkpoint_from(row),
            cancel_requested=bool(row.cancel_requested),
        )

    # -- Holding -------------------------------------------------------------

    async def extend_lease(
        self,
        lease: Lease,
        *,
        lease_seconds: float,
        now: datetime,
    ) -> LeaseState:
        """Renew the lease and report the cancellation flag in one round trip."""
        # Validated before it is stored: `extended_to` refuses to move an expiry
        # backwards, which would shorten ownership without the owner knowing.
        renewed = lease.extended_to(now + timedelta(seconds=lease_seconds))
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    update(JobQueueModel)
                    .where(*_owned_by(lease))
                    .values(lease_expires_at=renewed.expires_at)
                    .returning(JobQueueModel.cancel_requested)
                )
            ).first()
            if row is None:
                await _refuse(session, lease)
            cancel_requested = bool(row.cancel_requested)
        return LeaseState(lease=renewed, cancel_requested=cancel_requested)

    async def save_checkpoint(self, lease: Lease, checkpoint: Checkpoint) -> None:
        """Persist what the job has completed so far."""
        await self._guarded_write(
            lease,
            completed_stages=_encode_stages(checkpoint.completed_stages),
            resume_token=checkpoint.resume_token,
            checkpoint_updated_at=checkpoint.updated_at,
        )

    async def record_progress(self, lease: Lease, progress: StageProgress) -> None:
        """Record the latest observation of the job."""
        await self._guarded_write(
            lease,
            progress_stage=progress.stage.value,
            progress_transferred_bytes=progress.transferred_bytes,
            progress_total_bytes=progress.total_bytes,
            progress_speed_bps=progress.speed_bps,
            progress_eta_seconds=progress.eta_seconds,
            progress_observed_at=progress.observed_at,
        )

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
        await self._guarded_write(lease, available_at=available_at, **_UNLEASED)

    async def close(
        self,
        lease: Lease,
        *,
        now: datetime,
        failure: FailureReport | None = None,
    ) -> None:
        """Drop the lease on a job that has reached a terminal state.

        The failure is written to its own columns rather than into a payload,
        so triage on the device is a ``SELECT`` and not an exercise in decoding.
        """
        del now
        await self._guarded_write(
            lease,
            available_at=None,
            closed=True,
            failure_kind=None if failure is None else failure.kind.value,
            failure_code=None if failure is None else failure.code,
            failure_message=None if failure is None else failure.message,
            failure_stage=None if failure is None or failure.stage is None else failure.stage.value,
            failure_retry_after_seconds=None if failure is None else failure.retry_after_seconds,
            **_UNLEASED,
        )

    async def request_cancellation(self, job_id: JobId, *, now: datetime) -> bool:
        """Ask a job to stop at its next checkpoint.

        An upsert, because a job may be cancelled before it has ever been
        claimed and so before it has a row here. The ``WHERE`` on the conflict
        branch is what refuses to cancel something already finished: it makes
        the update a no-op, and nothing is returned.
        """
        del now
        statement = (
            sqlite_insert(JobQueueModel)
            .values(job_id=job_id.value, cancel_requested=True, closed=False)
            .on_conflict_do_update(
                index_elements=[JobQueueModel.job_id],
                set_={"cancel_requested": True},
                where=JobQueueModel.closed.is_(False),
            )
            .returning(JobQueueModel.job_id)
        )
        async with self._session_factory() as session, session.begin():
            return (await session.execute(statement)).first() is not None

    # -- Recovery ------------------------------------------------------------

    async def reclaim_expired(self, *, now: datetime) -> Sequence[Lease]:
        """Take back every lease that has lapsed."""
        return await self._take_back(JobQueueModel.lease_expires_at <= now)

    async def release_owned_by(self, worker: WorkerId, *, now: datetime) -> Sequence[Lease]:
        """Take back every lease held by ``worker``, expired or not."""
        del now
        return await self._take_back(JobQueueModel.lease_owner == str(worker))

    async def _take_back(self, predicate: ColumnElement[bool]) -> Sequence[Lease]:
        """Clear every lease the predicate selects and report which were cleared.

        See the module docstring for why this reads first and then writes,
        rather than using ``UPDATE ... RETURNING``.
        """
        held = (
            select(
                JobQueueModel.job_id,
                JobQueueModel.lease_owner,
                JobQueueModel.lease_acquired_at,
                JobQueueModel.lease_expires_at,
            )
            .where(JobQueueModel.lease_owner.is_not(None), predicate)
            .order_by(JobQueueModel.lease_acquired_at, JobQueueModel.job_id)
        )
        reclaimed: list[Lease] = []
        async with self._session_factory() as session:
            rows = (await session.execute(held)).all()
            # End the read transaction before opening a write one, so the write
            # never has to upgrade a stale snapshot.
            await session.rollback()
            for row in rows:
                # Compare-and-swap on the exact lease that was read: one renewed
                # in the gap does not match, and is left to its owner.
                changed = await _write(
                    session,
                    update(JobQueueModel)
                    .where(
                        JobQueueModel.job_id == row.job_id,
                        JobQueueModel.lease_owner == row.lease_owner,
                        JobQueueModel.lease_expires_at == row.lease_expires_at,
                    )
                    .values(available_at=None, **_UNLEASED),
                )
                if changed:
                    reclaimed.append(_lease_from(row))
            await session.commit()
        return tuple(reclaimed)

    # -- Diagnostics ---------------------------------------------------------

    async def state_of(self, job_id: JobId) -> ClaimedJob | None:
        """Return the queue's view of one job, or ``None`` if it is not leased."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(JobQueueModel).where(JobQueueModel.job_id == job_id.value)
                )
            ).scalar_one_or_none()
        if row is None or row.lease_owner is None:
            return None
        return ClaimedJob(
            lease=_lease_from(row),
            checkpoint=_checkpoint_from(row),
            cancel_requested=bool(row.cancel_requested),
        )

    # -- Internals -----------------------------------------------------------

    async def _guarded_write(self, lease: Lease, **values: object) -> None:
        """Apply ``values`` to the job, or refuse because the lease is gone."""
        async with self._session_factory() as session, session.begin():
            changed = await _write(
                session, update(JobQueueModel).where(*_owned_by(lease)).values(**values)
            )
            if not changed:
                await _refuse(session, lease)


_UNLEASED: Final[dict[str, None]] = {
    "lease_owner": None,
    "lease_acquired_at": None,
    "lease_expires_at": None,
}
"""The three columns that together mean "nobody holds this"."""


def _owned_by(lease: Lease) -> tuple[ColumnElement[bool], ...]:
    """Return the predicate that admits only the current lease holder.

    The guarantee the whole design rests on. Expressed as part of the write
    rather than as a check before it, so there is no instant in between during
    which the lease could be lost.
    """
    return (
        JobQueueModel.job_id == lease.job_id.value,
        JobQueueModel.lease_owner == str(lease.owner),
    )


async def _write(session: AsyncSession, statement: Any) -> int:
    """Execute a DML statement and return how many rows it changed."""
    # `execute` is typed as returning the read-oriented `Result`; a DML
    # statement actually returns a `CursorResult`, which carries the row count.
    result = cast("CursorResult[Any]", await session.execute(statement))
    return int(result.rowcount or 0)


async def _refuse(session: AsyncSession, lease: Lease) -> NoReturn:
    """Raise, naming whoever holds the job now.

    The current holder is read only to make the error legible; it has no part in
    the decision, which the failed ``UPDATE`` already made.
    """
    holder = (
        await session.execute(
            select(JobQueueModel.lease_owner).where(JobQueueModel.job_id == lease.job_id.value)
        )
    ).scalar_one_or_none()
    raise LeaseLostError(lease.job_id, holder)


def _lease_from(row: Any) -> Lease:
    """Rebuild a lease from a row that carries one."""
    return Lease(
        job_id=JobId(row.job_id),
        owner=_decode_worker(row.lease_owner),
        acquired_at=row.lease_acquired_at,
        expires_at=row.lease_expires_at,
    )


def _checkpoint_from(row: Row[Any] | JobQueueModel) -> Checkpoint:
    """Rebuild a checkpoint from the three columns that hold one."""
    return Checkpoint(
        completed_stages=_decode_stages(row.completed_stages),
        resume_token=row.resume_token,
        updated_at=row.checkpoint_updated_at,
    )


def _encode_stages(stages: Sequence[JobStage]) -> str | None:
    """Join completed stages for storage, or ``None`` when none are."""
    return STAGE_SEPARATOR.join(stage.value for stage in stages) or None


def _decode_stages(encoded: str | None) -> tuple[JobStage, ...]:
    """Split a stored stage list back into stages, in the order they finished."""
    if not encoded:
        return ()
    return tuple(JobStage(value) for value in encoded.split(STAGE_SEPARATOR))


def _decode_worker(encoded: str) -> WorkerId:
    """Rebuild a worker identity from its canonical ``host:role:index`` form."""
    host, role, index = encoded.split(":")
    return WorkerId(host=host, role=role, index=int(index))
