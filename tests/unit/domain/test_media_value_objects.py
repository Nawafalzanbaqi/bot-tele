"""Value objects reject invalid input and normalise valid input."""

from __future__ import annotations

import pytest

from mediahub.domain.media.errors import (
    InvalidFileSizeError,
    InvalidMediaIdError,
    InvalidMediaTitleError,
    InvalidSourceUrlError,
    InvalidStorageKeyError,
)
from mediahub.domain.media.value_objects import (
    FileSize,
    MediaId,
    MediaTitle,
    SourceUrl,
    StorageKey,
)

pytestmark = pytest.mark.unit


class TestSourceUrl:
    def test_normalises_scheme_host_and_drops_fragment(self) -> None:
        url = SourceUrl("  HTTPS://Example.COM/video.mp4?id=1#section  ")
        assert str(url) == "https://example.com/video.mp4?id=1"

    def test_exposes_host(self) -> None:
        assert SourceUrl("https://cdn.example.com:8443/a").host == "cdn.example.com"

    def test_equal_after_normalisation(self) -> None:
        assert SourceUrl("https://example.com/a#top") == SourceUrl("HTTPS://EXAMPLE.COM/a")

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "ftp://example.com/a", "file:///etc/passwd", "https://", "not a url"],
    )
    def test_rejects_unusable_urls(self, raw: str) -> None:
        with pytest.raises(InvalidSourceUrlError):
            SourceUrl(raw)


class TestMediaTitle:
    def test_collapses_whitespace(self) -> None:
        assert str(MediaTitle("  Clean   Architecture \n revisited ")) == (
            "Clean Architecture revisited"
        )

    @pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
    def test_rejects_empty(self, raw: str) -> None:
        with pytest.raises(InvalidMediaTitleError):
            MediaTitle(raw)

    def test_rejects_overlong(self) -> None:
        with pytest.raises(InvalidMediaTitleError):
            MediaTitle("x" * (MediaTitle.MAX_LENGTH + 1))


class TestStorageKey:
    def test_normalises_redundant_segments(self) -> None:
        assert str(StorageKey("video//2026/./talk.mp4")) == "video/2026/talk.mp4"

    def test_exposes_filename(self) -> None:
        assert StorageKey("video/2026/talk.mp4").filename == "talk.mp4"

    @pytest.mark.parametrize(
        "raw",
        ["", "/absolute/path", "..", "video/../../etc/passwd", "video\\windows.mp4"],
    )
    def test_rejects_unsafe_keys(self, raw: str) -> None:
        with pytest.raises(InvalidStorageKeyError):
            StorageKey(raw)


class TestFileSize:
    def test_rejects_negative(self) -> None:
        with pytest.raises(InvalidFileSizeError):
            FileSize(-1)

    @pytest.mark.parametrize(
        ("size", "expected"),
        [(0, "0 B"), (512, "512 B"), (2048, "2.0 KiB"), (5 * 1024**3, "5.0 GiB")],
    )
    def test_human_readable(self, size: int, expected: str) -> None:
        assert FileSize(size).human_readable == expected


class TestMediaId:
    def test_parses_canonical_form(self) -> None:
        media_id = MediaId.parse("1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed")
        assert str(media_id) == "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"

    def test_rejects_non_uuid(self) -> None:
        with pytest.raises(InvalidMediaIdError):
            MediaId.parse("not-a-uuid")
