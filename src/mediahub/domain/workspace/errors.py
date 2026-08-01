"""Failures the workspace can express.

Two of these are load-bearing for the retry taxonomy
(``docs/architecture/07-download-pipeline.md`` §7.7), because
:mod:`mediahub.application.download.failures` classifies by type:

* :class:`InsufficientDiskSpaceError` is **transient**. A small device fills up
  in normal use; something else finishes, a sweep runs, and the space returns.
* :class:`IntegrityCheckFailedError` is **permanent**. Re-downloading bytes that
  did not match their expected size or hash produces the same mismatch an hour
  later, so it must never be retried.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mediahub.domain.common.errors import DomainError, InvariantViolationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.domain.workspace.identifiers import LeaseId


class WorkspaceError(DomainError):
    """Base class for workspace failures."""

    code: ClassVar[str] = "workspace_error"


class InvalidArtifactNameError(InvariantViolationError):
    """A proposed artifact name is unsafe or unusable.

    Attributes:
        raw: The rejected name, truncated.
        reason: Why it was rejected.
    """

    code: ClassVar[str] = "invalid_artifact_name"

    MAX_ECHO_LENGTH: ClassVar[int] = 120

    def __init__(self, raw: str, reason: str) -> None:
        """Initialise the error from the rejected name and the reason."""
        echoed = raw[: self.MAX_ECHO_LENGTH]
        super().__init__(f"'{echoed}' is not a valid artifact name: {reason}")
        self.raw = echoed
        self.reason = reason


class PathEscapesWorkspaceError(WorkspaceError):
    """A path resolved outside the lease directory.

    Reaching this error means a containment check did its job: an external tool
    produced a path that would have written outside the space it was given.

    Attributes:
        path: The offending path, as text.
    """

    code: ClassVar[str] = "path_escapes_workspace"

    def __init__(self, path: str) -> None:
        """Initialise the error from the offending path."""
        super().__init__(f"'{path}' resolves outside its workspace lease.")
        self.path = path


class InsufficientDiskSpaceError(WorkspaceError):
    """There is not enough free space to reserve a lease.

    A first-class, expected outcome rather than an exception path: the device is
    small, and refusing work early is how the database survives.

    Attributes:
        requested_bytes: What the caller asked to reserve.
        available_bytes: What was actually free, minus the reserve floor.
    """

    code: ClassVar[str] = "insufficient_disk_space"

    def __init__(self, requested_bytes: int, available_bytes: int) -> None:
        """Initialise the error from the requested and available byte counts."""
        super().__init__(
            f"Cannot reserve {requested_bytes} bytes of workspace; "
            f"only {available_bytes} bytes are usable."
        )
        self.requested_bytes = requested_bytes
        self.available_bytes = available_bytes


class WorkspaceQuotaExceededError(WorkspaceError):
    """A configured workspace ceiling would be crossed.

    Distinct from :class:`InsufficientDiskSpaceError` on purpose: the device is
    not full, the *operator's* limit was reached. Retrying changes nothing,
    which is why this is a permanent failure and a full disk is not.

    Attributes:
        limit_bytes: The ceiling that applies.
        requested_bytes: What the caller asked for.
        scope: Which ceiling it was - ``lease`` or ``workspace``.
    """

    code: ClassVar[str] = "workspace_quota_exceeded"

    def __init__(self, limit_bytes: int, requested_bytes: int, *, scope: str = "lease") -> None:
        """Initialise the error from the ceiling, the request and its scope."""
        super().__init__(
            f"{requested_bytes} bytes exceeds the configured {scope} ceiling "
            f"of {limit_bytes} bytes."
        )
        self.limit_bytes = limit_bytes
        self.requested_bytes = requested_bytes
        self.scope = scope


class IntegrityCheckFailedError(WorkspaceError):
    """What was written is not what was expected.

    Raised before an artifact is ever published under its final name, so a
    caller cannot observe a file that failed verification
    (``docs/architecture/07-download-pipeline.md`` §7.12).

    Attributes:
        artifact: The artifact that failed.
        reason: Which property disagreed.
        expected: What was required.
        actual: What was found.
    """

    code: ClassVar[str] = "integrity_check_failed"

    def __init__(self, artifact: str, reason: str, *, expected: object, actual: object) -> None:
        """Initialise the error from the artifact and the disagreement."""
        super().__init__(
            f"'{artifact}' failed verification: {reason} (expected {expected}, got {actual})."
        )
        self.artifact = artifact
        self.reason = reason
        self.expected = expected
        self.actual = actual


class LeaseClosedError(WorkspaceError):
    """A lease was used after it was released.

    Every path inside a released lease has been deleted, so continuing to write
    would either fail obscurely or - worse - recreate a directory nothing owns
    and nothing will ever clean up.

    Attributes:
        lease_id: The lease that is no longer open, as text.
    """

    code: ClassVar[str] = "lease_closed"

    def __init__(self, lease_id: LeaseId | str) -> None:
        """Initialise the error from the released lease."""
        super().__init__(f"Lease '{lease_id}' has been released and can no longer be used.")
        self.lease_id = str(lease_id)


class WorkspaceInconsistentError(WorkspaceError):
    """A lease directory is not in a state the workspace can vouch for.

    Reaching this means something outside the adapter touched the lease: a
    symlink appeared, a directory turned up where only files belong, or the
    directory vanished while it was still leased.

    Attributes:
        lease_id: The lease that failed the check, as text.
        reason: What was wrong.
    """

    code: ClassVar[str] = "workspace_inconsistent"

    def __init__(self, lease_id: LeaseId | str, reason: str) -> None:
        """Initialise the error from the lease and what was found."""
        super().__init__(f"Lease '{lease_id}' is inconsistent: {reason}.")
        self.lease_id = str(lease_id)
        self.reason = reason
