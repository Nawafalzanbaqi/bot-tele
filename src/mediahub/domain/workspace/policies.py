"""The workspace's rules: naming, disk admission, and crash recovery.

All three are pure - stdlib only, no filesystem, no clock - so the hostile-input
corpus, the disk thresholds and the recovery decisions can each be tested
exhaustively in milliseconds, and the adapter is left with nothing to decide.

* :class:`FilenamePolicy` generates every name that reaches the filesystem and
  refuses anything that could escape a lease, confuse a shell or collide with a
  reserved device name (``docs/architecture/14-security-architecture.md`` §14.4).
* :class:`DiskPolicy` turns free space into an admission decision
  (``docs/architecture/11-storage-strategy.md`` §11.7).
* :class:`RecoveryPolicy` decides what happens to a lease directory found on
  disk after a restart (§11.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final

from mediahub.domain.workspace.enums import DiskState, RecoveryAction
from mediahub.domain.workspace.errors import InvalidArtifactNameError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mediahub.domain.workspace.entities import WorkspaceLease
    from mediahub.domain.workspace.value_objects import DiskBudget, LeaseOwner

_SAFE_CHARACTERS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_RESERVED_STEMS: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


@dataclass(frozen=True, slots=True)
class FilenamePolicy:
    """Produces and validates safe, lease-relative artifact names.

    Attributes:
        max_length: Longest name accepted, including the extension.
        max_extension_length: Longest extension accepted, without the dot.
        fallback_stem: Used when sanitising leaves nothing usable.
    """

    max_length: int = 120
    max_extension_length: int = 12
    fallback_stem: str = "artifact"

    REPLACEMENT: ClassVar[str] = "_"
    TEMPORARY_PREFIX: ClassVar[str] = "."
    TEMPORARY_SUFFIX: ClassVar[str] = ".partial"

    def validate(self, name: str) -> str:
        """Return ``name`` unchanged if it is safe, else raise.

        Args:
            name: A candidate name, relative to a lease directory.

        Returns:
            The same name.

        Raises:
            InvalidArtifactNameError: If the name could escape the lease, is
                reserved, is empty, or contains anything outside the safe set.
        """
        if not name:
            message = "the name is empty"
            raise InvalidArtifactNameError(name, message)
        if len(name) > self.max_length:
            message = f"longer than {self.max_length} characters"
            raise InvalidArtifactNameError(name, message)
        if name in {".", ".."} or name.startswith("."):
            message = "names may not be relative markers or hidden"
            raise InvalidArtifactNameError(name, message)
        if name.startswith("-"):
            # A leading dash makes a filename indistinguishable from a flag once
            # it reaches an external tool's argument list.
            message = "names may not begin with a dash"
            raise InvalidArtifactNameError(name, message)
        if "/" in name or "\\" in name:
            message = "names may not contain path separators"
            raise InvalidArtifactNameError(name, message)
        if "\x00" in name:
            message = "names may not contain null bytes"
            raise InvalidArtifactNameError(name, message)
        if set(name) - _SAFE_CHARACTERS:
            message = "names may only contain letters, digits, dot, dash and underscore"
            raise InvalidArtifactNameError(name, message)
        if name.split(".", maxsplit=1)[0].lower() in _RESERVED_STEMS:
            message = "the name is reserved by the operating system"
            raise InvalidArtifactNameError(name, message)
        return name

    def safe_name(self, stem: str, extension: str | None = None) -> str:
        """Build a safe name from untrusted parts.

        Every unsafe character is replaced rather than removed, so two different
        provider titles cannot silently collapse into the same name.

        Args:
            stem: Untrusted base name (a provider id, a title).
            extension: Untrusted extension, with or without a leading dot.

        Returns:
            A name that :meth:`validate` accepts.
        """
        cleaned_stem = self._sanitise(stem).strip("._-") or self.fallback_stem
        cleaned_extension = self._sanitise(extension or "").strip("._-")

        budget = self.max_length - (len(cleaned_extension) + 1 if cleaned_extension else 0)
        cleaned_stem = cleaned_stem[: max(1, budget)]
        if cleaned_stem.split(".", maxsplit=1)[0].lower() in _RESERVED_STEMS:
            cleaned_stem = f"{self.fallback_stem}_{cleaned_stem}"[: max(1, budget)]

        if not cleaned_extension:
            return cleaned_stem
        return f"{cleaned_stem}.{cleaned_extension[: self.max_extension_length]}"

    def generated_name(self, artifact_id: str, extension: str | None = None) -> str:
        """Build ``<artifact_id>.<extension>`` from a generated identifier.

        This is the *only* way a final artifact name should come into existence
        (``docs/architecture/14-security-architecture.md`` §14.4). A provider's
        filename is metadata; using it as a path component is how traversal,
        control characters, unicode confusables and reserved device names get
        onto disk. :meth:`safe_name` exists for the cases where a human-readable
        name is genuinely wanted - it sanitises rather than generates, which is
        strictly weaker.

        Args:
            artifact_id: A value this system generated, e.g. a lease-unique hex
                token. Sanitised anyway, because "we generated it" is an
                assumption and this is the last line before the filesystem.
            extension: Container or format hint, with or without a leading dot.

        Returns:
            A name that :meth:`validate` accepts.
        """
        identifier = self._sanitise(artifact_id).strip("._-") or self.fallback_stem
        return self.safe_name(identifier, extension)

    def temporary_name(self, token: str) -> str:
        """Build the hidden, unguessable name a download is written under.

        Two properties matter. It is **hidden and suffixed**, so an in-flight
        download can never be mistaken for a finished artifact - nothing is
        published until verification passes. And it carries a random token, so
        the exclusive create that opens it cannot collide with, or be
        pre-empted by, anything already in the directory.

        The result deliberately fails :meth:`validate`: temporary names are
        internal, and no caller may ask for one by name.

        Args:
            token: Random, generated text distinguishing this attempt.

        Returns:
            A name of the form ``.<token>.partial``.
        """
        cleaned = self._sanitise(token).strip("._-") or self.fallback_stem
        budget = self.max_length - len(self.TEMPORARY_PREFIX) - len(self.TEMPORARY_SUFFIX)
        return f"{self.TEMPORARY_PREFIX}{cleaned[: max(1, budget)]}{self.TEMPORARY_SUFFIX}"

    def is_temporary(self, name: str) -> bool:
        """Return whether ``name`` is an unpublished, in-flight artifact."""
        return name.startswith(self.TEMPORARY_PREFIX) and name.endswith(self.TEMPORARY_SUFFIX)

    def _sanitise(self, value: str) -> str:
        """Replace every character outside the safe set."""
        return "".join(char if char in _SAFE_CHARACTERS else self.REPLACEMENT for char in value)


@dataclass(frozen=True, slots=True)
class DiskPolicy:
    """Turns headroom into an admission decision.

    The thresholds are ``docs/architecture/11-storage-strategy.md`` §11.7. The
    reason they are a policy rather than an ``if`` in the adapter is that they
    encode a product judgement - deliveries free space, so they keep running
    while acquisitions are already being refused - and that judgement should be
    stated once, where it can be read.

    Ratios are fractions of the device, measured on headroom rather than on raw
    free space, so outstanding reservations count against the state exactly as
    written bytes do.

    Attributes:
        tight_ratio: Below this, only small requests are admitted.
        low_ratio: Below this, no new acquisitions are admitted.
        critical_ratio: Below this, nothing is admitted at all.
        tight_fraction: Share of headroom the largest admissible request may
            take while ``TIGHT``. A quarter leaves room for the jobs already
            running to finish, which is what actually frees the disk.
    """

    tight_ratio: float = 0.25
    low_ratio: float = 0.10
    critical_ratio: float = 0.05
    tight_fraction: float = 0.25

    def state_of(self, budget: DiskBudget) -> DiskState:
        """Return the device state implied by ``budget``."""
        ratio = budget.headroom_ratio
        if ratio < self.critical_ratio:
            return DiskState.CRITICAL
        if ratio < self.low_ratio:
            return DiskState.LOW
        if ratio < self.tight_ratio:
            return DiskState.TIGHT
        return DiskState.HEALTHY

    def largest_admissible(self, budget: DiskBudget) -> int:
        """Return the biggest reservation that would be granted right now."""
        state = self.state_of(budget)
        if not state.accepts_new_work:
            return 0
        if state is DiskState.TIGHT:
            return int(budget.headroom_bytes * self.tight_fraction)
        return budget.headroom_bytes

    def admits(self, budget: DiskBudget, request_bytes: int) -> bool:
        """Return whether a reservation of ``request_bytes`` may be granted."""
        return max(0, request_bytes) <= self.largest_admissible(budget)


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Decides the fate of a lease directory found on disk.

    Restarting is the only moment the workspace can be certain about ownership,
    and it is also the moment it knows least: the processes that owned these
    directories are gone. The rules below resolve that with the same identity
    model the queue already uses - a stable identity per logical process, one
    process per identity at a time (``docs/architecture/10-worker-architecture.md``
    §10.5):

    1. Same identity, same process id - this is our own live lease. Never touch it.
    2. Same identity, different process id - our previous incarnation, which is
       gone. Adopt it if it still holds work, otherwise delete it.
    3. A different identity - possibly still running. Delete only once it has
       been quiet for longer than a lease period; otherwise leave it alone.

    Rule 3 is the conservative half of §11.6 and is not negotiable: deleting a
    directory another worker is writing into destroys a healthy job, and the
    cost of waiting is a few megabytes for one sweep interval.

    Attributes:
        lease_expiry_seconds: Quiet period after which a foreign lease is
            considered abandoned.
        adopt_own_leases: Whether a surviving lease of ours may be reused.
            Off by default: reuse is only safe once something can also resume
            the job that was writing into it.
        delete_every_lease: Startup purge mode. The workspace root is wiped, on
            the grounds that anything present belongs to a process that no
            longer exists (§11.5). Correct for a single-process deployment, and
            exactly wrong for a shared root - which is why it is a decision the
            operator makes rather than a default the adapter assumes.
    """

    lease_expiry_seconds: float = 3600.0
    adopt_own_leases: bool = False
    delete_every_lease: bool = False

    def decide(
        self,
        lease: WorkspaceLease,
        *,
        now: datetime,
        owner: LeaseOwner,
    ) -> RecoveryAction:
        """Return what should happen to ``lease``.

        Args:
            lease: The lease read back from its manifest.
            now: The current time.
            owner: The identity of the process performing the sweep.

        Returns:
            The action to take.
        """
        if lease.owner.is_same_process(owner):
            return RecoveryAction.LEAVE
        if self.delete_every_lease:
            return RecoveryAction.DELETE
        if lease.owner.is_same_identity(owner):
            if self.adopt_own_leases and lease.is_recoverable:
                return RecoveryAction.ADOPT
            return RecoveryAction.DELETE
        if lease.has_expired(now, self.lease_expiry_seconds):
            return RecoveryAction.DELETE
        return RecoveryAction.LEAVE

    def decide_unclaimed(self, age_seconds: float) -> RecoveryAction:
        """Return what should happen to a directory with no readable manifest.

        A directory nothing claims is either debris or a lease that was being
        created at this exact moment. The age check separates the two: a manifest
        is written immediately after the directory, so anything still unclaimed a
        lease period later is debris.

        Args:
            age_seconds: How long ago the directory was last modified.

        Returns:
            The action to take.
        """
        if self.delete_every_lease or age_seconds >= self.lease_expiry_seconds:
            return RecoveryAction.DELETE
        return RecoveryAction.LEAVE
