"""The values the workspace reasons about.

Three of them, each replacing arithmetic that would otherwise be repeated in
every caller:

* :class:`LeaseOwner` answers "whose lease is this?" after a restart, which is
  the whole basis of crash recovery.
* :class:`DiskBudget` answers "how much may I hand out?" - free space minus
  outstanding reservations minus the emergency floor, never raw free space.
* :class:`IntegrityExpectation` answers "are these the bytes I asked for?", so
  that verification is one rule rather than one ``if`` per adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mediahub.domain.common.errors import InvariantViolationError
from mediahub.domain.common.value_object import ValueObject
from mediahub.domain.workspace.errors import IntegrityCheckFailedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.domain.common.fingerprint import Fingerprint


@dataclass(frozen=True, slots=True)
class LeaseOwner(ValueObject):
    """Which process a lease belongs to.

    The identity is deliberately **stable across restarts** and the process id
    deliberately is not. That pairing is what makes recovery decidable: same
    identity with a different process id means "the previous incarnation of me,
    which is gone", while a different identity means "somebody else, possibly
    still running" (``docs/architecture/10-worker-architecture.md`` §10.5).

    One identity therefore describes one process at a time. Two processes
    sharing a workspace root **must** differ in identity, or each will treat the
    other's live leases as its own wreckage.

    Attributes:
        identity: Stable name of the owning process, e.g. ``pi:worker:0``.
        process_id: The operating-system process id of this incarnation.
    """

    identity: str
    process_id: int

    def __post_init__(self) -> None:
        """Refuse an owner that could not be told apart or safely recorded."""
        identity = (self.identity or "").strip()
        if not identity:
            message = "A lease owner identity must not be blank."
            raise InvariantViolationError(message)
        if any(character in identity for character in "\r\n\x00"):
            message = "A lease owner identity must not contain control characters."
            raise InvariantViolationError(message)
        if self.process_id < 0:
            message = f"A process id must not be negative, got {self.process_id}."
            raise InvariantViolationError(message)
        object.__setattr__(self, "identity", identity)

    def is_same_identity(self, other: LeaseOwner) -> bool:
        """Return whether both owners name the same logical process."""
        return self.identity == other.identity

    def is_same_process(self, other: LeaseOwner) -> bool:
        """Return whether both owners are the same *running* process."""
        return self.is_same_identity(other) and self.process_id == other.process_id

    def __str__(self) -> str:
        """Return the ``identity#pid`` form used in logs and manifests."""
        return f"{self.identity}#{self.process_id}"


@dataclass(frozen=True, slots=True)
class DiskBudget(ValueObject):
    """What the device can still be asked for.

    ``headroom`` is the only number a caller should ever act on:

    .. code-block:: text

        headroom = free - outstanding reservations - emergency reserve

    Subtracting outstanding reservations is what stops two workers each
    believing the last gigabyte is theirs. Subtracting the emergency reserve is
    what leaves SQLite able to commit the transaction recording whatever went
    wrong (``docs/architecture/11-storage-strategy.md`` §11.7).

    Attributes:
        capacity_bytes: Size of the device holding the workspace.
        free_bytes: Unallocated space the filesystem reports.
        reserved_bytes: Space promised to open leases and not yet written.
        emergency_bytes: Floor that is never allocatable.
    """

    capacity_bytes: int
    free_bytes: int
    reserved_bytes: int = 0
    emergency_bytes: int = 0

    def __post_init__(self) -> None:
        """Refuse negative quantities, which can only be an accounting bug."""
        for name in ("capacity_bytes", "free_bytes", "reserved_bytes", "emergency_bytes"):
            value: int = getattr(self, name)
            if value < 0:
                message = f"DiskBudget.{name} must not be negative, got {value}."
                raise InvariantViolationError(message)

    @property
    def headroom_bytes(self) -> int:
        """Return the space that may actually be handed out."""
        return max(0, self.free_bytes - self.reserved_bytes - self.emergency_bytes)

    @property
    def headroom_ratio(self) -> float:
        """Return headroom as a fraction of the device, ``0.0`` when unknown."""
        if self.capacity_bytes <= 0:
            return 0.0
        return self.headroom_bytes / self.capacity_bytes

    def after_reserving(self, request_bytes: int) -> DiskBudget:
        """Return the budget that would result from granting ``request_bytes``."""
        return DiskBudget(
            capacity_bytes=self.capacity_bytes,
            free_bytes=self.free_bytes,
            reserved_bytes=self.reserved_bytes + max(0, request_bytes),
            emergency_bytes=self.emergency_bytes,
        )


@dataclass(frozen=True, slots=True)
class IntegrityExpectation(ValueObject):
    """What a downloaded artifact has to prove before it is published.

    Both fields are optional because both are often unknown: providers lie about
    size and rarely publish a hash. An expectation with nothing in it is not a
    failure, it is an honest "nothing to check" - and the size that *is* always
    known, the number of bytes actually written, is checked by the caller
    against what it received.

    Attributes:
        expected_bytes: Exact size the artifact must have, when known.
        expected_fingerprint: Digest the content must produce, when known.
    """

    expected_bytes: int | None = None
    expected_fingerprint: Fingerprint | None = None

    def __post_init__(self) -> None:
        """Refuse a negative size, which no artifact can satisfy."""
        if self.expected_bytes is not None and self.expected_bytes < 0:
            message = f"An expected size must not be negative, got {self.expected_bytes}."
            raise InvariantViolationError(message)

    @property
    def is_empty(self) -> bool:
        """Return whether there is nothing to verify."""
        return self.expected_bytes is None and self.expected_fingerprint is None

    def check(
        self,
        artifact: str,
        *,
        actual_bytes: int,
        actual_fingerprint: Fingerprint | None = None,
    ) -> None:
        """Raise unless the artifact matches every stated expectation.

        Size is checked first because it is free and catches the common failure
        - a truncated download - without hashing two gigabytes to discover it.

        Args:
            artifact: Name of the artifact, for the error message.
            actual_bytes: How many bytes were written.
            actual_fingerprint: The digest of those bytes, when one was taken.

        Raises:
            IntegrityCheckFailedError: If the size differs, if the digest
                differs, or if a digest was required and none was computed.
        """
        if self.expected_bytes is not None and actual_bytes != self.expected_bytes:
            raise IntegrityCheckFailedError(
                artifact,
                "size mismatch",
                expected=self.expected_bytes,
                actual=actual_bytes,
            )
        expected = self.expected_fingerprint
        if expected is None:
            return
        if actual_fingerprint is None:
            raise IntegrityCheckFailedError(
                artifact,
                "no digest was computed",
                expected=str(expected),
                actual=None,
            )
        if actual_fingerprint != expected:
            raise IntegrityCheckFailedError(
                artifact,
                "digest mismatch",
                expected=str(expected),
                actual=str(actual_fingerprint),
            )
