"""Failures the download aggregate can express."""

from __future__ import annotations

from typing import ClassVar

from mediahub.domain.common.errors import (
    ConflictError,
    EntityNotFoundError,
    InvalidStateTransitionError,
    InvariantViolationError,
)


class InvalidJobIdError(InvariantViolationError):
    """The supplied string is not a valid job identifier."""

    code: ClassVar[str] = "invalid_job_id"

    def __init__(self, raw: str) -> None:
        """Initialise the error from the rejected raw value."""
        super().__init__(f"'{raw}' is not a valid download job identifier (expected a UUID).")
        self.raw = raw


class InvalidProgressError(InvariantViolationError):
    """Reported progress is negative or exceeds the announced total."""

    code: ClassVar[str] = "invalid_progress"


class InvalidRetryPolicyError(InvariantViolationError):
    """The retry policy asks for an impossible number of attempts or delay."""

    code: ClassVar[str] = "invalid_retry_policy"


class DownloadJobNotFoundError(EntityNotFoundError):
    """No download job exists for the requested identifier."""

    code: ClassVar[str] = "download_job_not_found"

    def __init__(self, identifier: object) -> None:
        """Initialise the error from the identifier that was looked up."""
        super().__init__("Download job", identifier)


class DuplicateActiveJobError(ConflictError):
    """An active job already exists for this media item.

    Guards against queueing the same acquisition twice, which would race two
    writers onto one storage key.
    """

    code: ClassVar[str] = "duplicate_active_job"

    def __init__(self, media_id: object) -> None:
        """Initialise the error from the media identifier already in flight."""
        super().__init__(f"An active download job already exists for media '{media_id}'.")
        self.media_id = media_id


class InvalidJobTransitionError(InvalidStateTransitionError):
    """The requested lifecycle change is not allowed from the current state."""

    code: ClassVar[str] = "invalid_job_transition"

    def __init__(self, current: object, target: object) -> None:
        """Initialise the error from the current and requested states."""
        super().__init__("Download job", current, target)


class JobNotRetryableError(ConflictError):
    """The job has exhausted its retry budget.

    Attributes:
        attempts: How many attempts were consumed.
        max_attempts: The budget defined by the job's retry policy.
    """

    code: ClassVar[str] = "job_not_retryable"

    def __init__(self, attempts: int, max_attempts: int) -> None:
        """Initialise the error from the consumed and allowed attempt counts."""
        super().__init__(
            f"Download job exhausted its retry budget ({attempts}/{max_attempts} attempts)."
        )
        self.attempts = attempts
        self.max_attempts = max_attempts
