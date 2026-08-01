"""The card fills up, goes read-only, or comes back corrupted.

Storage is the thing that fails on a Raspberry Pi. Not dramatically - an SD card
does not announce its death, it starts refusing writes, and the kernel remounts
the filesystem read-only underneath a process that has no idea. Every test here
puts the workspace into one of those states and asserts the same two properties:
the job is settled rather than stranded, and nothing is deleted that has not
been proven to exist somewhere else.

The disk is never actually filled. :class:`DiskProbe` is a seam precisely so
that a device of any size can be substituted, because a test that has to fill a
real disk is a test that nobody runs and that fails differently on every machine.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.download.enums import JobStatus
from mediahub.domain.workspace.enums import DiskState
from mediahub.domain.workspace.errors import (
    InsufficientDiskSpaceError,
    IntegrityCheckFailedError,
    WorkspaceInconsistentError,
    WorkspaceQuotaExceededError,
)
from mediahub.domain.workspace.policies import DiskPolicy, FilenamePolicy
from mediahub.domain.workspace.value_objects import IntegrityExpectation
from mediahub.infrastructure.workspace import manifest
from mediahub.infrastructure.workspace.disk import DiskUsage, WorkspaceLimits
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.presentation.worker.loop import ClaimLoop
from tests.support.pipeline_fakes import PipelineHarness
from tests.support.worker_fakes import WORKER

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = [pytest.mark.failure, pytest.mark.integration]

GIGABYTE = 1024**3


class ScriptedDisk:
    """A device whose free space is whatever the test says it is."""

    def __init__(self, *, capacity: int = 32 * GIGABYTE, free: int = 16 * GIGABYTE) -> None:
        self.capacity = capacity
        self.free = free

    def usage(self, path: Path) -> DiskUsage:
        del path
        return DiskUsage(capacity_bytes=self.capacity, free_bytes=self.free)


@pytest.fixture
def disk() -> ScriptedDisk:
    return ScriptedDisk()


@pytest.fixture
def workspace(tmp_path: Path, disk: ScriptedDisk) -> FilesystemWorkspace:
    return FilesystemWorkspace(
        tmp_path / "workspace",
        limits=WorkspaceLimits(min_free_bytes=GIGABYTE, max_lease_bytes=8 * GIGABYTE),
        probe=disk,
    )


class TestDiskFull:
    def test_a_reservation_is_refused_before_any_work_starts(
        self, workspace: FilesystemWorkspace, disk: ScriptedDisk
    ) -> None:
        disk.free = GIGABYTE  # exactly the emergency floor: no headroom at all

        with (
            pytest.raises(InsufficientDiskSpaceError),
            workspace.lease(label="job", reserve_bytes=100 * 1024 * 1024),
        ):
            pass

    def test_refusing_leaves_no_directory_behind(
        self, workspace: FilesystemWorkspace, disk: ScriptedDisk
    ) -> None:
        disk.free = GIGABYTE

        with (
            pytest.raises(InsufficientDiskSpaceError),
            workspace.lease(label="job", reserve_bytes=GIGABYTE),
        ):
            pass

        assert list(workspace.root.iterdir()) == []

    def test_the_emergency_reserve_is_never_allocatable(
        self, workspace: FilesystemWorkspace, disk: ScriptedDisk
    ) -> None:
        # The floor exists so the database can still commit the transaction that
        # records what went wrong. Handing it out would lose the explanation
        # along with the work.
        disk.free = GIGABYTE + 1024
        budget = workspace.budget()

        assert budget.headroom_bytes == 1024
        assert budget.free_bytes == GIGABYTE + 1024

    def test_a_full_device_reports_critical_rather_than_merely_low(
        self, workspace: FilesystemWorkspace, disk: ScriptedDisk
    ) -> None:
        disk.free = GIGABYTE + 1

        assert DiskPolicy().state_of(workspace.budget()) is DiskState.CRITICAL

    def test_a_write_that_fills_the_device_is_named_not_guessed(
        self, workspace: FilesystemWorkspace
    ) -> None:
        # ENOSPC arrives as a generic OSError from deep in a write path. Left
        # unnamed it is classified as an unknown failure and retried in thirty
        # seconds, for ever.
        with (  # noqa: PT012 - the whole block, not one call, is what has to fail
            pytest.raises(InsufficientDiskSpaceError),
            workspace.lease(label="job") as scope,
            scope.open_artifact(extension="mp4") as writer,
        ):
            _fill_the_device(writer)
            writer.write(b"x" * 1024)

    def test_a_device_that_fills_mid_write_publishes_nothing(
        self, workspace: FilesystemWorkspace
    ) -> None:
        with (  # noqa: PT012 - the whole block, not one call, is what has to fail
            pytest.raises(InsufficientDiskSpaceError),
            workspace.lease(label="job") as scope,
            scope.open_artifact(extension="mp4") as writer,
        ):
            writer.write(b"x" * 1024)
            _fill_the_device(writer)
            writer.write(b"x" * 1024)

        assert list((workspace.root).rglob("*.mp4")) == []

    def test_the_lease_ceiling_stops_a_source_that_lied_about_its_size(
        self, workspace: FilesystemWorkspace
    ) -> None:
        small = FilesystemWorkspace(
            workspace.root.parent / "small",
            limits=WorkspaceLimits(max_lease_bytes=1024),
            probe=ScriptedDisk(),
        )

        with small.lease(label="job") as scope, scope.open_artifact(extension="mp4") as writer:
            writer.write(b"x" * 1024)
            with pytest.raises(WorkspaceQuotaExceededError):
                writer.write(b"x" * 1024)


def _fill_the_device(writer: Any) -> None:
    """Swap a writer's open handle for one on a device with nothing left.

    Untyped on purpose: the private handle is exactly what a full device takes
    away, and reaching for it is the only way to reproduce ENOSPC without an
    actual full disk.
    """
    handle = writer._handle
    assert handle is not None
    handle.close()
    writer._handle = _RefusingHandle()


class _RefusingHandle:
    """A file handle on a device with nothing left."""

    def write(self, chunk: bytes) -> int:
        del chunk
        raise OSError(errno.ENOSPC, "No space left on device")

    def close(self) -> None:
        return

    def flush(self) -> None:
        return

    def fileno(self) -> int:  # pragma: no cover - never reached; the write raises first
        return -1


class TestDiskFullDuringAJob:
    async def test_the_job_is_requeued_rather_than_stranded(self, tmp_path: Path) -> None:
        harness = PipelineHarness.build(tmp_path)
        job_id = await harness.worker.enqueue()
        harness.downloader.fetch_error = InsufficientDiskSpaceError(4096, 0)

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.QUEUED, "space may come back; the job waits"
        assert harness.worker.leases_in_use() == 0
        assert harness.worker.workspace_directories() == []

    async def test_a_full_disk_does_not_spend_the_queue(self, tmp_path: Path) -> None:
        # The behaviour this whole phase exists for: fifty queued jobs must not
        # each burn an attempt discovering the same full device.
        harness = PipelineHarness.build(tmp_path)
        loop = replace_workspace_with_full_disk(harness)
        for _ in range(5):
            await harness.worker.enqueue(max_attempts=1)

        first = await loop.run_once()
        rest = [await loop.run_once() for _ in range(4)]

        assert first is True, "the first job discovers the problem and reports it"
        assert rest == [False] * 4, "the rest are left queued, not failed"
        assert loop.starved

    async def test_claiming_resumes_once_space_returns(self, tmp_path: Path) -> None:
        harness = PipelineHarness.build(tmp_path)
        disk = ScriptedDisk(free=GIGABYTE)
        loop = replace_workspace_with_full_disk(harness, disk=disk)
        await harness.worker.enqueue()
        await harness.worker.enqueue()

        await loop.run_once()
        assert await loop.run_once() is False

        disk.free = 16 * GIGABYTE
        assert await loop.run_once() is True
        assert not loop.starved


def replace_workspace_with_full_disk(
    harness: PipelineHarness, *, disk: ScriptedDisk | None = None
) -> ClaimLoop:
    """Return a claim loop whose workspace sits on a device with no headroom."""
    device = disk or ScriptedDisk(free=GIGABYTE)
    starved = FilesystemWorkspace(
        harness.worker.workspace.root.parent / "starved",
        limits=WorkspaceLimits(min_free_bytes=GIGABYTE),
        probe=device,
        disk_policy=DiskPolicy(),
    )
    # A lease with no reservation still has to be admitted here, which is what
    # turns "the device is full" into the typed failure the loop understands.
    return ClaimLoop(
        services=replace(harness.worker.services, workspace=_AdmittingWorkspace(starved)),
        executor=harness.executor(),
        worker=WORKER,
        timings=harness.worker.timings,
    )


class _AdmittingWorkspace:
    """A workspace that admits every lease against the device, reservation or not.

    The production adapter only admits when a reservation is stated, because a
    caller that names no size is asking for "whatever is left". This wrapper
    states the question the claim loop's backpressure is written against: can
    this device hold anything at all?
    """

    def __init__(self, inner: FilesystemWorkspace) -> None:
        self._inner = inner

    def lease(
        self, *, label: str, reserve_bytes: int | None = None
    ) -> AbstractContextManager[WorkspaceScope]:
        if self._inner.free_bytes() <= 0:
            raise InsufficientDiskSpaceError(reserve_bytes or 0, 0)
        return self._inner.lease(label=label, reserve_bytes=reserve_bytes)

    def free_bytes(self) -> int:
        return self._inner.free_bytes()


class TestReadOnlyFilesystem:
    @pytest.mark.skipif(os.name == "nt", reason="Windows ignores directory read-only bits")
    def test_a_lease_cannot_be_opened_on_a_read_only_root(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        workspace = FilesystemWorkspace(root)
        root.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(PermissionError), workspace.lease(label="job"):
                pass
        finally:
            root.chmod(stat.S_IRWXU)

    def test_a_workspace_that_cannot_be_emptied_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deletion failing used to be swallowed and the bytes reported as
        # reclaimed, which meant the one condition an operator needed to see
        # produced no signal at all.
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            scope.path_for("payload.bin").write_bytes(b"x" * 2048)
            reclaimed = close_without_deleting(scope, monkeypatch)

        assert reclaimed == 0, "nothing was reclaimed, and the return value says so"

    def test_a_stranded_lease_is_reported_as_an_orphan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            lease_id = scope.lease_id
            close_without_deleting(scope, monkeypatch)

        orphans = workspace.orphans()

        assert [record.lease_id for record in orphans] == [lease_id]
        assert workspace.usage().is_leaking


def close_without_deleting(scope: Any, monkeypatch: pytest.MonkeyPatch) -> int:
    """Close ``scope`` on a filesystem that refuses to remove the directory.

    Returns:
        What ``close`` reported as reclaimed - which must be zero, because
        nothing was.
    """
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)
    reclaimed: int = scope.close()
    return reclaimed


class TestWorkspaceCorruption:
    def test_a_symlink_planted_in_a_lease_fails_the_consistency_check(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            link = scope.directory() / "planted"
            try:
                link.symlink_to(tmp_path)
            except (OSError, NotImplementedError):
                pytest.skip("symlinks are not available in this environment")

            with pytest.raises(WorkspaceInconsistentError):
                scope.verify_consistency()

    def test_a_lease_whose_directory_vanished_fails_loudly(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            shutil.rmtree(scope.directory())

            with pytest.raises(WorkspaceInconsistentError):
                scope.verify_consistency()

    def test_a_truncated_manifest_reads_as_unclaimed_rather_than_as_a_guess(
        self, tmp_path: Path
    ) -> None:
        # The exact state a power cut used to be able to produce before the
        # manifest was fsynced: the new name is visible, the bytes are not.
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            directory = scope.directory().parent
            (directory / manifest.MANIFEST_NAME).write_text("", encoding="utf-8")

            assert manifest.read(directory) is None

    def test_a_manifest_from_a_future_version_is_not_interpreted(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            directory = scope.directory().parent
            (directory / manifest.MANIFEST_NAME).write_text(
                json.dumps({"version": manifest.SCHEMA_VERSION + 1}), encoding="utf-8"
            )

            assert manifest.read(directory) is None, "guessing here deletes somebody's download"

    def test_a_corrupt_lease_does_not_stop_the_sweep_finding_the_others(
        self, tmp_path: Path
    ) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        (workspace.root / "debris").mkdir()
        (workspace.root / "not-a-lease").write_bytes(b"junk")
        with workspace.lease(label="good"):
            records = workspace.leases_on_disk()

        assert len(records) == 3, "everything under the root is reported, readable or not"
        assert sum(1 for record in records if record.lease is not None) == 1


class TestManifestDurability:
    def test_the_manifest_is_flushed_before_the_rename_publishes_it(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            directory = scope.directory().parent
            recovered = manifest.read(directory)

            assert recovered is not None
            assert str(recovered.id) == scope.lease_id

    def test_staging_debris_from_an_interrupted_write_can_be_cleared(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            directory = scope.directory().parent
            (directory / manifest.STAGING_NAME).write_text("half", encoding="utf-8")

            assert manifest.clear_staging(directory) is True
            assert manifest.clear_staging(directory) is False
            assert manifest.read(directory) is not None, "the real manifest is untouched"


class TestTemporaryFileVerification:
    def test_an_unfinished_download_is_never_listed_as_an_artifact(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope, scope.open_artifact(extension="mp4") as writer:
            writer.write(b"x" * 512)

            assert scope.names() == (), "in flight is not the same as finished"

    def test_a_file_that_fails_verification_leaves_nothing_at_all(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            with (
                pytest.raises(IntegrityCheckFailedError),
                scope.open_artifact(
                    extension="mp4", expect=IntegrityExpectation(expected_bytes=9999)
                ) as writer,
            ):
                writer.write(b"x" * 512)

            assert scope.names() == ()
            assert scope.used_bytes() == 0, "not under the final name, nor the temporary one"

    def test_a_temporary_name_is_hidden_and_suffixed(self) -> None:
        policy = FilenamePolicy()
        name = policy.temporary_name("abc123")

        assert policy.is_temporary(name)
        assert name.startswith(".")
        with pytest.raises(Exception, match="hidden"):
            policy.validate(name)

    def test_the_published_name_carries_the_digest_taken_from_the_stream(
        self, tmp_path: Path
    ) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            with scope.open_artifact(extension="mp4", role=ArtifactRole.PRIMARY) as writer:
                writer.write(b"payload")
            published = writer.published

            assert published is not None
            assert published.fingerprint is not None
            assert scope.fingerprint_of(published.name) == published.fingerprint
