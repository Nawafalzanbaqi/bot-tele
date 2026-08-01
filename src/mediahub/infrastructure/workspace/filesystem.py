"""Filesystem workspace adapter.

Implements :class:`~mediahub.application.workspace.ports.WorkspacePort`.

The layout is the design, so it is worth stating plainly::

    <root>/
      <label>-<lease_id>/        one lease, owned by one unit of work
        lease.json               the manifest: owner, state, accounting
        files/                   the artifacts, and the only thing exposed

Nothing is shared. There is no scratch directory two jobs both write into,
because ambiguous ownership means nobody deletes, and no filename is ever
reused: every lease id and every artifact name is generated
(``docs/architecture/11-storage-strategy.md`` §11.5).

Five guarantees are implemented here, each in one place:

* **Containment** - :mod:`~mediahub.infrastructure.workspace.containment` is the
  only code that turns a name into a path.
* **Atomicity** - bytes land in a hidden temporary file; the rename into the
  final name happens after verification and never before, so a partial download
  cannot be observed as a finished artifact.
* **Accounting** - reservations are held against the device's headroom in
  :mod:`~mediahub.infrastructure.workspace.disk` until they are written or
  released.
* **Reclamation** - leaving the lease context deletes the directory whether the
  work succeeded, failed or was cancelled. A device this small cannot rely on
  the happy path remembering to tidy up.
* **Recovery** - what survives a crash is found again through the manifests, and
  the decision about it belongs to
  :class:`~mediahub.domain.workspace.policies.RecoveryPolicy`.
"""

from __future__ import annotations

import os
import shutil
import socket
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from loguru import logger

from mediahub.application.workspace.ports import (
    ArtifactRef,
    ArtifactRole,
    LeaseRecord,
    WorkspaceUsage,
)
from mediahub.domain.common.errors import EntityNotFoundError, InvalidStateTransitionError
from mediahub.domain.workspace.entities import WorkspaceLease
from mediahub.domain.workspace.errors import (
    InsufficientDiskSpaceError,
    IntegrityCheckFailedError,
    LeaseClosedError,
    WorkspaceInconsistentError,
    WorkspaceQuotaExceededError,
)
from mediahub.domain.workspace.identifiers import LeaseId
from mediahub.domain.workspace.policies import DiskPolicy, FilenamePolicy
from mediahub.domain.workspace.value_objects import IntegrityExpectation, LeaseOwner
from mediahub.infrastructure.system.clock import SystemClock
from mediahub.infrastructure.workspace import containment, manifest
from mediahub.infrastructure.workspace.disk import (
    DiskAccountant,
    ReservationLedger,
    SystemDiskProbe,
    WorkspaceLimits,
    is_out_of_space,
)
from mediahub.infrastructure.workspace.hashing import StreamingDigest, digest_of

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path
    from typing import BinaryIO

    from mediahub.application.common.ports import Clock
    from mediahub.domain.common.fingerprint import Fingerprint
    from mediahub.domain.workspace.value_objects import DiskBudget
    from mediahub.infrastructure.workspace.disk import DiskProbe

_LEASE_LABEL_LENGTH: Final[int] = 24
"""How much of a label survives into a directory name. Enough to recognise, not
enough to matter."""

_TOKEN_LENGTH: Final[int] = 16
"""Characters of randomness in a generated artifact name."""

_ENTITY_NAME: Final[str] = "WorkspaceLease"


def _default_owner() -> LeaseOwner:
    """Return the identity of this process, for leases it opens.

    The hostname is the stable half - it survives a restart, which is what lets
    a worker recognise its own wreckage - and the process id is the volatile
    half that says which incarnation wrote it.
    """
    host = socket.gethostname().strip() or "mediahub"
    return LeaseOwner(identity=host, process_id=os.getpid())


class _AtomicWriter:
    """A stream that becomes an artifact only once it verifies.

    Everything is written to a hidden temporary file created with an exclusive,
    no-follow open, so nothing can be pre-empted by a planted symlink or a
    leftover. The rename into the final name is the last thing that happens, and
    it only happens if the size and digest agree with what was expected. Until
    then, the lease contains no such artifact - which is the whole point of
    ``docs/architecture/07-download-pipeline.md`` §7.12.
    """

    __slots__ = (
        "_digest",
        "_expect",
        "_final",
        "_guard",
        "_handle",
        "_lease_id",
        "_name",
        "_on_published",
        "_published",
        "_role",
        "_temporary",
    )

    def __init__(
        self,
        *,
        lease_id: str,
        name: str,
        final: Path,
        temporary: Path,
        role: ArtifactRole,
        expect: IntegrityExpectation,
        guard: Callable[[int], None],
        on_published: Callable[[ArtifactRef], None],
    ) -> None:
        """Prepare a writer; no file exists until :meth:`begin` is called."""
        self._lease_id = lease_id
        self._name = name
        self._final = final
        self._temporary = temporary
        self._role = role
        self._expect = expect
        self._guard = guard
        self._on_published = on_published
        self._digest = StreamingDigest()
        self._handle: BinaryIO | None = None
        self._published: ArtifactRef | None = None

    @property
    def name(self) -> str:
        """Return the final name this artifact will have once published."""
        return self._name

    @property
    def bytes_written(self) -> int:
        """Return how many bytes have been accepted so far."""
        return self._digest.length

    @property
    def published(self) -> ArtifactRef | None:
        """Return the finished artifact, or ``None`` while still in flight."""
        return self._published

    def begin(self) -> None:
        """Create the temporary file. Owned by the enclosing context manager."""
        descriptor = containment.open_exclusive(self._temporary)
        self._handle = os.fdopen(descriptor, "wb")

    def write(self, chunk: bytes) -> int:
        """Append ``chunk``, enforcing the lease ceiling and the device's limits."""
        if self._handle is None:
            raise LeaseClosedError(self._name)
        if not chunk:
            return 0
        self._guard(len(chunk))
        try:
            self._handle.write(chunk)
        except OSError as error:
            raise self._as_workspace_error(error) from error
        self._digest.update(chunk)
        return len(chunk)

    def fingerprint(self) -> Fingerprint:
        """Return the digest of everything written so far."""
        return self._digest.fingerprint()

    def commit(self) -> ArtifactRef:
        """Verify, then publish under the final name.

        Returns:
            A reference to the published artifact, carrying the digest that was
            computed from the stream.

        Raises:
            IntegrityCheckFailedError: If the bytes do not match what was
                expected. The temporary file is removed first, so a failed
                verification leaves nothing behind at all.
            InsufficientDiskSpaceError: If the final flush found the device full.
        """
        handle = self._handle
        if handle is None:  # pragma: no cover - the context manager prevents this
            raise LeaseClosedError(self._name)
        try:
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as error:
            self.abort()
            raise self._as_workspace_error(error) from error
        finally:
            handle.close()
            self._handle = None

        fingerprint = self._digest.fingerprint()
        try:
            self._expect.check(
                self._name,
                actual_bytes=self._digest.length,
                actual_fingerprint=fingerprint,
            )
        except IntegrityCheckFailedError:
            self._discard_temporary()
            raise

        self._temporary.replace(self._final)
        containment.fsync_directory(self._final.parent)
        reference = ArtifactRef(
            lease_id=self._lease_id,
            name=self._name,
            size_bytes=self._digest.length,
            role=self._role,
            fingerprint=fingerprint,
        )
        self._published = reference
        self._on_published(reference)
        return reference

    def abort(self) -> None:
        """Close and delete the temporary file, leaving no trace."""
        if self._handle is not None:
            with suppress(OSError):
                self._handle.close()
            self._handle = None
        self._discard_temporary()

    def _discard_temporary(self) -> None:
        """Remove the temporary file, ignoring the fact that it may be gone."""
        with suppress(OSError):
            self._temporary.unlink(missing_ok=True)

    def _as_workspace_error(self, error: OSError) -> Exception:
        """Translate a write failure into the workspace's own vocabulary.

        A full device arrives as a generic ``OSError`` from somewhere deep in a
        write path. Left alone it would be classified as an unknown failure and
        retried in thirty seconds, forever; named here it becomes the transient,
        long-backoff failure the pipeline expects
        (``docs/architecture/07-download-pipeline.md`` §7.7).
        """
        if is_out_of_space(error):
            return InsufficientDiskSpaceError(self._digest.length, 0)
        return error


class _FilesystemScope:
    """A live lease backed by one directory."""

    __slots__ = (
        "_clock",
        "_closed",
        "_digests",
        "_directory",
        "_lease",
        "_ledger",
        "_max_bytes",
        "_policy",
        "_root",
        "_used_bytes",
    )

    def __init__(
        self,
        *,
        lease: WorkspaceLease,
        root: Path,
        policy: FilenamePolicy,
        ledger: ReservationLedger,
        clock: Clock,
        max_bytes: int | None = None,
    ) -> None:
        """Bind the scope to its lease directory and its accounting.

        Args:
            lease: The lease this scope is the live half of.
            root: The lease directory, holding the manifest and ``files/``.
            policy: Naming rules.
            ledger: Where this lease's reservation is tracked.
            clock: Source of the current time, for manifest updates.
            max_bytes: Ceiling for this lease, when one is configured.
        """
        self._lease = lease
        self._root = root
        self._directory = root / manifest.ARTIFACT_DIRECTORY_NAME
        self._policy = policy
        self._ledger = ledger
        self._clock = clock
        self._max_bytes = max_bytes
        self._digests: dict[str, Fingerprint] = {}
        # Read from disk rather than assumed to be zero: an adopted lease starts
        # with a half-finished download already in it, and a ceiling that
        # ignored those bytes would let a resumed job write twice its allowance.
        self._used_bytes = _directory_size(self._directory)
        self._closed = False

    @property
    def lease_id(self) -> str:
        """Return the identifier of this lease."""
        return str(self._lease.id)

    @property
    def reserved_bytes(self) -> int:
        """Return the space this lease reserved when it was opened."""
        return self._lease.reserved_bytes

    def directory(self) -> Path:
        """Return the lease directory external tools may write into."""
        return self._directory

    def path_for(self, name: str) -> Path:
        """Return the contained path for a validated name."""
        self._require_open()
        self._policy.validate(name)
        return containment.resolve_within(self._directory, name)

    def contains(self, path: Path) -> bool:
        """Return whether ``path`` really resolves inside this lease."""
        return containment.is_within(self._directory, path)

    @contextmanager
    def open_artifact(
        self,
        *,
        extension: str | None = None,
        role: ArtifactRole = ArtifactRole.PRIMARY,
        expect: IntegrityExpectation | None = None,
    ) -> Iterator[_AtomicWriter]:
        """Open a stream that becomes an artifact once it verifies."""
        self._require_open()
        token = uuid4().hex[:_TOKEN_LENGTH]
        name = self._policy.generated_name(token, extension)
        writer = _AtomicWriter(
            lease_id=self.lease_id,
            name=name,
            final=containment.resolve_within(self._directory, name),
            temporary=containment.resolve_within(
                self._directory, self._policy.temporary_name(token)
            ),
            role=role,
            expect=expect or IntegrityExpectation(),
            guard=self._guard_capacity,
            on_published=self._record_published,
        )
        writer.begin()
        try:
            yield writer
            writer.commit()
        except BaseException:
            # Including a failed verification: nothing is left under the final
            # name, and nothing is left under the temporary one either.
            writer.abort()
            raise

    def artifact(self, name: str, *, role: ArtifactRole = ArtifactRole.PRIMARY) -> ArtifactRef:
        """Take a reference to an existing file in this lease."""
        path = self.path_for(name)
        if not path.is_file():
            message = f"artifact '{name}' does not exist in lease {self.lease_id}"
            raise FileNotFoundError(message)
        return ArtifactRef(
            lease_id=self.lease_id,
            name=name,
            size_bytes=path.stat().st_size,
            role=role,
            fingerprint=self._digests.get(name),
        )

    def artifacts(self) -> Sequence[ArtifactRef]:
        """Return references to every published file currently in the lease."""
        return tuple(self.artifact(name) for name in self.names())

    def names(self) -> Sequence[str]:
        """Return the names of every published file in the lease.

        In-flight temporaries are excluded, which is what makes "the artifact
        exists" and "the artifact is complete" the same statement.
        """
        if not self._directory.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in self._directory.iterdir()
                if entry.is_file() and not self._policy.is_temporary(entry.name)
            )
        )

    def used_bytes(self) -> int:
        """Return everything the lease occupies, temporaries included."""
        return _directory_size(self._directory)

    def remove(self, name: str) -> None:
        """Delete one file from the lease. Missing files are ignored."""
        path = self.path_for(name)
        path.unlink(missing_ok=True)
        self._digests.pop(name, None)
        self._settle()

    def fingerprint_of(self, name: str) -> Fingerprint:
        """Return the content digest of one artifact, computing it at most once."""
        cached = self._digests.get(name)
        if cached is not None:
            return cached
        path = self.path_for(name)
        if not path.is_file():
            message = f"artifact '{name}' does not exist in lease {self.lease_id}"
            raise FileNotFoundError(message)
        computed = digest_of(path)
        self._digests[name] = computed
        return computed

    def verify(self, name: str, *, expect: IntegrityExpectation | None = None) -> ArtifactRef:
        """Check an artifact against its expectations and return a reference."""
        path = self.path_for(name)
        if not path.is_file():
            message = f"artifact '{name}' does not exist in lease {self.lease_id}"
            raise FileNotFoundError(message)
        expectation = expect or IntegrityExpectation()
        size = path.stat().st_size
        needs_digest = expectation.expected_fingerprint is not None
        fingerprint = self.fingerprint_of(name) if needs_digest else self._digests.get(name)
        expectation.check(name, actual_bytes=size, actual_fingerprint=fingerprint)
        return ArtifactRef(
            lease_id=self.lease_id,
            name=name,
            size_bytes=size,
            fingerprint=fingerprint,
        )

    def verify_consistency(self) -> None:
        """Assert that the lease is still a lease."""
        if self._closed:
            raise LeaseClosedError(self.lease_id)
        if self._directory.is_symlink() or not self._directory.is_dir():
            raise WorkspaceInconsistentError(self.lease_id, "the lease directory is missing")
        for entry in containment.iter_entries(self._directory):
            if entry.is_symlink():
                raise WorkspaceInconsistentError(
                    self.lease_id, f"'{entry.name}' is a symbolic link"
                )
            if not (entry.is_file() or entry.is_dir()):
                raise WorkspaceInconsistentError(
                    self.lease_id, f"'{entry.name}' is not a regular file"
                )

    def close(self) -> int:
        """Release the lease: record the intent, then delete everything.

        The manifest is moved to ``RELEASING`` *before* the files go, so a crash
        halfway through deletion leaves a directory that says "this was being
        deleted" rather than one that claims to hold live work.

        Removal is *verified*. A read-only filesystem, a revoked permission or a
        descriptor another process still holds all make deletion fail, and the
        previous behaviour was to swallow that and report the bytes as reclaimed
        anyway - so the one event an operator needed to see, a workspace that has
        stopped emptying itself, was the one event that produced no log line and
        no metric. The reservation is released either way: this process is
        finished with the lease whether or not the filesystem agreed, and holding
        the reservation would refuse work the device can still do.

        Returns:
            The number of bytes actually reclaimed; ``0`` if the directory
            survived.
        """
        if self._closed:
            return 0
        self._closed = True
        occupied = self.used_bytes()
        now = self._clock.now()
        with suppress(OSError, InvalidStateTransitionError):
            self._lease.begin_release(now)
            manifest.write(self._root, self._lease)

        shutil.rmtree(self._root, ignore_errors=True)
        self._ledger.release(self.lease_id)
        if not self._root.exists():
            return occupied

        logger.bind(
            lease_id=self.lease_id, workspace=str(self._root), stranded_bytes=occupied
        ).error(
            "A workspace lease could not be deleted and is now an orphan. It will be "
            "swept at the next restart; until then its bytes are unavailable."
        )
        return 0

    def _require_open(self) -> None:
        """Refuse to operate on a lease that has already been released."""
        if self._closed:
            raise LeaseClosedError(self.lease_id)

    def _guard_capacity(self, additional_bytes: int) -> None:
        """Refuse a write that would take the lease past its ceiling."""
        if self._max_bytes is None:
            return
        projected = self._used_bytes + additional_bytes
        if projected > self._max_bytes:
            raise WorkspaceQuotaExceededError(self._max_bytes, projected)
        self._used_bytes = projected

    def _record_published(self, reference: ArtifactRef) -> None:
        """Remember an artifact's digest and bring the accounting up to date."""
        if reference.fingerprint is not None:
            self._digests[reference.name] = reference.fingerprint
        self._settle()

    def _settle(self) -> None:
        """Re-read the lease's size and report it to the manifest and the ledger."""
        used = self.used_bytes()
        self._used_bytes = used
        self._ledger.settle(self.lease_id, used)
        with suppress(OSError, InvalidStateTransitionError):
            self._lease.record_usage(used, self._clock.now())
            manifest.write(self._root, self._lease)


class FilesystemWorkspace:
    """Hands out lease directories under one root.

    Everything is set once at construction and the adapter holds no per-lease
    state beyond the ledger, so several callers can hold leases concurrently
    without coordinating.
    """

    __slots__ = (
        "_accountant",
        "_clock",
        "_disk_policy",
        "_limits",
        "_owner",
        "_policy",
        "_root",
    )

    def __init__(
        self,
        root: Path,
        *,
        limits: WorkspaceLimits | None = None,
        owner: LeaseOwner | None = None,
        clock: Clock | None = None,
        filename_policy: FilenamePolicy | None = None,
        disk_policy: DiskPolicy | None = None,
        probe: DiskProbe | None = None,
    ) -> None:
        """Prepare the workspace root.

        Args:
            root: Directory that will contain every lease.
            limits: Emergency reserve and the per-lease and per-workspace
                ceilings. Defaults to "the device is the only limit".
            owner: Identity recorded on every lease this process opens. Defaults
                to ``<hostname>`` plus this process id, which is what makes a
                restarted process able to recognise its own wreckage.
            clock: Source of the current time. Defaults to the system clock.
            filename_policy: Naming rules.
            disk_policy: Admission thresholds.
            probe: How free space is read. Substituted in tests, so disk
                pressure can be exercised without filling a real device.
        """
        self._root = root
        self._limits = limits or WorkspaceLimits()
        self._owner = owner or _default_owner()
        self._clock = clock or SystemClock()
        self._policy = filename_policy or FilenamePolicy()
        self._disk_policy = disk_policy or DiskPolicy()
        self._accountant = DiskAccountant(
            root,
            emergency_bytes=self._limits.min_free_bytes,
            probe=probe or SystemDiskProbe(),
            ledger=ReservationLedger(),
        )
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the workspace root. For diagnostics and startup sweeps only."""
        return self._root

    @property
    def owner(self) -> LeaseOwner:
        """Return the identity this process stamps on the leases it opens."""
        return self._owner

    def free_bytes(self) -> int:
        """Return usable free space: headroom after reservations and the floor."""
        return self.budget().headroom_bytes

    def budget(self) -> DiskBudget:
        """Return the current free/reserved/emergency picture of the device."""
        return self._accountant.budget()

    def usage(self) -> WorkspaceUsage:
        """Return what the workspace is holding, for reporting and health."""
        ledger = self._accountant.ledger
        return WorkspaceUsage(
            lease_count=ledger.count(),
            used_bytes=_directory_size(self._root),
            reserved_bytes=ledger.outstanding_bytes(),
            budget=self.budget(),
            orphan_leases=len(self.orphans()),
        )

    def orphans(self) -> Sequence[LeaseRecord]:
        """Return leases this live process owns on disk but is not holding.

        The startup sweep answers "what did a *previous* process leave behind?".
        This answers the question nothing else asks: what has this process leaked
        *while running*? The two halves of the definition are exact - the
        manifest names this identity **and** this process id, so it cannot be
        another worker's live lease; and the ledger has no entry for it, so it is
        not one of ours either.

        In a healthy process the answer is always empty. Anything else means a
        deletion failed or a scope was dropped without being closed, and on a
        32 GB card that is measured in days, not years.
        """
        held = self._accountant.ledger.identifiers()
        return tuple(
            record
            for record in self.leases_on_disk()
            if record.lease is not None
            and record.lease.owner.is_same_process(self._owner)
            and record.lease_id not in held
        )

    @contextmanager
    def lease(
        self,
        *,
        label: str,
        reserve_bytes: int | None = None,
    ) -> Iterator[_FilesystemScope]:
        """Open a lease directory, removing it and its contents on exit."""
        reservation = max(0, reserve_bytes or 0)
        if reserve_bytes is not None:
            self._admit(reservation)

        now = self._clock.now()
        lease_id = LeaseId(uuid4().hex)
        directory = self._make_directory(label, lease_id)
        lease = WorkspaceLease(
            lease_id=lease_id,
            owner=self._owner,
            label=self._policy.safe_name(label)[:_LEASE_LABEL_LENGTH],
            created_at=now,
            reserved_bytes=reservation,
        )
        try:
            manifest.write(directory, lease)
        except OSError:
            # A lease nothing can recover is worse than no lease at all: it
            # would be swept as unclaimed debris and its bytes leaked until then.
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self._accountant.ledger.reserve(str(lease_id), reservation)

        scope = self._scope_for(lease, directory)
        bound = logger.bind(lease_id=str(lease_id), workspace=str(directory))
        bound.debug("Workspace lease opened")
        try:
            yield scope
        finally:
            reclaimed = scope.close()
            bound.bind(reclaimed_bytes=reclaimed).debug("Workspace lease released")

    @contextmanager
    def reopen(self, lease_id: str) -> Iterator[_FilesystemScope]:
        """Take ownership of a lease that survived the process that made it."""
        directory = self._entry_for(lease_id)
        lease = manifest.read(directory) if directory is not None and directory.is_dir() else None
        if directory is None or lease is None:
            raise EntityNotFoundError(_ENTITY_NAME, lease_id)

        lease.adopt(self._owner, self._clock.now())
        manifest.write(directory, lease)
        self._accountant.ledger.reserve(str(lease.id), lease.outstanding_bytes)

        scope = self._scope_for(lease, directory)
        bound = logger.bind(lease_id=str(lease.id), workspace=str(directory))
        bound.info("Adopted a workspace lease left by a previous process")
        try:
            yield scope
        finally:
            reclaimed = scope.close()
            bound.bind(reclaimed_bytes=reclaimed).debug("Workspace lease released")

    def leases_on_disk(self) -> Sequence[LeaseRecord]:
        """Return every entry under the root, whether or not it claims an owner."""
        if not self._root.is_dir():
            return ()
        # Wall clock rather than the injected one: this age is compared against
        # a filesystem timestamp, and mixing two sources of time is how a sweep
        # decides a directory written moments ago is an hour old.
        now = time.time()
        records: list[LeaseRecord] = []
        for entry in sorted(self._root.iterdir()):
            lease = manifest.read(entry) if entry.is_dir() and not entry.is_symlink() else None
            records.append(
                LeaseRecord(
                    lease_id=str(lease.id) if lease is not None else entry.name,
                    lease=lease,
                    used_bytes=_directory_size(entry) if entry.is_dir() else _size_of(entry),
                    age_seconds=_age_seconds(entry, now),
                )
            )
        return tuple(records)

    def discard(self, lease_id: str) -> int:
        """Delete one lease directory and return the bytes reclaimed.

        Safe by construction: the entry is located by *listing* the root and
        matching a name, never by joining the argument to a path, so nothing a
        caller passes can reach outside the workspace. A symlink is unlinked
        rather than followed.
        """
        entry = self._entry_for(lease_id)
        if entry is None:
            return 0
        reclaimed = _directory_size(entry) if entry.is_dir() else _size_of(entry)
        if entry.is_symlink() or entry.is_file():
            entry.unlink(missing_ok=True)
        else:
            shutil.rmtree(entry)
        self._accountant.ledger.release(lease_id)
        logger.bind(lease_id=lease_id, reclaimed_bytes=reclaimed).info(
            "Reclaimed a workspace lease"
        )
        return reclaimed

    def _admit(self, reservation: int) -> None:
        """Refuse a reservation the ceilings or the device cannot honour."""
        ceiling = self._limits.max_lease_bytes
        if ceiling is not None and reservation > ceiling:
            raise WorkspaceQuotaExceededError(ceiling, reservation, scope="lease")

        total_ceiling = self._limits.max_total_bytes
        if total_ceiling is not None:
            projected = _directory_size(self._root) + reservation
            if projected > total_ceiling:
                raise WorkspaceQuotaExceededError(total_ceiling, projected, scope="workspace")

        budget = self._accountant.budget()
        if not self._disk_policy.admits(budget, reservation):
            raise InsufficientDiskSpaceError(
                reservation, self._disk_policy.largest_admissible(budget)
            )

    def _make_directory(self, label: str, lease_id: LeaseId) -> Path:
        """Create ``<label>-<lease_id>/files`` and return the lease directory."""
        safe_label = self._policy.safe_name(label)[:_LEASE_LABEL_LENGTH]
        directory = self._root / f"{safe_label}-{lease_id}"
        directory.mkdir(parents=True, exist_ok=False)
        (directory / manifest.ARTIFACT_DIRECTORY_NAME).mkdir()
        return directory

    def _scope_for(self, lease: WorkspaceLease, directory: Path) -> _FilesystemScope:
        """Return a live scope over an existing lease directory."""
        return _FilesystemScope(
            lease=lease,
            root=directory,
            policy=self._policy,
            ledger=self._accountant.ledger,
            clock=self._clock,
            max_bytes=self._limits.max_lease_bytes,
        )

    def _entry_for(self, identifier: str) -> Path | None:
        """Return the root entry ``identifier`` names, or ``None``.

        Located by listing rather than by joining: an identifier is matched
        against the names that are actually there, so a traversal sequence
        simply matches nothing.
        """
        if not identifier or identifier in {".", ".."} or {"/", "\\"} & set(identifier):
            return None
        if not self._root.is_dir():
            return None
        suffix = f"-{identifier}"
        for entry in self._root.iterdir():
            if entry.name == identifier or entry.name.endswith(suffix):
                return entry
        return None


def _directory_size(directory: Path) -> int:
    """Return the total size of every regular file under ``directory``."""
    return sum(_size_of(path) for path in containment.iter_regular_files(directory))


def _size_of(path: Path) -> int:
    """Return a file's size, treating anything unreadable as zero."""
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - the file went away mid-walk
        return 0


def _age_seconds(entry: Path, now: float) -> float:
    """Return how long ago ``entry`` was last modified, never negative."""
    try:
        return max(0.0, now - entry.stat().st_mtime)
    except OSError:  # pragma: no cover - the entry went away mid-scan
        return 0.0
