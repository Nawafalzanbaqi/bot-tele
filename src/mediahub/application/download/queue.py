"""The queue and lease contract a worker executes against.

This module defines *what* a worker needs from the queue, and nothing about how
the queue is stored. It is the seam described in
``docs/architecture/09-queue-architecture.md`` §9.3: the aggregate carries the
business fields, and these are the **scheduling mechanics** - lease ownership,
checkpoints, cancellation and live progress.

Four ideas carry the whole design:

* **The lease is the only crash-detection primitive.** A worker that dies stops
  extending its lease; when the lease expires the job becomes claimable again.
  Nothing else - no liveness ping, no registry, no lock service.
* **A checkpoint is a set of completed stages.** Resuming is therefore set
  arithmetic (:meth:`Checkpoint.remaining`) rather than a stored cursor that can
  disagree with reality.
* **Cancellation is read on the heartbeat's round trip.** One call extends the
  lease *and* answers "should I stop?", so there is no second polling loop to
  forget (``docs/architecture/10-worker-architecture.md`` §10.2).
* **The queue never decides policy.** It does not know what a retry budget is,
  when a job has failed for the last time, or what a stage does. Those are
  rules, and rules live in the domain and the use cases above this port.

Every write is guarded by lease ownership: a worker whose lease has been
reclaimed cannot corrupt a job another worker is now running.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from mediahub.application.download.errors import (
    InvalidCheckpointError,
    InvalidLeaseError,
    InvalidWorkerIdentityError,
)
from mediahub.domain.common.time import ensure_utc
from mediahub.domain.download.errors import InvalidProgressError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from datetime import datetime

    from mediahub.application.download.failures import FailureReport
    from mediahub.domain.download.value_objects import JobId

PERCENT: Final[int] = 100

_IDENTITY_SEPARATOR: Final[str] = ":"


# --------------------------------------------------------------------------- #
# Stages                                                                       #
# --------------------------------------------------------------------------- #


class JobStage(StrEnum):
    """One resumable step of a job.

    Stages exist so that a crash costs one stage rather than a whole job, and so
    that an operator reading a status sees "verifying" rather than "running"
    (``docs/architecture/08-state-machine.md`` §8.1).

    Attributes:
        PROBE: Read what the source is, without spending bandwidth.
        DOWNLOAD: Transfer the bytes into the workspace lease.
        VERIFY: Prove the bytes are what was asked for.
        DELIVER: Hand the artifact to its destination and take a receipt.
        CLEANUP: Release everything the job reserved.
    """

    PROBE = "probe"
    DOWNLOAD = "download"
    VERIFY = "verify"
    DELIVER = "deliver"
    CLEANUP = "cleanup"


# --------------------------------------------------------------------------- #
# Identity and leases                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WorkerId:
    """Stable identity of one worker process.

    Deliberately **not** random. A restarted worker keeps its identity, which is
    what lets it release its own stale leases at startup instead of waiting a
    full lease period to recover work it was already doing
    (``docs/architecture/10-worker-architecture.md`` §10.5).

    Attributes:
        host: The machine the process runs on.
        role: What the process is, e.g. ``worker``.
        index: Which slot of that role, for several processes on one host.
    """

    host: str
    role: str = "worker"
    index: int = 0

    def __post_init__(self) -> None:
        """Reject an identity that could not be told apart from another."""
        for label, value in (("host", self.host), ("role", self.role)):
            if not value.strip():
                message = f"worker {label} must not be blank"
                raise InvalidWorkerIdentityError(message)
            if _IDENTITY_SEPARATOR in value:
                message = f"worker {label} must not contain '{_IDENTITY_SEPARATOR}'"
                raise InvalidWorkerIdentityError(message)
        if self.index < 0:
            message = f"worker index must be >= 0, got {self.index}"
            raise InvalidWorkerIdentityError(message)

    def __str__(self) -> str:
        """Return the canonical ``host:role:index`` form."""
        return _IDENTITY_SEPARATOR.join((self.host, self.role, str(self.index)))


@dataclass(frozen=True, slots=True)
class Lease:
    """Exclusive, time-boxed ownership of one job.

    A lease is the only thing that makes a running job safe: while it is held
    and unexpired, exactly one worker may write to the job; once it expires, the
    job belongs to whoever claims it next. There is no "unlock" message to lose.

    Attributes:
        job_id: The job this lease covers.
        owner: The worker holding it.
        acquired_at: When it was taken (UTC).
        expires_at: When it stops being valid (UTC).
    """

    job_id: JobId
    owner: WorkerId
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        """Reject a lease that has no life in it."""
        object.__setattr__(
            self, "acquired_at", ensure_utc(self.acquired_at, field_name="acquired_at")
        )
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at, field_name="expires_at"))
        if self.expires_at <= self.acquired_at:
            message = "a lease must expire after it was acquired"
            raise InvalidLeaseError(message)

    def is_expired(self, now: datetime) -> bool:
        """Return whether the lease has lapsed at ``now``."""
        return now >= self.expires_at

    def is_held_by(self, worker: WorkerId) -> bool:
        """Return whether ``worker`` is the owner of this lease."""
        return self.owner == worker

    def remaining_seconds(self, now: datetime) -> float:
        """Return how long the lease is still valid for, never negative."""
        return max(0.0, (self.expires_at - now).total_seconds())

    def extended_to(self, expires_at: datetime) -> Lease:
        """Return the same lease with a later expiry.

        Renewing to the instant it already expires is a no-op rather than an
        error: a heartbeat is idempotent, and a clock that has not visibly moved
        is not a reason to fail a healthy job. Moving an expiry *backwards*
        would shorten ownership without the owner knowing, and is refused.

        Args:
            expires_at: The new expiry (UTC).

        Returns:
            A new lease; leases are values and are never mutated.

        Raises:
            InvalidLeaseError: If the new expiry is earlier than the current one.
        """
        if expires_at < self.expires_at:
            message = "extending a lease must not move its expiry backwards"
            raise InvalidLeaseError(message)
        return replace(self, expires_at=expires_at)


# --------------------------------------------------------------------------- #
# Checkpoints                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """What a job has already finished, durably.

    Recorded after every completed stage, which is what turns a power cut into
    "resume from verify" instead of "download it all again"
    (``docs/architecture/07-download-pipeline.md``).

    Attributes:
        completed_stages: Stages that finished, in the order they finished.
        resume_token: Opaque state a stage may need to continue where it left
            off. Never interpreted here - only the stage that wrote it knows
            what it means.
        updated_at: When the checkpoint was last written (UTC).
    """

    completed_stages: tuple[JobStage, ...] = ()
    resume_token: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        """Reject a checkpoint claiming to have finished a stage twice."""
        if len(set(self.completed_stages)) != len(self.completed_stages):
            message = f"a stage may only be completed once, got {list(self.completed_stages)}"
            raise InvalidCheckpointError(message)
        if self.updated_at is not None:
            object.__setattr__(
                self, "updated_at", ensure_utc(self.updated_at, field_name="updated_at")
            )

    @classmethod
    def empty(cls) -> Checkpoint:
        """Return the checkpoint of a job that has done nothing yet."""
        return cls()

    @property
    def last_completed(self) -> JobStage | None:
        """Return the most recently completed stage, if any."""
        return self.completed_stages[-1] if self.completed_stages else None

    @property
    def is_fresh(self) -> bool:
        """Return whether no stage has completed yet."""
        return not self.completed_stages

    def has_completed(self, stage: JobStage) -> bool:
        """Return whether ``stage`` is already done."""
        return stage in self.completed_stages

    def remaining(self, plan: Sequence[JobStage]) -> tuple[JobStage, ...]:
        """Return the stages of ``plan`` that still have to run, in order.

        This is the whole of "resume from the last checkpoint": a set
        difference, evaluated against the plan rather than against a stored
        position that could disagree with it.
        """
        return tuple(stage for stage in plan if not self.has_completed(stage))

    def with_stage(
        self,
        stage: JobStage,
        *,
        at: datetime,
        resume_token: str | None = None,
    ) -> Checkpoint:
        """Return this checkpoint plus one completed stage.

        Args:
            stage: The stage that just finished.
            at: When it finished (UTC).
            resume_token: Replacement resume state, or ``None`` to keep the
                current one.

        Returns:
            A new checkpoint. Completing a stage twice is a no-op rather than an
            error, because a stage re-run after a reclaim is normal.
        """
        if self.has_completed(stage):
            return replace(self, updated_at=at, resume_token=resume_token or self.resume_token)
        return Checkpoint(
            completed_stages=(*self.completed_stages, stage),
            resume_token=resume_token if resume_token is not None else self.resume_token,
            updated_at=at,
        )

    def with_resume_token(self, resume_token: str | None, *, at: datetime) -> Checkpoint:
        """Return this checkpoint carrying new resume state."""
        return replace(self, resume_token=resume_token, updated_at=at)


# --------------------------------------------------------------------------- #
# Progress                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StageProgress:
    """One observation of a job in flight.

    Everything an operator or a chat message wants to show: where the job is,
    how far it has got, how fast it is going and when it will finish. It is
    telemetry, not state - losing an update costs nothing.

    Attributes:
        stage: Which stage is running.
        transferred_bytes: Bytes moved so far in this stage.
        total_bytes: Expected total, when the source announced one.
        speed_bps: Observed rate in bytes per second, when known.
        eta_seconds: Estimated seconds remaining, when known.
        observed_at: When the observation was taken (UTC).
    """

    stage: JobStage
    transferred_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_seconds: float | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Reject byte counts and rates that cannot exist."""
        if self.transferred_bytes < 0:
            message = f"Transferred bytes must be >= 0, got {self.transferred_bytes}."
            raise InvalidProgressError(message)
        if self.total_bytes is not None and self.total_bytes < 0:
            message = f"Total bytes must be >= 0, got {self.total_bytes}."
            raise InvalidProgressError(message)
        for label, value in (("speed_bps", self.speed_bps), ("eta_seconds", self.eta_seconds)):
            if value is not None and value < 0:
                message = f"{label} must be >= 0, got {value}."
                raise InvalidProgressError(message)
        if self.observed_at is not None:
            object.__setattr__(
                self, "observed_at", ensure_utc(self.observed_at, field_name="observed_at")
            )

    @classmethod
    def starting(cls, stage: JobStage, *, at: datetime | None = None) -> StageProgress:
        """Return the zero observation recorded when a stage begins."""
        return cls(stage=stage, observed_at=at)

    @property
    def percentage(self) -> float | None:
        """Return completion in percent, or ``None`` when the total is unknown.

        Clamped at 100: a source that under-declares its size must not produce a
        progress bar reading 143%.
        """
        if not self.total_bytes:
            return None
        ratio = min(self.transferred_bytes / self.total_bytes, 1.0)
        return round(ratio * PERCENT, 2)


# --------------------------------------------------------------------------- #
# Claims                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """One job, leased to one worker, with everything needed to resume it.

    Attributes:
        lease: Proof of ownership, and the deadline for keeping it.
        checkpoint: What the job has already finished.
        attempt: Which attempt this is. Stamped by the claim use case from the
            aggregate, because the attempt budget is a business rule and the
            queue does not own it.
        cancel_requested: Whether someone has asked for the job to stop.
    """

    lease: Lease
    checkpoint: Checkpoint = Checkpoint()
    attempt: int = 0
    cancel_requested: bool = False

    @property
    def job_id(self) -> JobId:
        """Return the job this claim covers."""
        return self.lease.job_id

    @property
    def owner(self) -> WorkerId:
        """Return the worker that holds the claim."""
        return self.lease.owner

    def with_attempt(self, attempt: int) -> ClaimedJob:
        """Return the claim with the attempt number the aggregate reports."""
        return replace(self, attempt=attempt)

    def with_lease(self, lease: Lease) -> ClaimedJob:
        """Return the claim carrying a renewed lease."""
        return replace(self, lease=lease)

    def with_checkpoint(self, checkpoint: Checkpoint) -> ClaimedJob:
        """Return the claim carrying a newer checkpoint."""
        return replace(self, checkpoint=checkpoint)


@dataclass(frozen=True, slots=True)
class LeaseState:
    """The answer to a heartbeat: how long we still own it, and should we stop.

    Attributes:
        lease: The renewed lease.
        cancel_requested: Whether cancellation has been requested since the last
            round trip.
    """

    lease: Lease
    cancel_requested: bool = False


# --------------------------------------------------------------------------- #
# The port                                                                     #
# --------------------------------------------------------------------------- #


class JobQueue(Protocol):
    """Atomic hand-off of work from producers to workers.

    Implementations must guarantee:

    * **Exactly one winner.** Two workers claiming concurrently cannot both
      receive the same job. On SQLite this is a single conditional ``UPDATE``;
      the loser sees nothing and backs off
      (``docs/architecture/09-queue-architecture.md`` §9.4).
    * **Ownership on every write.** ``extend_lease``, ``save_checkpoint``,
      ``record_progress``, ``release`` and ``close`` raise
      :class:`~mediahub.application.download.errors.LeaseLostError` when the
      caller no longer holds the lease. This is what stops a worker that stalled
      past its lease from writing over the worker that took over.
    * **No policy.** The queue never decides whether to retry, how long to back
      off, or what a stage means. It is told.
    """

    async def claim(
        self,
        *,
        worker: WorkerId,
        lease_seconds: float,
        now: datetime,
    ) -> ClaimedJob | None:
        """Take the highest-priority due job, or return ``None`` if none is due.

        Args:
            worker: Who is claiming.
            lease_seconds: How long the lease should last.
            now: Current time.

        Returns:
            The claim, including any checkpoint left by a previous attempt, or
            ``None`` when the queue has nothing claimable.
        """
        ...

    async def extend_lease(
        self,
        lease: Lease,
        *,
        lease_seconds: float,
        now: datetime,
    ) -> LeaseState:
        """Renew the lease and report whether cancellation was requested.

        Deliberately one call: the heartbeat is the cancellation poll.

        Raises:
            LeaseLostError: If the caller no longer owns the job.
        """
        ...

    async def save_checkpoint(self, lease: Lease, checkpoint: Checkpoint) -> None:
        """Persist what the job has completed so far.

        Raises:
            LeaseLostError: If the caller no longer owns the job.
        """
        ...

    async def record_progress(self, lease: Lease, progress: StageProgress) -> None:
        """Record the latest observation of the job.

        Callers throttle; implementations must not assume they do.

        Raises:
            LeaseLostError: If the caller no longer owns the job.
        """
        ...

    async def release(
        self,
        lease: Lease,
        *,
        now: datetime,
        available_at: datetime | None = None,
    ) -> None:
        """Give the job back so it can be claimed again.

        Used for a graceful drain and for a retryable failure. ``available_at``
        gates when it may be claimed next; ``None`` means immediately.

        Raises:
            LeaseLostError: If the caller no longer owns the job.
        """
        ...

    async def close(
        self,
        lease: Lease,
        *,
        now: datetime,
        failure: FailureReport | None = None,
    ) -> None:
        """Drop the lease on a job that has reached a terminal state.

        Args:
            lease: The lease being surrendered.
            now: Current time.
            failure: Why the job ended, when it ended badly. Kept so triage does
                not have to open a payload
                (``docs/architecture/09-queue-architecture.md`` §9.3).

        Raises:
            LeaseLostError: If the caller no longer owns the job.
        """
        ...

    async def request_cancellation(self, job_id: JobId, *, now: datetime) -> bool:
        """Ask a running job to stop at its next checkpoint.

        Cooperative by design: there is no forced kill, because terminating a
        worker mid-write risks partial files and orphaned subprocesses
        (``docs/architecture/09-queue-architecture.md`` §9.7).

        Returns:
            Whether the request was recorded.
        """
        ...

    async def reclaim_expired(self, *, now: datetime) -> Sequence[Lease]:
        """Take back every lease that has lapsed, and report which.

        The single mechanism behind crash recovery: a worker that died stopped
        renewing, so its jobs become claimable again without anyone noticing it
        died.
        """
        ...

    async def release_owned_by(self, worker: WorkerId, *, now: datetime) -> Sequence[Lease]:
        """Take back every lease held by ``worker``, expired or not.

        Called at startup by a worker with the same identity as one that died:
        waiting a full lease period to recover its own work would be a needless
        two-minute stall on every restart.
        """
        ...

    async def state_of(self, job_id: JobId) -> ClaimedJob | None:
        """Return the queue's view of one job, for diagnostics and tests."""
        ...
