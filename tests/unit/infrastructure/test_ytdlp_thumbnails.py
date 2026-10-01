"""A thumbnail URL that names no image type gets one, so the engine's safety check passes."""

from __future__ import annotations

import pytest

from mediahub.infrastructure.download.ytdlp.thumbnails import (
    attach_thumbnail_fixer,
    fix_thumbnail_extensions,
    guessed_extension,
)

SIGNED = "https://cf-st.sc-cdn.net/d/abc.IRZXSOY?uc=46"
PLAIN = "https://i.ytimg.com/vi/abc/maxresdefault.jpg"


class TestGuessedExtension:
    def test_mirrors_the_engine(self) -> None:
        assert guessed_extension(SIGNED) == "irzxsoy"
        assert guessed_extension(PLAIN) == "jpg"
        assert guessed_extension("https://x/y.webp?x=1") == "webp"
        assert guessed_extension("https://x/no-extension") == ""
        assert guessed_extension("https://x/odd.a-b") == ""


class TestFixThumbnailExtensions:
    def test_a_token_extension_becomes_jpg(self) -> None:
        info = {"thumbnails": [{"id": "0", "url": SIGNED}]}

        assert fix_thumbnail_extensions(info) == 1
        assert info["thumbnails"][0]["ext"] == "jpg"

    def test_a_real_image_extension_is_left_alone(self) -> None:
        info = {"thumbnails": [{"id": "0", "url": PLAIN}]}

        assert fix_thumbnail_extensions(info) == 0
        assert "ext" not in info["thumbnails"][0]

    def test_an_explicit_extension_is_respected(self) -> None:
        info = {"thumbnails": [{"id": "0", "url": SIGNED, "ext": "webp"}]}

        assert fix_thumbnail_extensions(info) == 0
        assert info["thumbnails"][0]["ext"] == "webp"

    def test_a_url_without_any_extension_is_named_too(self) -> None:
        info = {"thumbnails": [{"id": "0", "url": "https://x/no-extension"}]}

        assert fix_thumbnail_extensions(info) == 1

    def test_hostile_shapes_do_not_crash(self) -> None:
        assert fix_thumbnail_extensions({"thumbnails": None}) == 0
        assert fix_thumbnail_extensions({"thumbnails": ["x", {"url": 5}, {}]}) == 0
        assert fix_thumbnail_extensions({}) == 0


class TestAttach:
    def test_a_double_without_the_hook_is_left_alone(self) -> None:
        assert attach_thumbnail_fixer(object()) is False

    def test_the_fixer_is_registered_before_processing_and_fixes_in_place(self) -> None:
        pytest.importorskip("yt_dlp")
        registered: list[tuple[object, str]] = []

        class Engine:
            def add_post_processor(self, processor: object, when: str) -> None:
                registered.append((processor, when))

        assert attach_thumbnail_fixer(Engine()) is True
        (processor, when), = registered
        assert when == "pre_process"
        info = {"thumbnails": [{"id": "0", "url": SIGNED}]}
        deleted, returned = processor.run(info)  # type: ignore[attr-defined]
        assert deleted == []
        assert returned is info
        assert info["thumbnails"][0]["ext"] == "jpg"
