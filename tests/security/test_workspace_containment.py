"""T2: nothing a provider names can write outside its lease.

``docs/architecture/14-security-architecture.md`` §14.4 rests on one claim -
there is exactly one function that turns a name into a path. These tests attack
that claim from every direction the threat model lists: traversal, absolute
paths, symlinks, reserved device names, control characters, and a caller trying
to point the sweeper at something outside the root.

Downloaded bytes are untrusted input. So are the filenames that arrive with
them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediahub.domain.workspace.errors import (
    InvalidArtifactNameError,
    PathEscapesWorkspaceError,
)
from mediahub.domain.workspace.policies import FilenamePolicy
from mediahub.infrastructure.workspace import containment
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace

pytestmark = pytest.mark.security

TRAVERSALS = [
    "../escape.mp4",
    "../../etc/passwd",
    "..\\..\\windows\\system32\\config\\sam",
    "sub/dir.mp4",
    "/etc/passwd",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "....//....//etc/passwd",
    "..",
    ".",
]

HOSTILE_NAMES = [
    "con.mp4",
    "PRN.txt",
    "lpt9.bin",
    "null\x00byte.mp4",
    "newline\n.mp4",
    "unicode\u202egpj.exe",
    "-rf.mp4",
    ".hidden",
    "$(whoami).mp4",
    "a;rm -rf /.mp4",
]


@pytest.fixture
def workspace(tmp_path: Path) -> FilesystemWorkspace:
    return FilesystemWorkspace(tmp_path / "workspace")


class TestNamesFromAProvider:
    @pytest.mark.parametrize("name", TRAVERSALS)
    def test_traversal_never_becomes_a_path(
        self, workspace: FilesystemWorkspace, name: str
    ) -> None:
        with workspace.lease(label="job") as scope, pytest.raises(InvalidArtifactNameError):
            scope.path_for(name)

    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_hostile_names_are_refused(self, workspace: FilesystemWorkspace, name: str) -> None:
        with workspace.lease(label="job") as scope, pytest.raises(InvalidArtifactNameError):
            scope.path_for(name)

    @pytest.mark.parametrize("name", [*TRAVERSALS, *HOSTILE_NAMES])
    def test_sanitising_one_always_produces_a_usable_name(self, name: str) -> None:
        policy = FilenamePolicy()

        assert policy.validate(policy.safe_name(name, "mp4"))

    @pytest.mark.parametrize("extension", ["../../sh", "exe\x00", "e" * 200])
    def test_a_hostile_extension_cannot_escape_either(
        self, workspace: FilesystemWorkspace, extension: str
    ) -> None:
        with workspace.lease(label="job") as scope:
            with scope.open_artifact(extension=extension) as writer:
                writer.write(b"payload")

            path = scope.path_for(writer.name)
            assert path.parent == scope.directory().resolve()

    def test_a_hostile_label_cannot_escape_the_root(self, workspace: FilesystemWorkspace) -> None:
        with workspace.lease(label="../../../../etc/cron.d/evil") as scope:
            assert scope.directory().is_relative_to(workspace.root.resolve())


class TestSymlinks:
    def test_a_link_out_of_the_lease_is_not_contained(
        self, workspace: FilesystemWorkspace, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()

        with workspace.lease(label="job") as scope:
            link = scope.directory() / "link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
                pytest.skip("symlinks are not available in this environment")

            assert not scope.contains(link / "payload.mp4")

    def test_a_link_is_never_written_through(
        self, workspace: FilesystemWorkspace, tmp_path: Path
    ) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("original", encoding="utf-8")

        with workspace.lease(label="job") as scope:
            planted = scope.directory() / "planted"
            try:
                planted.symlink_to(target)
            except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
                pytest.skip("symlinks are not available in this environment")

            with pytest.raises(FileExistsError):
                containment.open_exclusive(planted)

            assert target.read_text(encoding="utf-8") == "original"

    def test_a_link_is_not_walked_into_when_measuring_a_lease(
        self, workspace: FilesystemWorkspace, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "huge.bin").write_bytes(b"x" * 4096)

        with workspace.lease(label="job") as scope:
            try:
                (scope.directory() / "link").symlink_to(elsewhere, target_is_directory=True)
            except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
                pytest.skip("symlinks are not available in this environment")

            assert scope.used_bytes() == 0


class TestContainmentFunction:
    @pytest.mark.parametrize(
        "candidate",
        ["..", "../escape.mp4", "../../etc/passwd", "/etc/passwd", "../sibling/../../far"],
    )
    def test_resolution_refuses_anything_outside_the_root(
        self, tmp_path: Path, candidate: str
    ) -> None:
        root = tmp_path / "lease"
        root.mkdir()

        with pytest.raises(PathEscapesWorkspaceError):
            containment.resolve_within(root, candidate)

    @pytest.mark.parametrize("candidate", ["sub/dir.mp4", "....//....//passwd", "a.mp4"])
    def test_resolution_allows_what_genuinely_stays_inside(
        self, tmp_path: Path, candidate: str
    ) -> None:
        # Containment answers "is this inside?", not "is this a name we accept?".
        # The second question belongs to the filename policy, and `path_for`
        # asks both - which is why these are refused there and allowed here.
        root = tmp_path / "lease"
        root.mkdir()

        assert containment.resolve_within(root, candidate).is_relative_to(root.resolve())

    def test_the_root_itself_is_contained(self, tmp_path: Path) -> None:
        root = tmp_path / "lease"
        root.mkdir()

        assert containment.resolve_within(root, root) == root.resolve()

    def test_exclusive_creation_refuses_an_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "already-there"
        path.write_bytes(b"x")

        with pytest.raises(FileExistsError):
            containment.open_exclusive(path)

    def test_a_created_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "new"

        descriptor = containment.open_exclusive(path)
        os.close(descriptor)

        if os.name != "nt":  # pragma: no cover - POSIX permissions only
            assert path.stat().st_mode & 0o777 == 0o600


class TestSweepingIsSafe:
    @pytest.mark.parametrize(
        "identifier",
        ["../../etc", "..", ".", "", "/etc/passwd", "C:\\Windows", "a/b", "a\\b"],
    )
    def test_discard_cannot_be_pointed_outside_the_root(
        self, workspace: FilesystemWorkspace, identifier: str, tmp_path: Path
    ) -> None:
        victim = tmp_path / "victim.txt"
        victim.write_text("original", encoding="utf-8")

        assert workspace.discard(identifier) == 0
        assert victim.exists()

    def test_discard_unlinks_a_link_rather_than_following_it(
        self, workspace: FilesystemWorkspace, tmp_path: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "precious.bin").write_bytes(b"x" * 16)

        try:
            (workspace.root / "planted").symlink_to(elsewhere, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
            pytest.skip("symlinks are not available in this environment")

        workspace.discard("planted")

        assert (elsewhere / "precious.bin").exists()
        assert not (workspace.root / "planted").exists()
