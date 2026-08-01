"""The power goes out mid-download. The disk comes back, exactly once.

The crash is simulated the only honest way available in-process: a lease is
opened, written into, and then simply abandoned - the context manager never
exits, so nothing tidies up. That is the state a power cut leaves behind, and
the state ``docs/architecture/11-storage-strategy.md`` §11.6 exists to resolve.

What these tests assert:

* a lease that outlived its process is found again, not leaked;
* it is reclaimed - or adopted, if the operator asked for that - by the identity
  that owns it, and by nobody else;
* a lease another *live* process owns is never deleted, whatever it costs;
* a half-written download is never visible as a finished artifact;
* a full disk is a first-class, retryable outcome rather than an unknown error;
* every sweep converges, and running one twice is harmless.
"""

from __future__ import annotations

import errno
from typing import TYPE_CHECKING, cast

import pytest

from mediahub.application.download.failures import DISK_RETRY_SECONDS, classify
from mediahub.application.workspace.dto import RecoveryReport
from mediahub.application.workspace.use_cases.recover_workspaces import RecoverWorkspaces
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.workspace.enums import LeaseState
from mediahub.domain.workspace.errors import (
    InsufficientDiskSpaceError,
    IntegrityCheckFailedError,
)
from mediahub.domain.workspace.policies import RecoveryPolicy
from mediahub.domain.workspace.value_objects import IntegrityExpectation, LeaseOwner
from mediahub.infrastructure.di.container import build_container
from mediahub.infrastructure.system.clock import SystemClock
from mediahub.infrastructure.workspace import manifest
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace, _AtomicWriter
from mediahub.shared.config.settings import (
    DatabaseSettings,
    DownloadSettings,
    Environment,
    LoggingSettings,
    LogLevel,
    PersistenceBackend,
    Settings,
    WorkspaceSettings,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import BinaryIO

pytestmark = [pytest.mark.chaos, pytest.mark.integration]

WORKER = LeaseOwner(identity="pi:worker:0", process_id=4_001)
WORKER_RESTARTED = LeaseOwner(identity="pi:worker:0", process_id=4_002)
OTHER_WORKER = LeaseOwner(identity="pi:worker:1", process_id=4_003)

_CRASHED: list[object] = []
"""Abandoned lease contexts, kept alive so the garbage collector does not do the
tidying up that a crashed process would never have done."""


@pytest.fixture(autouse=True)
def _forget_crashed_leases() -> Iterator[None]:
    yield
    _CRASHED.clear()


def swap_handle(writer: _AtomicWriter, replacement: object) -> None:
    """Give a writer a different file handle, as a failing device would."""
    current = writer._handle
    assert current is not None
    current.close()
    writer._handle = cast("BinaryIO", replacement)


def crash_mid_download(workspace: FilesystemWorkspace, *, payload: bytes = b"x" * 4_096) -> str:
    """Open a lease, write part of a download into it, and die."""
    context = workspace.lease(label="acquire", reserve_bytes=1_000_000)
    _CRASHED.append(context)
    scope = context.__enter__()
    with scope.open_artifact(extension="mp4") as writer:
        writer.write(payload)
    return scope.lease_id


def sweep(
    workspace: FilesystemWorkspace, policy: RecoveryPolicy, owner: LeaseOwner
) -> RecoveryReport:
    """Run the real recovery use case over a real workspace root."""
    return RecoverWorkspaces(
        workspace=workspace,
        policy=policy,
        owner=owner,
        clock=SystemClock(),
    ).execute()


class TestARestartFindsWhatTheCrashLeft:
    def test_the_lease_survives_the_process(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        crashed = FilesystemWorkspace(root, owner=WORKER)

        lease_id = crash_mid_download(crashed)

        restarted = FilesystemWorkspace(root, owner=WORKER_RESTARTED)
        records = restarted.leases_on_disk()

        assert [record.lease_id for record in records] == [lease_id]
        assert records[0].lease is not None
        assert records[0].lease.state is LeaseState.ACTIVE
        assert records[0].lease.owner == WORKER

    def test_our_own_wreckage_is_reclaimed(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        crash_mid_download(FilesystemWorkspace(root, owner=WORKER))
        restarted = FilesystemWorkspace(root, owner=WORKER_RESTARTED)

        report = sweep(restarted, RecoveryPolicy(), WORKER_RESTARTED)

        assert len(report.deleted) == 1
        assert report.reclaimed_bytes >= 4_096
        assert list(root.iterdir()) == []

    def test_our_own_wreckage_can_be_adopted_and_resumed(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        lease_id = crash_mid_download(FilesystemWorkspace(root, owner=WORKER))
        restarted = FilesystemWorkspace(root, owner=WORKER_RESTARTED)

        report = sweep(restarted, RecoveryPolicy(adopt_own_leases=True), WORKER_RESTARTED)

        assert report.adopted == (lease_id,)
        assert list(root.iterdir()) != []

        with restarted.reopen(lease_id) as scope:
            # The partial download is still there, and it is ours now.
            assert scope.used_bytes() == 4_096
            recorded = manifest.read(scope.directory().parent)
            assert recorded is not None
            assert recorded.owner == WORKER_RESTARTED

        assert list(root.iterdir()) == []

    def test_a_live_worker_sharing_the_root_is_not_robbed(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        neighbour = FilesystemWorkspace(root, owner=OTHER_WORKER)
        neighbours_lease = crash_mid_download(neighbour)
        ours = FilesystemWorkspace(root, owner=WORKER)
        crash_mid_download(ours)

        report = sweep(
            FilesystemWorkspace(root, owner=WORKER_RESTARTED),
            RecoveryPolicy(lease_expiry_seconds=3_600),
            WORKER_RESTARTED,
        )

        assert report.left == (neighbours_lease,)
        assert len(report.deleted) == 1
        assert [entry.name for entry in root.iterdir()] == [
            entry.name for entry in root.iterdir() if neighbours_lease in entry.name
        ]

    def test_an_abandoned_neighbour_is_eventually_reclaimed(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        crash_mid_download(FilesystemWorkspace(root, owner=OTHER_WORKER))

        # Nothing has touched it for longer than a lease period.
        report = sweep(
            FilesystemWorkspace(root, owner=WORKER),
            RecoveryPolicy(lease_expiry_seconds=0.0),
            WORKER,
        )

        assert len(report.deleted) == 1
        assert list(root.iterdir()) == []

    def test_purge_on_start_leaves_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        crash_mid_download(FilesystemWorkspace(root, owner=WORKER))
        crash_mid_download(FilesystemWorkspace(root, owner=OTHER_WORKER))
        (root / "debris").mkdir()

        report = sweep(
            FilesystemWorkspace(root, owner=WORKER_RESTARTED),
            RecoveryPolicy(delete_every_lease=True),
            WORKER_RESTARTED,
        )

        assert len(report.deleted) == 3
        assert list(root.iterdir()) == []

    def test_sweeping_twice_is_harmless(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        crash_mid_download(FilesystemWorkspace(root, owner=WORKER))
        restarted = FilesystemWorkspace(root, owner=WORKER_RESTARTED)

        first = sweep(restarted, RecoveryPolicy(), WORKER_RESTARTED)
        second = sweep(restarted, RecoveryPolicy(), WORKER_RESTARTED)

        assert len(first.deleted) == 1
        assert second.examined == 0
        assert not second.changed_anything

    def test_a_crash_between_verification_and_publication_leaves_nothing_usable(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "workspace"
        workspace = FilesystemWorkspace(root, owner=WORKER)
        context = workspace.lease(label="acquire")
        _CRASHED.append(context)
        scope = context.__enter__()

        writer_context = scope.open_artifact(extension="mp4")
        writer = writer_context.__enter__()
        writer.write(b"x" * 1_024)
        # The process dies here. The operating system closes the descriptor;
        # nothing renames anything, so the bytes exist and the artifact does not.
        swap_handle(writer, _Closed())

        leftovers = list(scope.directory().iterdir())
        assert scope.names() == ()
        assert writer.published is None
        assert scope.used_bytes() == 1_024
        assert all(path.name.endswith(".partial") for path in leftovers)

        report = sweep(
            FilesystemWorkspace(root, owner=WORKER_RESTARTED),
            RecoveryPolicy(),
            WORKER_RESTARTED,
        )
        assert len(report.deleted) == 1
        assert list(root.iterdir()) == []


class TestTheContainerSweepsAtStartup:
    """The wiring, not just the pieces: booting is what reclaims the disk."""

    def test_booting_reclaims_what_the_last_run_left(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        crash_mid_download(FilesystemWorkspace(root, owner=WORKER))
        (root / "debris").mkdir()
        (root / "debris" / "leftover.mp4").write_bytes(b"x" * 128)

        container = build_container(_settings(root, purge_on_start=True))

        assert container.workspace is not None
        assert list(root.iterdir()) == []

    def test_a_deployment_that_shares_its_root_can_refuse_to_purge(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        crash_mid_download(FilesystemWorkspace(root, owner=OTHER_WORKER))

        build_container(_settings(root, purge_on_start=False))

        # Somebody else may still be writing in there; it waits out the lease.
        assert list(root.iterdir()) != []

    def test_no_workspace_is_built_when_nothing_will_write(self, tmp_path: Path) -> None:
        container = build_container(
            _settings(tmp_path / "workspace", purge_on_start=True, download_enabled=False)
        )

        assert container.workspace is None


def _settings(root: Path, *, purge_on_start: bool, download_enabled: bool = True) -> Settings:
    """Return a container configuration pointed at ``root``."""
    return Settings(
        _env_file=None,
        environment=Environment.TESTING,
        database=DatabaseSettings(backend=PersistenceBackend.MEMORY),
        download=DownloadSettings(enabled=download_enabled),
        workspace=WorkspaceSettings(
            root=root,
            min_free_bytes=0,
            purge_on_start=purge_on_start,
            lease_expiry_seconds=3_600.0,
        ),
        logging=LoggingSettings(level=LogLevel.WARNING),
    )


class TestTheDiskFillsUp:
    def test_a_full_device_is_transient_with_a_long_backoff(self) -> None:
        report = classify(InsufficientDiskSpaceError(2_000, 10))

        assert report.kind is FailureKind.TRANSIENT
        assert report.retry_after_seconds == DISK_RETRY_SECONDS
        assert report.code == "insufficient_disk_space"

    def test_a_corrupt_download_is_permanent(self) -> None:
        report = classify(
            IntegrityCheckFailedError("a.mp4", "size mismatch", expected=10, actual=9)
        )

        assert report.kind is FailureKind.PERMANENT
        assert not report.is_retryable

    def test_a_download_that_fills_the_disk_publishes_nothing(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace", owner=WORKER)

        with workspace.lease(label="acquire", reserve_bytes=1_000) as scope:
            with (  # noqa: PT012 - the block is the test
                pytest.raises(InsufficientDiskSpaceError),
                scope.open_artifact(extension="mp4") as writer,
            ):
                writer.write(b"x" * 512)
                swap_handle(writer, _NoRoomLeft())
                writer.write(b"x" * 512)

            assert scope.names() == ()
            assert scope.used_bytes() == 0

    def test_a_truncated_download_is_never_delivered(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace", owner=WORKER)
        expectation = IntegrityExpectation(expected_bytes=4_096)

        with workspace.lease(label="acquire") as scope:
            with (
                pytest.raises(IntegrityCheckFailedError),
                scope.open_artifact(extension="mp4", expect=expectation) as writer,
            ):
                writer.write(b"x" * 2_048)

            assert scope.artifacts() == ()
            assert list(scope.directory().iterdir()) == []


class _Closed:
    """A file handle the operating system already reclaimed."""

    def close(self) -> None:
        return None


class _NoRoomLeft:
    """A file handle on a device that has just filled up."""

    def write(self, data: bytes) -> int:
        del data
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        return None
