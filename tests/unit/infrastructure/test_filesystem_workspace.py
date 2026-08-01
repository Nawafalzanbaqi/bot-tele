"""The workspace contains what it is given and reclaims it unconditionally.

Disk pressure is exercised through a substituted probe rather than by filling a
real device: a test that needs a full disk is a test nobody runs, and the
behaviour under pressure is the behaviour that matters most.
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.common.errors import EntityNotFoundError
from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm
from mediahub.domain.workspace.enums import DiskState, LeaseState
from mediahub.domain.workspace.errors import (
    InsufficientDiskSpaceError,
    IntegrityCheckFailedError,
    InvalidArtifactNameError,
    LeaseClosedError,
    WorkspaceInconsistentError,
    WorkspaceQuotaExceededError,
)
from mediahub.domain.workspace.policies import DiskPolicy
from mediahub.domain.workspace.value_objects import IntegrityExpectation, LeaseOwner
from mediahub.infrastructure.workspace import manifest
from mediahub.infrastructure.workspace.disk import (
    DiskUsage,
    ReservationLedger,
    WorkspaceLimits,
    is_out_of_space,
)
from mediahub.infrastructure.workspace.filesystem import (
    FilesystemWorkspace,
    _AtomicWriter,
    _FilesystemScope,
)
from mediahub.infrastructure.workspace.hashing import StreamingDigest, digest_of

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
GIGABYTE = 1024**3


class FakeDisk:
    """A device of any size, without one."""

    def __init__(self, *, capacity: int = 100 * GIGABYTE, free: int = 90 * GIGABYTE) -> None:
        self.capacity = capacity
        self.free = free

    def usage(self, path: Path) -> DiskUsage:
        del path
        return DiskUsage(capacity_bytes=self.capacity, free_bytes=self.free)


class MovingClock:
    """A clock that only moves when a test says so."""

    def __init__(self, moment: datetime = NOW) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, seconds: float) -> None:
        self._moment += timedelta(seconds=seconds)


@pytest.fixture
def workspace(tmp_path: Path) -> FilesystemWorkspace:
    return FilesystemWorkspace(tmp_path / "workspace")


def swap_handle(writer: _AtomicWriter, replacement: object) -> None:
    """Give a writer a different file handle, as a failing device would."""
    current = writer._handle
    assert current is not None
    current.close()
    writer._handle = cast("BinaryIO", replacement)


@pytest.fixture(autouse=True)
def _release_abandoned_leases() -> Iterator[None]:
    """Let go of any deliberately crashed lease once a test is finished."""
    yield
    _ABANDONED.clear()


def sha256_of(payload: bytes) -> Fingerprint:
    accumulator = StreamingDigest()
    accumulator.update(payload)
    return accumulator.fingerprint()


class TestLeaseLifecycle:
    def test_lease_directory_exists_while_open(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="probe") as scope:
            assert scope.directory().is_dir()
            assert scope.lease_id

    def test_lease_is_deleted_on_exit(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="probe") as scope:
            directory = scope.directory()
            scope.path_for("a.mp4").write_bytes(b"data")

        assert not directory.exists()
        assert list(workspace.root.iterdir()) == []

    def test_lease_is_deleted_even_when_the_body_raises(
        self, workspace: FilesystemWorkspace
    ) -> None:
        seen: list[Path] = []

        def fail_inside_a_lease() -> None:
            with workspace.lease(label="probe") as scope:
                seen.append(scope.directory())
                scope.path_for("a.mp4").write_bytes(b"data")
                message = "boom"
                raise RuntimeError(message)

        with pytest.raises(RuntimeError):
            fail_inside_a_lease()

        assert not seen[0].exists()

    def test_two_leases_are_isolated(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="one") as first, workspace.lease(label="two") as second:
            first.path_for("a.mp4").write_bytes(b"1")

            assert first.directory() != second.directory()
            assert first.lease_id != second.lease_id
            assert second.names() == ()

    def test_label_is_sanitised_into_the_directory_name(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="../../etc/passwd") as scope:
            lease_directory = scope.directory().parent

            assert lease_directory.parent == workspace.root.resolve()
            assert scope.lease_id in lease_directory.name

    def test_a_released_lease_refuses_to_be_used(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="probe") as scope:
            pass

        with pytest.raises(LeaseClosedError):
            scope.path_for("a.mp4")

    def test_the_manifest_records_ownership_and_state(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="probe", reserve_bytes=1_000) as scope:
            scope.path_for("a.mp4").write_bytes(b"x" * 10)
            scope.remove("a.mp4")
            recorded = manifest.read(scope.directory().parent)

        assert recorded is not None
        assert recorded.owner == workspace.owner
        assert recorded.reserved_bytes == 1_000
        assert str(recorded.id) == scope.lease_id


class TestContainment:
    def test_path_for_stays_inside_the_lease(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            path = scope.path_for("video.mp4")

            assert path.parent == scope.directory().resolve()

    @pytest.mark.parametrize("name", ["../escape.mp4", "sub/dir.mp4", "/abs.mp4", "..", ".hidden"])
    def test_unsafe_names_are_refused(self, workspace: FilesystemWorkspace, name: str) -> None:
        with workspace.lease(label="job") as scope, pytest.raises(InvalidArtifactNameError):
            scope.path_for(name)

    def test_contains_rejects_a_path_outside_the_lease(
        self, workspace: FilesystemWorkspace, tmp_path: Path
    ) -> None:
        with workspace.lease(label="job") as scope:
            assert scope.contains(scope.directory() / "a.mp4")
            assert not scope.contains(tmp_path / "elsewhere.mp4")

    def test_one_lease_cannot_reach_into_another(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="one") as first, workspace.lease(label="two") as second:
            assert not first.contains(second.directory() / "a.mp4")


class TestArtifacts:
    def test_artifact_reports_size_and_role(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("video.mp4").write_bytes(b"x" * 32)

            artifact = scope.artifact("video.mp4", role=ArtifactRole.PRIMARY)

            assert artifact.size_bytes == 32
            assert artifact.role is ArtifactRole.PRIMARY
            assert artifact.lease_id == scope.lease_id

    def test_artifact_requires_the_file_to_exist(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope, pytest.raises(FileNotFoundError):
            scope.artifact("missing.mp4")

    def test_names_and_used_bytes_track_contents(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("a.mp4").write_bytes(b"x" * 10)
            scope.path_for("b.jpg").write_bytes(b"x" * 5)

            assert scope.names() == ("a.mp4", "b.jpg")
            assert scope.used_bytes() == 15
            assert len(scope.artifacts()) == 2

    def test_remove_is_idempotent(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("a.mp4").write_bytes(b"x")

            scope.remove("a.mp4")
            scope.remove("a.mp4")

            assert scope.names() == ()

    def test_a_lease_whose_directory_vanished_reports_nothing(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            shutil.rmtree(scope.directory())

            assert scope.names() == ()
            assert scope.used_bytes() == 0

    def test_the_manifest_is_not_an_artifact(self, workspace: FilesystemWorkspace) -> None:
        # Bookkeeping lives beside the files, never among them.
        with workspace.lease(label="job") as scope:
            assert scope.names() == ()
            assert (scope.directory().parent / manifest.MANIFEST_NAME).is_file()


class TestAtomicWrites:
    def test_an_artifact_appears_only_once_it_is_complete(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(b"x" * 10)
                in_flight = writer.published

                assert scope.names() == ()
                assert in_flight is None

            published = writer.published
            assert scope.names() == (writer.name,)
            assert published is not None
            assert published.size_bytes == 10

    def test_a_failed_download_leaves_nothing_behind(self, workspace: FilesystemWorkspace) -> None:
        def download_that_dies(scope: _FilesystemScope) -> None:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(b"x" * 10)
                message = "connection reset"
                raise RuntimeError(message)

        with workspace.lease(label="job") as scope:
            with pytest.raises(RuntimeError):
                download_that_dies(scope)

            assert scope.names() == ()
            assert scope.used_bytes() == 0
            assert list(scope.directory().iterdir()) == []

    def test_the_generated_name_carries_the_extension(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope, scope.open_artifact(extension="mp4") as writer:
            assert writer.name.endswith(".mp4")

    def test_names_never_come_from_the_caller(self, workspace: FilesystemWorkspace) -> None:
        # There is no parameter to pass a provider's filename through.
        with workspace.lease(label="job") as scope:
            with scope.open_artifact(extension="../../etc/passwd") as writer:
                writer.write(b"x")

            assert "/" not in writer.name
            assert scope.path_for(writer.name).parent == scope.directory().resolve()

    def test_two_writers_in_one_lease_do_not_collide(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            with scope.open_artifact(extension="mp4") as first:
                first.write(b"a" * 4)
            with scope.open_artifact(extension="jpg", role=ArtifactRole.THUMBNAIL) as second:
                second.write(b"b" * 8)

            assert first.name != second.name
            assert sorted(scope.names()) == sorted([first.name, second.name])
            assert scope.artifact(second.name, role=ArtifactRole.THUMBNAIL).size_bytes == 8

    def test_writing_after_the_block_is_refused(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(b"x")

            with pytest.raises(LeaseClosedError):
                writer.write(b"more")

    def test_empty_chunks_are_ignored(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope, scope.open_artifact() as writer:
            assert writer.write(b"") == 0
            assert writer.bytes_written == 0


class TestHashing:
    def test_the_digest_comes_from_the_stream(self, workspace: FilesystemWorkspace) -> None:
        payload = b"the quick brown fox" * 100

        with workspace.lease(label="job") as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(payload)

            published = writer.published
            assert published is not None
            assert published.fingerprint == sha256_of(payload)
            assert scope.fingerprint_of(writer.name) == sha256_of(payload)

    def test_a_file_written_by_an_external_tool_is_hashed_once(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("engine.mp4").write_bytes(b"payload")

            first = scope.fingerprint_of("engine.mp4")
            # The file changes underneath; the cached digest proves it was not
            # read a second time.
            scope.path_for("engine.mp4").write_bytes(b"different")

            assert scope.fingerprint_of("engine.mp4") == first

    def test_hashing_a_missing_artifact_fails(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope, pytest.raises(FileNotFoundError):
            scope.fingerprint_of("missing.mp4")

    def test_a_partial_digest_is_available_mid_stream(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope, scope.open_artifact() as writer:
            writer.write(b"abc")

            assert writer.fingerprint() == sha256_of(b"abc")
            writer.write(b"def")
            assert writer.fingerprint() == sha256_of(b"abcdef")

    def test_digest_of_matches_the_streaming_digest(self, tmp_path: Path) -> None:
        path = tmp_path / "payload.bin"
        payload = b"z" * (2 * 1024 * 1024 + 7)
        path.write_bytes(payload)

        assert digest_of(path) == sha256_of(payload)

    def test_other_algorithms_are_supported(self) -> None:
        accumulator = StreamingDigest(HashAlgorithm.SHA512)
        accumulator.update(b"abc")

        assert accumulator.fingerprint().algorithm is HashAlgorithm.SHA512


class TestVerification:
    def test_a_matching_size_publishes(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            with scope.open_artifact(
                extension="mp4", expect=IntegrityExpectation(expected_bytes=10)
            ) as writer:
                writer.write(b"x" * 10)

            assert scope.names() == (writer.name,)

    def test_a_truncated_download_is_never_published(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            with (
                pytest.raises(IntegrityCheckFailedError),
                scope.open_artifact(
                    extension="mp4", expect=IntegrityExpectation(expected_bytes=10)
                ) as writer,
            ):
                writer.write(b"x" * 9)

            assert scope.names() == ()
            assert list(scope.directory().iterdir()) == []

    def test_corrupt_content_is_never_published(self, workspace: FilesystemWorkspace) -> None:
        expectation = IntegrityExpectation(expected_fingerprint=sha256_of(b"the real thing"))

        with workspace.lease(label="job") as scope:
            with (
                pytest.raises(IntegrityCheckFailedError),
                scope.open_artifact(extension="mp4", expect=expectation) as writer,
            ):
                writer.write(b"something else entirely")

            assert scope.names() == ()

    def test_verify_checks_a_file_an_external_tool_wrote(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("engine.mp4").write_bytes(b"payload")

            reference = scope.verify(
                "engine.mp4",
                expect=IntegrityExpectation(
                    expected_bytes=7, expected_fingerprint=sha256_of(b"payload")
                ),
            )

            assert reference.size_bytes == 7
            assert reference.fingerprint == sha256_of(b"payload")

    def test_verify_rejects_a_size_mismatch(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("engine.mp4").write_bytes(b"payload")

            with pytest.raises(IntegrityCheckFailedError):
                scope.verify("engine.mp4", expect=IntegrityExpectation(expected_bytes=999))

    def test_verify_without_expectations_still_returns_a_reference(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("engine.mp4").write_bytes(b"payload")

            reference = scope.verify("engine.mp4")

            assert reference.size_bytes == 7
            assert reference.fingerprint is None

    def test_verify_requires_the_file_to_exist(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope, pytest.raises(FileNotFoundError):
            scope.verify("missing.mp4")

    def test_consistency_accepts_a_healthy_lease(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("a.mp4").write_bytes(b"x")

            scope.verify_consistency()

    def test_consistency_notices_a_vanished_directory(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            shutil.rmtree(scope.directory())

            with pytest.raises(WorkspaceInconsistentError):
                scope.verify_consistency()

    def test_consistency_refuses_a_released_lease(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job") as scope:
            pass

        with pytest.raises(LeaseClosedError):
            scope.verify_consistency()


class TestDiskBudget:
    def test_reservation_beyond_free_space_is_refused(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", probe=FakeDisk(capacity=4 * GIGABYTE, free=4 * GIGABYTE)
        )

        with (
            pytest.raises(InsufficientDiskSpaceError) as excinfo,
            workspace.lease(label="huge", reserve_bytes=8 * GIGABYTE),
        ):
            pass

        assert excinfo.value.requested_bytes == 8 * GIGABYTE

    def test_reserve_floor_reduces_reported_free_space(self, tmp_path: Path) -> None:
        generous = FilesystemWorkspace(tmp_path / "a")
        strict = FilesystemWorkspace(tmp_path / "b", limits=WorkspaceLimits(min_free_bytes=2**40))

        assert strict.free_bytes() < generous.free_bytes()

    def test_a_small_reservation_succeeds(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="job", reserve_bytes=1024) as scope:
            assert scope.directory().is_dir()
            assert scope.reserved_bytes == 1024

    def test_an_open_reservation_is_held_against_headroom(self, tmp_path: Path) -> None:
        # The point of accounting: the second caller sees the first one's promise.
        workspace = FilesystemWorkspace(
            tmp_path / "ws", probe=FakeDisk(capacity=20 * GIGABYTE, free=10 * GIGABYTE)
        )

        with workspace.lease(label="first", reserve_bytes=8 * GIGABYTE):
            assert workspace.free_bytes() == 2 * GIGABYTE

            with (
                pytest.raises(InsufficientDiskSpaceError),
                workspace.lease(label="second", reserve_bytes=4 * GIGABYTE),
            ):
                pass

    def test_written_bytes_stop_being_a_promise(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", probe=FakeDisk(capacity=20 * GIGABYTE, free=10 * GIGABYTE)
        )

        with workspace.lease(label="job", reserve_bytes=1_000) as scope:
            assert workspace.usage().reserved_bytes == 1_000

            with scope.open_artifact(extension="mp4") as writer:
                writer.write(b"x" * 400)

            assert workspace.usage().reserved_bytes == 600

    def test_a_released_lease_returns_its_reservation(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", probe=FakeDisk(capacity=20 * GIGABYTE, free=10 * GIGABYTE)
        )

        with workspace.lease(label="job", reserve_bytes=8 * GIGABYTE):
            pass

        assert workspace.free_bytes() == 10 * GIGABYTE
        assert workspace.usage().lease_count == 0

    def test_usage_reports_what_is_held(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "ws", probe=FakeDisk(free=50 * GIGABYTE))

        with workspace.lease(label="job", reserve_bytes=1_000) as scope:
            scope.path_for("a.mp4").write_bytes(b"x" * 100)
            usage = workspace.usage()

        assert usage.lease_count == 1
        assert usage.used_bytes >= 100
        assert DiskPolicy().state_of(usage.budget) is DiskState.HEALTHY

    def test_a_critical_device_refuses_new_work(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", probe=FakeDisk(capacity=100 * GIGABYTE, free=GIGABYTE)
        )

        with (
            pytest.raises(InsufficientDiskSpaceError),
            workspace.lease(label="job", reserve_bytes=1),
        ):
            pass

    def test_a_lease_without_a_reservation_is_not_admitted_against_the_disk(
        self, tmp_path: Path
    ) -> None:
        # Reading metadata into a lease must not be blocked by a tight device;
        # only callers that ask for space are held to the admission rules.
        workspace = FilesystemWorkspace(tmp_path / "ws", probe=FakeDisk(free=1))

        with workspace.lease(label="probe") as scope:
            assert scope.directory().is_dir()


class TestQuotas:
    def test_a_reservation_beyond_the_lease_ceiling_is_refused(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", limits=WorkspaceLimits(max_lease_bytes=1_000)
        )

        with (
            pytest.raises(WorkspaceQuotaExceededError) as excinfo,
            workspace.lease(label="job", reserve_bytes=2_000),
        ):
            pass

        assert excinfo.value.scope == "lease"

    def test_writing_past_the_lease_ceiling_is_refused(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", limits=WorkspaceLimits(max_lease_bytes=100)
        )

        with workspace.lease(label="job") as scope:
            with (
                pytest.raises(WorkspaceQuotaExceededError),
                scope.open_artifact(extension="mp4") as writer,
            ):
                writer.write(b"x" * 101)

            assert scope.names() == ()

    def test_a_lease_that_cannot_record_itself_is_not_left_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A lease with no manifest is unrecoverable debris; better to fail the
        # lease than to leak a directory nothing will ever claim.
        workspace = FilesystemWorkspace(tmp_path / "ws")

        def refuse(directory: Path, lease: object) -> None:
            del directory, lease
            message = "read-only file system"
            raise OSError(message)

        monkeypatch.setattr(manifest, "write", refuse)

        with (
            pytest.raises(OSError, match="read-only"),
            workspace.lease(label="job"),
        ):
            pass

        assert list(workspace.root.iterdir()) == []

    def test_an_adopted_lease_counts_what_is_already_in_it(self, tmp_path: Path) -> None:
        # A resumed download must not get a fresh allowance: the bytes the
        # previous attempt wrote are still on the device.
        root = tmp_path / "ws"
        crashed = FilesystemWorkspace(root, owner=LeaseOwner(identity="pi:0", process_id=1))
        leaked = _leak_a_lease(crashed)  # leaves ten bytes behind
        restarted = FilesystemWorkspace(
            root,
            owner=LeaseOwner(identity="pi:0", process_id=2),
            limits=WorkspaceLimits(max_lease_bytes=15),
        )

        with (
            restarted.reopen(leaked) as scope,
            pytest.raises(WorkspaceQuotaExceededError),
            scope.open_artifact() as writer,
        ):
            writer.write(b"x" * 10)

    def test_the_workspace_ceiling_counts_every_lease(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", limits=WorkspaceLimits(max_total_bytes=1_000)
        )

        with workspace.lease(label="first") as scope:
            scope.path_for("a.mp4").write_bytes(b"x" * 900)

            with (
                pytest.raises(WorkspaceQuotaExceededError) as excinfo,
                workspace.lease(label="second", reserve_bytes=500),
            ):
                pass

        assert excinfo.value.scope == "workspace"


class FullDisk:
    """A file handle on a device with no room left.

    Substituted for the real one so the translation of ``ENOSPC`` into a typed,
    transient failure is exercised without a full device - and the temporary
    file is still really there, so the cleanup is real too.
    """

    def __init__(self, *, fail_on_flush: bool = False) -> None:
        self.fail_on_flush = fail_on_flush
        self.closed = False

    def _refuse(self) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    def write(self, data: bytes) -> int:
        if self.fail_on_flush:
            return len(data)
        self._refuse()
        return 0

    def flush(self) -> None:
        if self.fail_on_flush:
            self._refuse()

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        self.closed = True


class TestOutOfSpace:
    @pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT, errno.EFBIG])
    def test_a_device_or_quota_with_no_room_is_recognised(self, code: int) -> None:
        assert is_out_of_space(OSError(code, "no room"))

    def test_other_failures_are_left_alone(self) -> None:
        assert not is_out_of_space(OSError(errno.EACCES, "Permission denied"))

    def test_a_full_device_mid_write_becomes_a_workspace_failure(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            with (  # noqa: PT012 - the block is the test
                pytest.raises(InsufficientDiskSpaceError),
                scope.open_artifact(extension="mp4") as writer,
            ):
                swap_handle(writer, FullDisk())
                writer.write(b"x" * 10)

            assert scope.names() == ()
            assert list(scope.directory().iterdir()) == []

    def test_a_full_device_on_the_final_flush_publishes_nothing(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            with (  # noqa: PT012 - the block is the test
                pytest.raises(InsufficientDiskSpaceError),
                scope.open_artifact(extension="mp4") as writer,
            ):
                swap_handle(writer, FullDisk(fail_on_flush=True))
                writer.write(b"x" * 10)

            assert scope.names() == ()
            assert list(scope.directory().iterdir()) == []

    def test_a_write_failure_that_is_not_about_space_is_passed_through(
        self, workspace: FilesystemWorkspace
    ) -> None:
        class Broken(FullDisk):
            def _refuse(self) -> None:
                raise OSError(errno.EACCES, "Permission denied")

        with workspace.lease(label="job") as scope:
            with (  # noqa: PT012
                pytest.raises(OSError, match="Permission denied"),
                scope.open_artifact(extension="mp4") as writer,
            ):
                swap_handle(writer, Broken())
                writer.write(b"x")

            assert scope.names() == ()


class TestReservationLedger:
    def test_settling_is_absolute_not_incremental(self) -> None:
        ledger = ReservationLedger()
        ledger.reserve("a", 1_000)

        ledger.settle("a", 400)
        ledger.settle("a", 400)

        assert ledger.outstanding_bytes() == 600

    def test_settling_an_unknown_lease_is_harmless(self) -> None:
        ledger = ReservationLedger()

        ledger.settle("ghost", 10)

        assert ledger.outstanding_bytes() == 0

    def test_releasing_twice_is_harmless(self) -> None:
        ledger = ReservationLedger()
        ledger.reserve("a", 10)

        ledger.release("a")
        ledger.release("a")

        assert ledger.count() == 0

    def test_concurrent_reservations_all_land(self) -> None:
        ledger = ReservationLedger()

        def reserve(index: int) -> None:
            ledger.reserve(f"lease-{index}", 100)

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert ledger.count() == 32
        assert ledger.outstanding_bytes() == 3_200


class TestConcurrentLeases:
    def test_leases_opened_from_several_threads_stay_separate(
        self, workspace: FilesystemWorkspace
    ) -> None:
        results: list[tuple[str, tuple[str, ...]]] = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def acquire(index: int) -> None:
            barrier.wait()
            with workspace.lease(label=f"job-{index}", reserve_bytes=1_000) as scope:
                with scope.open_artifact(extension="mp4") as writer:
                    writer.write(bytes([index]) * (index + 1))
                with lock:
                    results.append((scope.lease_id, tuple(scope.names())))

        threads = [threading.Thread(target=acquire, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len({lease_id for lease_id, _ in results}) == 8
        assert all(len(names) == 1 for _, names in results)
        assert list(workspace.root.iterdir()) == []
        assert workspace.usage().reserved_bytes == 0


class TestRecovery:
    def test_a_surviving_lease_is_listed_with_its_owner(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        crashed = FilesystemWorkspace(root, owner=LeaseOwner(identity="pi:worker:0", process_id=1))
        leaked = _leak_a_lease(crashed)

        restarted = FilesystemWorkspace(
            root, owner=LeaseOwner(identity="pi:worker:0", process_id=2)
        )
        records = restarted.leases_on_disk()

        assert len(records) == 1
        assert records[0].lease_id == leaked
        assert records[0].lease is not None
        assert records[0].lease.owner.process_id == 1
        assert records[0].lease.state is LeaseState.ACTIVE
        assert records[0].used_bytes >= 10

    def test_a_directory_with_no_manifest_is_reported_unclaimed(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        (root / "debris").mkdir()
        (root / "debris" / "leftover.mp4").write_bytes(b"x")

        records = workspace.leases_on_disk()

        assert [record.lease for record in records] == [None]
        assert records[0].lease_id == "debris"

    def test_a_stray_file_is_reported_too(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        (root / "stray.tmp").write_bytes(b"x")

        records = workspace.leases_on_disk()

        assert records[0].lease_id == "stray.tmp"
        assert records[0].used_bytes == 1

    def test_discard_reclaims_a_lease(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        leaked = _leak_a_lease(workspace)

        reclaimed = workspace.discard(leaked)

        assert reclaimed >= 10
        assert list(root.iterdir()) == []

    def test_discard_is_idempotent(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "ws")
        leaked = _leak_a_lease(workspace)

        workspace.discard(leaked)

        assert workspace.discard(leaked) == 0

    def test_discard_removes_a_stray_file(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        (root / "stray.tmp").write_bytes(b"xx")

        assert workspace.discard("stray.tmp") == 2
        assert list(root.iterdir()) == []

    def test_reopening_adopts_the_lease(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        crashed = FilesystemWorkspace(root, owner=LeaseOwner(identity="pi:worker:0", process_id=1))
        leaked = _leak_a_lease(crashed)
        restarted = FilesystemWorkspace(
            root, owner=LeaseOwner(identity="pi:worker:0", process_id=2)
        )

        with restarted.reopen(leaked) as scope:
            assert scope.lease_id == leaked
            assert len(scope.names()) == 1
            assert scope.used_bytes() == 10
            recorded = manifest.read(scope.directory().parent)
            assert recorded is not None
            assert recorded.owner.process_id == 2

        assert list(root.iterdir()) == []

    def test_reopening_something_that_is_not_there_fails(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with (
            pytest.raises(EntityNotFoundError),
            workspace.reopen("0" * 32),
        ):
            pass

    def test_reopening_a_directory_with_no_manifest_fails(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        (root / "debris").mkdir()

        with (
            pytest.raises(EntityNotFoundError),
            workspace.reopen("debris"),
        ):
            pass

    def test_an_unreadable_manifest_reads_as_unclaimed(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        leaked = _leak_a_lease(workspace)
        directory = next(entry for entry in root.iterdir() if leaked in entry.name)
        (directory / manifest.MANIFEST_NAME).write_text("{ this is not json", encoding="utf-8")

        assert manifest.read(directory) is None
        assert workspace.leases_on_disk()[0].lease is None

    def test_a_manifest_from_another_version_is_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        leaked = _leak_a_lease(workspace)
        directory = next(entry for entry in root.iterdir() if leaked in entry.name)
        (directory / manifest.MANIFEST_NAME).write_text('{"version": 99}', encoding="utf-8")

        assert manifest.read(directory) is None

    def test_a_manifest_that_fails_domain_validation_is_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        leaked = _leak_a_lease(workspace)
        directory = next(entry for entry in root.iterdir() if leaked in entry.name)
        path = directory / manifest.MANIFEST_NAME
        path.write_text(
            path.read_text(encoding="utf-8").replace(leaked, "not-a-lease-id"),
            encoding="utf-8",
        )

        assert manifest.read(directory) is None

    def test_recovery_ignores_a_root_that_is_not_there(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "ws")
        shutil.rmtree(workspace.root)

        assert workspace.leases_on_disk() == ()
        assert workspace.discard("anything") == 0

    @pytest.mark.parametrize("identifier", ["", ".", "..", "../escape", "a/b", "a\\b"])
    def test_discard_refuses_to_be_pointed_outside_the_root(
        self, workspace: FilesystemWorkspace, identifier: str
    ) -> None:
        assert workspace.discard(identifier) == 0

    def test_the_age_of_a_directory_comes_from_the_filesystem(self, tmp_path: Path) -> None:
        # Compared against a file's mtime, so it must be measured with the same
        # clock the filesystem uses, not with an injected one.
        root = tmp_path / "ws"
        workspace = FilesystemWorkspace(root)
        leaked = _leak_a_lease(workspace)
        directory = next(entry for entry in root.iterdir() if leaked in entry.name)
        long_ago = directory.stat().st_mtime - 7_200
        os.utime(directory, (long_ago, long_ago))

        assert workspace.leases_on_disk()[0].age_seconds >= 7_000

    def test_releasing_a_lease_twice_reclaims_nothing_further(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with workspace.lease(label="job") as scope:
            scope.path_for("a.mp4").write_bytes(b"x" * 10)

        assert scope.close() == 0

    def test_a_workspace_ceiling_stays_out_of_the_way_when_there_is_room(
        self, tmp_path: Path
    ) -> None:
        workspace = FilesystemWorkspace(
            tmp_path / "ws", limits=WorkspaceLimits(max_total_bytes=10 * 1024**2)
        )

        with workspace.lease(label="job", reserve_bytes=1_000) as scope:
            assert scope.reserved_bytes == 1_000

    def test_a_lease_records_the_clock_it_was_given(self, tmp_path: Path) -> None:
        clock = MovingClock()
        workspace = FilesystemWorkspace(tmp_path / "ws", clock=clock)
        leaked = _leak_a_lease(workspace)
        directory = next(entry for entry in workspace.root.iterdir() if leaked in entry.name)

        recorded = manifest.read(directory)

        assert recorded is not None
        assert recorded.created_at == NOW


_ABANDONED: list[object] = []
"""Keeps abandoned lease contexts alive.

Dropping the reference would let the garbage collector close the context
manager and tidy the lease away - which is exactly what a crashed process does
*not* do, and the whole point of these tests.
"""


def _leak_a_lease(workspace: FilesystemWorkspace) -> str:
    """Open a lease, half-fill it and abandon it, as a crash would."""
    context = workspace.lease(label="crashed", reserve_bytes=1_000)
    _ABANDONED.append(context)
    scope = context.__enter__()
    with scope.open_artifact(extension="mp4") as writer:
        writer.write(b"x" * 10)
    # Deliberately never leaving the context: the process "died" here.
    return scope.lease_id
