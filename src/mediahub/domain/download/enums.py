"""Closed value sets used by the download aggregate."""

from __future__ import annotations

from enum import StrEnum


class FailureKind(StrEnum):
    """How a failure should be treated by whoever is orchestrating the work.

    Classification is the *adapter's* responsibility, because only the adapter
    can read a provider's error and know whether it means "geo-blocked forever"
    or "rate-limited, try in sixty seconds".

    Two rules govern its use:

    * A ``PERMANENT`` or ``POLICY`` failure is never retried. Retrying a DRM
      error three times with exponential backoff wastes an hour and teaches the
      user that the product is slow.
    * An *unknown* failure is never classified ``PERMANENT``. Unknown means
      ``TRANSIENT`` with a low attempt cap, so a novel error gets one honest
      retry and then a dead letter a human can read.

    Attributes:
        TRANSIENT: Might succeed later. Eligible for retry with backoff.
        PERMANENT: Will never succeed as requested. Terminal.
        POLICY: Refused by a rule, not by a failure. Terminal, and auditable.
        CANCELLED: Stopped on request. Not a failure at all.
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    POLICY = "policy"
    CANCELLED = "cancelled"

    @property
    def is_retryable(self) -> bool:
        """Return whether work that failed this way may be attempted again."""
        return self is FailureKind.TRANSIENT


class JobStatus(StrEnum):
    """Lifecycle state of a download job.

    Attributes:
        QUEUED: Accepted and waiting to be picked up.
        RUNNING: Actively being processed by a worker.
        SUCCEEDED: Finished successfully. Terminal.
        FAILED: Finished unsuccessfully; may be requeued if attempts remain.
        CANCELLED: Stopped on request. Terminal.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Return whether the job has reached an end state for good."""
        return self in {JobStatus.SUCCEEDED, JobStatus.CANCELLED}

    @property
    def is_active(self) -> bool:
        """Return whether the job still occupies queue or worker capacity."""
        return self in {JobStatus.QUEUED, JobStatus.RUNNING}


class JobPriority(StrEnum):
    """Relative scheduling priority.

    Stored as a string for readability in the database and on the wire, while
    :attr:`weight` gives the numeric ordering a scheduler needs (higher runs
    first).
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"

    @property
    def weight(self) -> int:
        """Return the numeric scheduling weight; higher wins."""
        return _PRIORITY_WEIGHTS[self]


_PRIORITY_WEIGHTS: dict[JobPriority, int] = {
    JobPriority.LOW: 10,
    JobPriority.NORMAL: 50,
    JobPriority.HIGH: 90,
}
