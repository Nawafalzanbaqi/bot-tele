"""The workspace port.

A **lease** is a bounded, private directory owned by one unit of work. It is
opened by whoever owns the work (a worker, a CLI command, a test), passed into
operations that need to write, and closed - deleting everything - when the work
reaches a terminal state.

Four properties are the whole point:

* **Containment.** A name is turned into a path only by
  :meth:`WorkspaceScope.path_for`, which refuses anything that could escape.
  External tools that generate their own filenames are checked on the way back
  with :meth:`WorkspaceScope.contains`.
* **Atomicity.** Bytes arrive through :meth:`WorkspaceScope.open_artifact`,
  which writes under a hidden temporary name and renames into place *only after*
  verification succeeds. A reader of the lease therefore never sees a partial
  file under a final name (``docs/architecture/07-download-pipeline.md`` §7.12).
* **Accounting.** Every lease reserves space up front, and the reservation is
  subtracted from the device's headroom until it is written or released, so two
  callers cannot both believe the last gigabyte is theirs
  (``docs/architecture/11-storage-strategy.md`` §11.7).
* **Reclamation.** Leaving the lease context deletes the directory, so a failed
  or cancelled operation cannot leak bytes onto a small device - and anything
  that survives a crash is found again by :meth:`WorkspacePort.leases_on_disk`.

``Path`` appears in this port deliberately. A workspace *is* a filesystem
concept - a tmpfs workspace still has paths - and external tools such as the
download engine need a real directory. What the port refuses to expose is the
workspace *root*: a caller can only ever obtain a path for a validated name
inside its own lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mediahub.domain.common.fingerprint import Fingerprint
    from mediahub.domain.workspace.entities import WorkspaceLease
    from mediahub.domain.workspace.value_objects import DiskBudget, IntegrityExpectation


class ArtifactRole(StrEnum):
    """What a file inside a lease is for.

    Attributes:
        PRIMARY: The media itself. Exactly one per download result.
        COMPANION: Another item of the same post, and media in its own right -
            the second and later pictures of a carousel or a slideshow.
            Distinct from ``THUMBNAIL``, which is a preview *of* the primary and
            is never worth delivering on its own; a companion is something the
            user asked for and would notice the absence of.
        THUMBNAIL: A poster or preview image.
        SUBTITLE: A subtitle or caption track.
        SIDECAR: Anything else the engine produced, e.g. extracted metadata.
    """

    PRIMARY = "primary"
    COMPANION = "companion"
    THUMBNAIL = "thumbnail"
    SUBTITLE = "subtitle"
    SIDECAR = "sidecar"


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A handle to one file inside a lease.

    Deliberately **not** a path: it identifies a file by lease and name, and
    only the workspace adapter can turn it back into a location on disk. That
    is what stops paths from spreading through the codebase.

    Attributes:
        lease_id: The lease that owns the file.
        name: The file's name inside the lease directory.
        size_bytes: Size at the moment the reference was taken.
        role: What the file is for.
        fingerprint: Content digest, when one has been computed. Present for
            free on anything written through :meth:`WorkspaceScope.open_artifact`,
            because the digest is taken from the stream rather than by reading
            the finished file again.
    """

    lease_id: str
    name: str
    size_bytes: int
    role: ArtifactRole = ArtifactRole.PRIMARY
    fingerprint: Fingerprint | None = None


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    """A lease directory found on disk, as read back after a restart.

    Returned by :meth:`WorkspacePort.leases_on_disk`. It is a snapshot rather
    than a live handle: deciding what to do with a lease must not require
    opening it, because most of them are about to be deleted.

    Attributes:
        lease_id: The directory's identifier, as text.
        lease: The lease reconstructed from its manifest, or ``None`` when no
            manifest could be read - debris, a directory created by something
            else, or a crash between ``mkdir`` and the first write.
        used_bytes: Bytes currently occupied by the directory.
        age_seconds: How long ago the directory itself was last modified. The
            only age available when there is no manifest to ask.
    """

    lease_id: str
    lease: WorkspaceLease | None
    used_bytes: int
    age_seconds: float


@dataclass(frozen=True, slots=True)
class WorkspaceUsage:
    """What the workspace is holding right now.

    Attributes:
        lease_count: Open leases in this process.
        used_bytes: Bytes on disk under the workspace root.
        reserved_bytes: Reserved-but-unwritten bytes still accounted for.
        budget: Free space, reservations and the emergency floor.
        orphan_leases: Lease directories this process owns on disk but is no
            longer holding - a deletion that failed, or a scope dropped without
            being closed. Always zero in a healthy process; anything else is
            bytes that will not come back until a restart.
    """

    lease_count: int
    used_bytes: int
    reserved_bytes: int
    budget: DiskBudget
    orphan_leases: int = 0

    @property
    def is_leaking(self) -> bool:
        """Return whether the workspace is holding bytes nothing will free."""
        return self.orphan_leases > 0


class ArtifactWriter(Protocol):
    """A stream that becomes an artifact only if it verifies.

    Obtained from :meth:`WorkspaceScope.open_artifact`. Leaving the context
    manager normally flushes, verifies and atomically renames; leaving it with
    an exception deletes the temporary file. Either way nothing partial is left
    under a name a caller could use.
    """

    @property
    def name(self) -> str:
        """Return the final name this artifact will have once published."""
        ...

    @property
    def bytes_written(self) -> int:
        """Return how many bytes have been accepted so far."""
        ...

    @property
    def published(self) -> ArtifactRef | None:
        """Return the reference to the finished artifact, or ``None``.

        Only populated after the context manager exits successfully, which is
        precisely the point: while the block is running there is no artifact.
        """
        ...

    def write(self, chunk: bytes) -> int:
        """Append ``chunk`` and return how many bytes were written.

        Raises:
            InsufficientDiskSpaceError: If the device filled up mid-write.
            WorkspaceQuotaExceededError: If a configured ceiling was crossed.
        """
        ...

    def fingerprint(self) -> Fingerprint:
        """Return the digest of everything written so far.

        Computed incrementally from the stream, so obtaining it costs nothing
        and never re-reads the file (``docs/architecture/11-storage-strategy.md``
        §11.8).
        """
        ...


class WorkspaceScope(Protocol):
    """A live lease: the space one unit of work may write to."""

    @property
    def lease_id(self) -> str:
        """Return the identifier of this lease."""
        ...

    @property
    def reserved_bytes(self) -> int:
        """Return the space this lease reserved when it was opened."""
        ...

    def directory(self) -> Path:
        """Return the lease directory.

        For adapters that must hand a real directory to an external tool. Such
        an adapter is responsible for checking every file the tool produced with
        :meth:`contains` before treating it as an artifact.
        """
        ...

    def path_for(self, name: str) -> Path:
        """Return the contained path for a validated name.

        Raises:
            InvalidArtifactNameError: If the name is unsafe.
            PathEscapesWorkspaceError: If the resolved path leaves the lease.
            LeaseClosedError: If the lease has already been released.
        """
        ...

    def contains(self, path: Path) -> bool:
        """Return whether ``path`` really resolves inside this lease."""
        ...

    def open_artifact(
        self,
        *,
        extension: str | None = None,
        role: ArtifactRole = ArtifactRole.PRIMARY,
        expect: IntegrityExpectation | None = None,
    ) -> AbstractContextManager[ArtifactWriter]:
        """Open a stream that becomes an artifact once it verifies.

        The name is **generated**, never supplied: a provider's filename is
        metadata, and using it as a path component is how traversal and reserved
        device names reach the disk (``docs/architecture/14-security-architecture.md``
        §14.4). The caller learns the name from the writer.

        Args:
            extension: Container hint for the generated name, e.g. ``mp4``.
                Sanitised; it never becomes a path component of its own.
            role: What the artifact will be.
            expect: Size and digest the content must match before it is
                published. Omitted expectations are simply not checked.

        Returns:
            A context manager yielding the writer.

        Raises:
            LeaseClosedError: If the lease has already been released.
        """
        ...

    def artifact(self, name: str, *, role: ArtifactRole = ArtifactRole.PRIMARY) -> ArtifactRef:
        """Take a reference to an existing file in this lease.

        Raises:
            InvalidArtifactNameError: If the name is unsafe.
            PathEscapesWorkspaceError: If the file is not inside the lease.
            FileNotFoundError: If the file does not exist.
        """
        ...

    def artifacts(self) -> Sequence[ArtifactRef]:
        """Return references to every published file currently in the lease."""
        ...

    def names(self) -> Sequence[str]:
        """Return the names of every published file in the lease.

        In-flight temporary files are excluded by construction: an unfinished
        download must never be visible as a finished artifact.
        """
        ...

    def used_bytes(self) -> int:
        """Return the total size of everything in the lease, temporaries included."""
        ...

    def remove(self, name: str) -> None:
        """Delete one file from the lease. Missing files are ignored."""
        ...

    def fingerprint_of(self, name: str) -> Fingerprint:
        """Return the content digest of one artifact.

        Free for anything written through :meth:`open_artifact` - the digest was
        taken from the stream. Anything an external tool wrote is hashed once
        and remembered, so asking twice costs one read, not two.

        Raises:
            FileNotFoundError: If the artifact does not exist.
        """
        ...

    def verify(
        self,
        name: str,
        *,
        expect: IntegrityExpectation | None = None,
    ) -> ArtifactRef:
        """Check an artifact against its expectations and return a reference.

        For files an external tool wrote directly into the lease directory.
        Anything written through :meth:`open_artifact` was already verified
        before it was published.

        Raises:
            FileNotFoundError: If the artifact does not exist.
            IntegrityCheckFailedError: If size or digest disagree.
        """
        ...

    def verify_consistency(self) -> None:
        """Assert that the lease is still a lease.

        Checks that the directory exists, holds nothing but regular files, and
        contains no symlink - a symlink inside a lease is either an attack or a
        bug, and in both cases the next write through it would land outside the
        space this lease is accountable for.

        Raises:
            WorkspaceInconsistentError: If the lease is not in a state the
                workspace can vouch for.
        """
        ...


class WorkspaceLeasing(Protocol):
    """The half of the workspace a unit of work needs: somewhere to write.

    Split from the whole port deliberately. A worker executing a job must not be
    able to sweep the workspace root - the two operations disagree about what a
    lease belonging to somebody else means, and only one of them is ever right.
    """

    def lease(
        self,
        *,
        label: str,
        reserve_bytes: int | None = None,
    ) -> AbstractContextManager[WorkspaceScope]:
        """Open a lease, deleting it and its contents on exit.

        Args:
            label: Short, human-readable purpose, used in logs and directory
                names. Sanitised by the adapter.
            reserve_bytes: Space the caller expects to need. Checked against
                headroom **before** any work starts, and held against it until
                the lease is released, so the device refuses early rather than
                filling up.

        Raises:
            InsufficientDiskSpaceError: If the reservation cannot be honoured.
            WorkspaceQuotaExceededError: If it exceeds a configured ceiling.
        """
        ...

    def free_bytes(self) -> int:
        """Return usable free space: headroom after reservations and the floor."""
        ...


class WorkspaceInventory(Protocol):
    """The half a sweeper needs: what is on disk, and how to remove it.

    Reading is separate from deleting, and both are separate from deciding
    (:class:`~mediahub.domain.workspace.policies.RecoveryPolicy`), so the rules
    can be tested without a filesystem and the filesystem without the rules.
    """

    def leases_on_disk(self) -> Sequence[LeaseRecord]:
        """Return every lease directory under the root, owned or not.

        The input to crash recovery.
        """
        ...

    def discard(self, lease_id: str) -> int:
        """Delete one lease directory and return the bytes reclaimed.

        Idempotent: discarding something that is already gone is not an error,
        because a sweep that cannot be run twice is a sweep that cannot be run
        after a crash.
        """
        ...


class WorkspacePort(WorkspaceLeasing, WorkspaceInventory, Protocol):
    """Everything: leasing, recovery, and the accounting between them.

    What the composition root wires up. Callers should depend on the narrower
    half they actually use.
    """

    def reopen(self, lease_id: str) -> AbstractContextManager[WorkspaceScope]:
        """Take ownership of a lease that survived the process that made it.

        The recovery half of the lifecycle: the bytes of a half-finished
        download are still there, and a resumed job should continue into the
        same lease rather than start again. Ownership is rewritten to this
        process, so a later sweep does not treat the lease as abandoned.

        Raises:
            EntityNotFoundError: If no such lease exists on disk.
            InvalidStateTransitionError: If the lease no longer holds work.
        """
        ...

    def budget(self) -> DiskBudget:
        """Return the current free/reserved/emergency picture of the device."""
        ...

    def usage(self) -> WorkspaceUsage:
        """Return what the workspace is holding, for reporting and health."""
        ...
