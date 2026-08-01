"""Filenames that reach the filesystem are generated, and hostile ones refused."""

from __future__ import annotations

import pytest

from mediahub.domain.workspace.errors import InvalidArtifactNameError
from mediahub.domain.workspace.policies import FilenamePolicy

pytestmark = pytest.mark.unit


@pytest.fixture
def policy() -> FilenamePolicy:
    return FilenamePolicy()


class TestValidate:
    @pytest.mark.parametrize("name", ["video.mp4", "abc123.m4a", "a-b_c.1.webm", "x"])
    def test_accepts_safe_names(self, policy: FilenamePolicy, name: str) -> None:
        assert policy.validate(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            ".",
            "..",
            "../escape.mp4",
            "..\\escape.mp4",
            "/absolute.mp4",
            "sub/dir.mp4",
            "sub\\dir.mp4",
            ".hidden",
            "with space.mp4",
            "semi;colon.mp4",
            "dollar$(whoami).mp4",
            "quote'.mp4",
            "pipe|.mp4",
            "null\x00byte.mp4",
            "newline\n.mp4",
            "unicode\u202e.mp4",
            "con.mp4",
            "PRN.txt",
            "com1.mp4",
        ],
    )
    def test_rejects_hostile_names(self, policy: FilenamePolicy, name: str) -> None:
        with pytest.raises(InvalidArtifactNameError):
            policy.validate(name)

    def test_rejects_an_overlong_name(self, policy: FilenamePolicy) -> None:
        with pytest.raises(InvalidArtifactNameError):
            policy.validate("a" * 500 + ".mp4")


class TestSafeName:
    @pytest.mark.parametrize(
        ("stem", "extension"),
        [
            ("../../etc/passwd", "mp4"),
            ("a b;c$(d)", "mp4"),
            ("видео", "mp4"),
            ("\u202eexe.mp4", "mp4"),
            ("con", "mp4"),
            ("", ""),
            ("...", "..."),
        ],
    )
    def test_output_is_always_valid(
        self, policy: FilenamePolicy, stem: str, extension: str
    ) -> None:
        generated = policy.safe_name(stem, extension)

        assert policy.validate(generated) == generated

    def test_keeps_recognisable_content(self, policy: FilenamePolicy) -> None:
        assert policy.safe_name("abc123", "mp4") == "abc123.mp4"

    def test_replaces_rather_than_deletes(self, policy: FilenamePolicy) -> None:
        # Two different titles must not collapse onto one name.
        first = policy.safe_name("a b", "mp4")
        second = policy.safe_name("a-b", "mp4")

        assert first != second

    def test_truncates_to_the_length_budget(self, policy: FilenamePolicy) -> None:
        generated = policy.safe_name("x" * 500, "mp4")

        assert len(generated) <= policy.max_length
        assert generated.endswith(".mp4")

    def test_reserved_stem_is_defused(self, policy: FilenamePolicy) -> None:
        generated = policy.safe_name("nul", "mp4")

        assert generated != "nul.mp4"
        assert policy.validate(generated) == generated

    def test_extension_is_capped(self, policy: FilenamePolicy) -> None:
        generated = policy.safe_name("clip", "e" * 50)

        _, _, extension = generated.partition(".")
        assert len(extension) <= policy.max_extension_length


class TestGeneratedName:
    def test_is_the_identifier_plus_the_extension(self, policy: FilenamePolicy) -> None:
        assert policy.generated_name("9f3a1c", "mp4") == "9f3a1c.mp4"

    def test_works_without_an_extension(self, policy: FilenamePolicy) -> None:
        assert policy.generated_name("9f3a1c") == "9f3a1c"

    @pytest.mark.parametrize(
        ("identifier", "extension"),
        [
            ("../../etc/passwd", "mp4"),
            ("con", "mp4"),
            ("", ""),
            ("\u202eexe", "../sh"),
            ("-flag", "mp4"),
        ],
    )
    def test_output_is_always_valid(
        self, policy: FilenamePolicy, identifier: str, extension: str
    ) -> None:
        generated = policy.generated_name(identifier, extension)

        assert policy.validate(generated) == generated


class TestTemporaryName:
    def test_is_hidden_and_suffixed(self, policy: FilenamePolicy) -> None:
        temporary = policy.temporary_name("9f3a1c")

        assert temporary.startswith(".")
        assert temporary.endswith(".partial")
        assert policy.is_temporary(temporary)

    def test_is_never_a_name_a_caller_could_ask_for(self, policy: FilenamePolicy) -> None:
        # An in-flight download must not be reachable as a published artifact.
        with pytest.raises(InvalidArtifactNameError):
            policy.validate(policy.temporary_name("9f3a1c"))

    def test_a_published_name_is_not_temporary(self, policy: FilenamePolicy) -> None:
        assert not policy.is_temporary(policy.generated_name("9f3a1c", "mp4"))

    @pytest.mark.parametrize("token", ["../../etc/passwd", "", "a" * 500])
    def test_hostile_tokens_stay_inside_one_name(self, policy: FilenamePolicy, token: str) -> None:
        temporary = policy.temporary_name(token)

        assert "/" not in temporary
        assert "\\" not in temporary
        assert len(temporary) <= policy.max_length
