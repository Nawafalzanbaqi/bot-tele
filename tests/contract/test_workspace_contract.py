"""Every workspace behaves the same way, or the code above it is lying.

One suite, run against every implementation of
:class:`~mediahub.application.workspace.ports.WorkspacePort`. Today that is the
filesystem adapter; a tmpfs backend
(``docs/architecture/11-storage-strategy.md`` §11.2) joins the parametrisation
and has to pass exactly this.

The properties a workspace must have, and what each one prevents:

===============================  =============================================
Property                         Without it
===============================  =============================================
A lease is private               Two jobs overwrite each other's files
A lease is reclaimed             A small device fills up and stays full
Names are contained              A provider filename writes to ``/etc``
Nothing partial is published     A truncated download is delivered as media
Verification precedes publishing Corrupt bytes reach the destination
Reservations are accounted       Two workers each claim the last gigabyte
Survivors are findable           A crash leaks a directory forever
===============================  =============================================
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.domain.common.fingerprint import Fingerprint
from mediahub.domain.workspace.errors import (
    IntegrityCheckFailedError,
    InvalidArtifactNameError,
    PathEscapesWorkspaceError,
)
from mediahub.domain.workspace.value_objects import IntegrityExpectation
from mediahub.infrastructure.workspace.containment import resolve_within
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.infrastructure.workspace.hashing import StreamingDigest

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspacePort

pytestmark = pytest.mark.integration

PAYLOAD = b"contract payload" * 64


@pytest.fixture(params=["filesystem"])
def workspace(request: pytest.FixtureRequest, tmp_path: Path) -> WorkspacePort:
    """Build the workspace implementation under test."""
    if request.param != "filesystem":  # pragma: no cover - one implementation today
        message = f"unknown workspace implementation: {request.param}"
        raise AssertionError(message)
    return FilesystemWorkspace(tmp_path / "workspace")


def digest(payload: bytes) -> Fingerprint:
    accumulator = StreamingDigest()
    accumulator.update(payload)
    return accumulator.fingerprint()


class TestLeaseIsPrivateAndReclaimed:
    def test_each_lease_gets_its_own_space(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as first, workspace.lease(label="b") as second:
            with first.open_artifact(extension="mp4") as writer:
                writer.write(PAYLOAD)

            assert first.lease_id != second.lease_id
            assert second.names() == ()

    def test_leaving_the_lease_reclaims_everything(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a", reserve_bytes=1_000) as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(PAYLOAD)
            directory = scope.directory()

        assert not directory.exists()
        assert workspace.usage().used_bytes == 0
        assert workspace.usage().reserved_bytes == 0

    def test_reclamation_happens_even_when_the_work_fails(self, workspace: WorkspacePort) -> None:
        directories = []

        with (  # noqa: PT012 - the block is the test
            pytest.raises(RuntimeError),
            workspace.lease(label="a") as scope,
        ):
            directories.append(scope.directory())
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(PAYLOAD)
            message = "the stage blew up"
            raise RuntimeError(message)

        assert not directories[0].exists()


class TestNamesAreContained:
    @pytest.mark.parametrize("name", ["../escape.mp4", "sub/dir.mp4", "..", ".hidden", ""])
    def test_a_hostile_name_never_becomes_a_path(self, workspace: WorkspacePort, name: str) -> None:
        with workspace.lease(label="a") as scope, pytest.raises(InvalidArtifactNameError):
            scope.path_for(name)

    def test_an_absolute_path_is_refused(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope, pytest.raises(InvalidArtifactNameError):
            scope.path_for("/etc/passwd")

    def test_a_path_from_outside_is_not_contained(
        self, workspace: WorkspacePort, tmp_path: Path
    ) -> None:
        with workspace.lease(label="a") as scope:
            assert not scope.contains(tmp_path / "elsewhere.bin")

    def test_generated_names_are_always_usable(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(PAYLOAD)

            assert scope.path_for(writer.name).is_file()


class TestPublishingIsAtomic:
    def test_an_artifact_is_invisible_until_it_is_complete(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(PAYLOAD)
                assert scope.names() == ()

            assert scope.names() == (writer.name,)

    def test_an_abandoned_write_leaves_no_trace(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope:
            with (  # noqa: PT012 - the block is the test
                pytest.raises(RuntimeError),
                scope.open_artifact(extension="mp4") as writer,
            ):
                writer.write(PAYLOAD)
                message = "connection reset"
                raise RuntimeError(message)

            assert scope.names() == ()
            assert scope.used_bytes() == 0


class TestVerificationPrecedesPublishing:
    def test_the_expected_size_must_match(self, workspace: WorkspacePort) -> None:
        expectation = IntegrityExpectation(expected_bytes=len(PAYLOAD) + 1)

        with workspace.lease(label="a") as scope:
            with (
                pytest.raises(IntegrityCheckFailedError),
                scope.open_artifact(extension="mp4", expect=expectation) as writer,
            ):
                writer.write(PAYLOAD)

            assert scope.names() == ()

    def test_the_expected_digest_must_match(self, workspace: WorkspacePort) -> None:
        expectation = IntegrityExpectation(expected_fingerprint=digest(b"other bytes"))

        with workspace.lease(label="a") as scope:
            with (
                pytest.raises(IntegrityCheckFailedError),
                scope.open_artifact(extension="mp4", expect=expectation) as writer,
            ):
                writer.write(PAYLOAD)

            assert scope.names() == ()

    def test_a_matching_artifact_is_published_with_its_digest(
        self, workspace: WorkspacePort
    ) -> None:
        expectation = IntegrityExpectation(
            expected_bytes=len(PAYLOAD), expected_fingerprint=digest(PAYLOAD)
        )

        with workspace.lease(label="a") as scope:
            with scope.open_artifact(extension="mp4", expect=expectation) as writer:
                writer.write(PAYLOAD)

            assert writer.published is not None
            assert writer.published.fingerprint == digest(PAYLOAD)
            assert scope.fingerprint_of(writer.name) == digest(PAYLOAD)

    def test_a_file_from_an_external_tool_can_be_verified(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope:
            scope.path_for("engine.mp4").write_bytes(PAYLOAD)

            reference = scope.verify(
                "engine.mp4",
                expect=IntegrityExpectation(expected_bytes=len(PAYLOAD)),
            )

            assert reference.size_bytes == len(PAYLOAD)

    def test_a_healthy_lease_is_consistent(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(PAYLOAD)

            scope.verify_consistency()


class TestSpaceIsAccounted:
    def test_free_space_never_exceeds_the_budget_headroom(self, workspace: WorkspacePort) -> None:
        assert workspace.free_bytes() == workspace.budget().headroom_bytes

    def test_an_open_reservation_is_visible_to_everyone(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a", reserve_bytes=4_096):
            assert workspace.usage().reserved_bytes == 4_096
            assert workspace.usage().lease_count == 1

        assert workspace.usage().reserved_bytes == 0

    def test_usage_reports_the_bytes_on_disk(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope:
            with scope.open_artifact(extension="mp4") as writer:
                writer.write(PAYLOAD)

            assert workspace.usage().used_bytes >= len(PAYLOAD)


class TestSurvivorsAreFindable:
    def test_a_lease_that_outlives_its_owner_can_be_found_and_reclaimed(
        self, workspace: WorkspacePort, tmp_path: Path
    ) -> None:
        del tmp_path
        context = workspace.lease(label="crashed", reserve_bytes=1_000)
        scope = context.__enter__()
        with scope.open_artifact(extension="mp4") as writer:
            writer.write(PAYLOAD)

        records = workspace.leases_on_disk()
        assert [record.lease_id for record in records] == [scope.lease_id]
        assert records[0].lease is not None

        reclaimed = workspace.discard(scope.lease_id)
        assert reclaimed >= len(PAYLOAD)
        assert workspace.leases_on_disk() == ()

    def test_discarding_something_that_is_gone_is_not_an_error(
        self, workspace: WorkspacePort
    ) -> None:
        assert workspace.discard("0" * 32) == 0


class TestContainmentIsEnforcedInOnePlace:
    def test_resolution_is_the_only_way_out(self, workspace: WorkspacePort) -> None:
        with workspace.lease(label="a") as scope, pytest.raises(PathEscapesWorkspaceError):
            resolve_within(scope.directory(), "../../../etc/passwd")
