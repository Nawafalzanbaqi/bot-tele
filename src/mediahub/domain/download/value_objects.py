"""Immutable values used by the download aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID

from mediahub.domain.common.value_object import ValueObject
from mediahub.domain.download.errors import (
    InvalidJobIdError,
    InvalidProgressError,
    InvalidRetryPolicyError,
)

_PERCENT: int = 100


@dataclass(frozen=True, slots=True)
class JobId(ValueObject):
    """Stable identifier of a download job."""

    value: UUID

    @classmethod
    def parse(cls, raw: str) -> JobId:
        """Build an identifier from its canonical string form.

        Args:
            raw: A UUID in any form :class:`uuid.UUID` accepts.

        Returns:
            The parsed identifier.

        Raises:
            InvalidJobIdError: If ``raw`` is not a valid UUID.
        """
        try:
            return cls(UUID(raw))
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidJobIdError(str(raw)) from exc

    def __str__(self) -> str:
        """Return the canonical string form of the identifier."""
        return str(self.value)


@dataclass(frozen=True, slots=True)
class DownloadProgress(ValueObject):
    """How much of a transfer has completed.

    ``total_bytes`` is optional because many sources do not announce a length
    up front; percentage is therefore ``None`` until a total is known.

    Attributes:
        downloaded_bytes: Bytes transferred so far.
        total_bytes: Announced total, when the source provides one.
    """

    downloaded_bytes: int = 0
    total_bytes: int | None = None

    def __post_init__(self) -> None:
        """Reject impossible byte counts."""
        if self.downloaded_bytes < 0:
            message = f"Downloaded bytes must be >= 0, got {self.downloaded_bytes}."
            raise InvalidProgressError(message)
        if self.total_bytes is None:
            return
        if self.total_bytes < 0:
            message = f"Total bytes must be >= 0, got {self.total_bytes}."
            raise InvalidProgressError(message)
        if self.downloaded_bytes > self.total_bytes:
            message = (
                f"Downloaded bytes ({self.downloaded_bytes}) cannot exceed "
                f"total bytes ({self.total_bytes})."
            )
            raise InvalidProgressError(message)

    @classmethod
    def none(cls) -> DownloadProgress:
        """Return the zero-progress value used when a job is created."""
        return cls()

    @property
    def percentage(self) -> float | None:
        """Return completion in percent, or ``None`` if the total is unknown."""
        if not self.total_bytes:
            return None
        return round(self.downloaded_bytes / self.total_bytes * _PERCENT, 2)

    @property
    def is_complete(self) -> bool:
        """Return whether every announced byte has been transferred."""
        return self.total_bytes is not None and self.downloaded_bytes == self.total_bytes


@dataclass(frozen=True, slots=True)
class RetryPolicy(ValueObject):
    """How often and how patiently a failed job may be retried.

    The policy is stored on the job so that changing the global default never
    silently rewrites the contract of jobs already in flight.

    Attributes:
        max_attempts: Total attempts allowed, including the first one.
        backoff_seconds: Base delay a scheduler should apply between attempts.
    """

    max_attempts: int = 3
    backoff_seconds: int = 30

    MAX_ALLOWED_ATTEMPTS: ClassVar[int] = 10
    MAX_BACKOFF_SECONDS: ClassVar[int] = 3600

    def __post_init__(self) -> None:
        """Validate the policy against the bounds the system will honour."""
        if not 1 <= self.max_attempts <= self.MAX_ALLOWED_ATTEMPTS:
            message = (
                f"max_attempts must be between 1 and {self.MAX_ALLOWED_ATTEMPTS}, "
                f"got {self.max_attempts}."
            )
            raise InvalidRetryPolicyError(message)
        if not 0 <= self.backoff_seconds <= self.MAX_BACKOFF_SECONDS:
            message = (
                f"backoff_seconds must be between 0 and {self.MAX_BACKOFF_SECONDS}, "
                f"got {self.backoff_seconds}."
            )
            raise InvalidRetryPolicyError(message)

    @classmethod
    def default(cls) -> RetryPolicy:
        """Return the policy applied when a caller does not specify one."""
        return cls()

    def delay_for(self, attempt: int) -> int:
        """Return the delay in seconds before ``attempt`` (1-based).

        Uses exponential backoff: ``backoff * 2 ** (attempt - 1)``. The first
        attempt is never delayed.
        """
        if attempt <= 1:
            return 0
        # `attempt` is at least 2 here, so the exponent is never negative.
        return self.backoff_seconds * int(2 ** (attempt - 2))
