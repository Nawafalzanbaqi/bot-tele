"""Typed failure reports, and the one place a raw exception becomes one.

A worker must never report "something went wrong". Every ending is classified -
transient, permanent, policy or cancelled - because the classification *is* the
retry decision (``docs/architecture/07-download-pipeline.md`` §7.7).

Two rules from the pipeline document are encoded here and must not be softened:

* **A permanent or policy failure is never retried.** Retrying a DRM error three
  times with exponential backoff wastes an hour and teaches the user that the
  product is slow.
* **An unknown failure is never classified permanent.** Anything this module
  does not recognise becomes ``TRANSIENT`` with the honest code
  ``unknown_failure``, so a novel error gets one real retry and then a dead
  letter a human can read.

Classification lives here, in the application layer, rather than in the worker:
the worker's job is to execute and report, and "what does this exception mean"
is knowledge about the system, not about running a loop.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from mediahub.application.common.errors import ApplicationError
from mediahub.application.delivery.errors import DeliveryError
from mediahub.application.download.errors import DownloadError
from mediahub.domain.common.errors import DomainError
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.workspace.errors import InsufficientDiskSpaceError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.queue import JobStage

UNKNOWN_FAILURE_CODE: Final[str] = "unknown_failure"
"""Code carried by anything the taxonomy does not recognise."""

MAX_MESSAGE_LENGTH: Final[int] = 500
"""Failure messages are for triage, not for transcripts."""

DISK_RETRY_SECONDS: Final[float] = 300.0
"""A full disk will not free itself in thirty seconds.

Retrying a disk failure on the normal exponential curve wastes several wakeups
on a device that is asleep most of the time, so the rule in
``docs/architecture/09-queue-architecture.md`` §9.5 is a long flat delay
instead."""


@dataclass(frozen=True, slots=True)
class FailureReport:
    """Why a job stopped, in a form a caller can act on without parsing prose.

    Attributes:
        kind: How the failure should be treated.
        code: Stable, machine-readable identifier. Clients branch on this.
        message: Human-readable summary, truncated to a triage-sized length.
        stage: Which stage was running, when one was.
        retry_after_seconds: Delay the other side explicitly asked for. When
            present it outranks any calculated backoff - guessing when a
            provider has told you the answer is self-inflicted damage.
    """

    kind: FailureKind
    code: str
    message: str
    stage: JobStage | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        """Normalise the message so a stack trace cannot become a field."""
        cleaned = " ".join(self.message.split())[:MAX_MESSAGE_LENGTH]
        object.__setattr__(self, "message", cleaned or "unknown error")

    @property
    def is_retryable(self) -> bool:
        """Return whether the work may be attempted again."""
        return self.kind.is_retryable

    @property
    def is_cancellation(self) -> bool:
        """Return whether this "failure" is really a cancellation."""
        return self.kind is FailureKind.CANCELLED

    def at_stage(self, stage: JobStage) -> FailureReport:
        """Return the same report, attributed to ``stage``."""
        return replace(self, stage=stage)


def classify(error: BaseException, *, stage: JobStage | None = None) -> FailureReport:
    """Turn any exception into a typed report.

    Args:
        error: What was raised.
        stage: The stage that raised it, when known.

    Returns:
        A report whose ``kind`` decides the caller's retry behaviour. Errors
        that already classify themselves - every
        :class:`~mediahub.application.download.errors.DownloadError` and
        :class:`~mediahub.application.delivery.errors.DeliveryError` - are
        trusted, because only the adapter that raised one can know what a
        provider's error meant.
    """
    if isinstance(error, DownloadError | DeliveryError):
        return FailureReport(
            kind=error.kind,
            code=error.code,
            message=error.message,
            stage=stage,
            retry_after_seconds=error.retry_after_seconds,
        )
    if isinstance(error, InsufficientDiskSpaceError):
        # Expected on a small device, and temporary: something else finishes,
        # a sweep runs, and the space comes back.
        return FailureReport(
            kind=FailureKind.TRANSIENT,
            code=error.code,
            message=error.message,
            stage=stage,
            retry_after_seconds=DISK_RETRY_SECONDS,
        )
    if isinstance(error, DomainError | ApplicationError):
        # A rule was broken or an orchestration precondition failed. Neither
        # improves by being attempted again with the same inputs.
        return FailureReport(
            kind=FailureKind.PERMANENT,
            code=error.code,
            message=error.message,
            stage=stage,
        )
    return FailureReport(
        kind=FailureKind.TRANSIENT,
        code=UNKNOWN_FAILURE_CODE,
        message=f"{type(error).__name__}: {error}",
        stage=stage,
    )
