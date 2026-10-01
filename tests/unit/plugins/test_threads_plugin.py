"""The Threads plugin reads a post from the JSON its page embeds."""

from __future__ import annotations

import json

import pytest
from yt_dlp.utils import ExtractorError

from yt_dlp_plugins.extractor.threads import (
    ThreadsMediahubIE,
    build_info,
    caption_of,
    find_post,
    item_info,
)

URL = "https://www.threads.com/@someone/post/DQNCKbZjq-v"
CODE = "DQNCKbZjq-v"


def video_post(**overrides: object) -> dict[str, object]:
    post: dict[str, object] = {
        "code": CODE,
        "pk": "3750663577377091503",
        "media_type": 2,
        "original_width": 720,
        "original_height": 1280,
        "taken_at": 1761334040,
        "like_count": 1227,
        "has_audio": True,
        "caption": {"text": "coffee dates with your bestie >>>\n\nkipotheshibainu"},
        "user": {"username": "instagram", "pk": 25025320},
        "video_versions": [
            {"type": 102, "url": "https://cdn/v102.mp4"},
            {"type": 101, "url": "https://cdn/v101.mp4"},
            {"type": 103, "url": "https://cdn/v103.mp4"},
        ],
        "image_versions2": {
            "candidates": [
                {"url": "https://cdn/640.jpg", "width": 640, "height": 1136},
                {"url": "https://cdn/320.jpg", "width": 320, "height": 568},
            ]
        },
        "carousel_media": None,
    }
    post.update(overrides)
    return post


def page_with(*posts: dict[str, object]) -> str:
    bbox = {"__bbox": {"result": {"data": {"items": list(posts)}}}}
    payload = {"require": [["x", "y", None, [bbox]]]}
    blob = json.dumps(payload)
    noise = json.dumps({"require": [["z", 1]]})
    return (
        "<html><head></head><body>"
        f'<script type="application/json" data-content-len="12" data-sjs>{noise}</script>'
        f'<script type="application/json" data-content-len="999" data-sjs>{blob}</script>'
        "</body></html>"
    )


class TestFindingThePost:
    def test_the_post_with_the_code_is_found_among_others(self) -> None:
        other = video_post(code="OTHER123")

        found = find_post(page_with(other, video_post()), CODE)

        assert found is not None
        assert found["code"] == CODE

    def test_a_page_without_it_yields_nothing(self) -> None:
        assert find_post(page_with(video_post(code="OTHER123")), CODE) is None
        assert find_post("<html></html>", CODE) is None

    def test_a_broken_blob_is_skipped(self) -> None:
        broken = '<script type="application/json" data-sjs>{not json</script>'
        page = broken + page_with(video_post())

        assert find_post(page, CODE) is not None


class TestDescribingAPost:
    def test_a_video_post_lists_its_renditions_best_first(self) -> None:
        info = build_info(video_post(), code=CODE, url=URL)

        assert info["id"] == CODE
        assert info["title"] == "coffee dates with your bestie >>>"
        assert info["uploader"] == "instagram"
        assert info["timestamp"] == 1761334040
        assert [f["format_id"] for f in info["formats"]] == ["v102", "v101", "v103"]
        assert info["formats"][0]["vcodec"] == "avc1"
        assert (info["formats"][0]["width"], info["formats"][0]["height"]) == (720, 1280)
        assert max(info["formats"], key=lambda f: f["quality"])["format_id"] == "v101"
        assert info["formats"][0]["ext"] == "mp4"
        assert info["thumbnails"][0]["width"] == 640
        assert info["webpage_url"] == URL

    def test_an_image_post_is_the_largest_candidate(self) -> None:
        post = video_post(video_versions=[], media_type=1)

        info = build_info(post, code=CODE, url=URL)

        assert info["url"] == "https://cdn/640.jpg"
        assert info["ext"] == "jpg"
        assert "vcodec" not in info, "declaring no streams makes the engine refuse the file"
        assert info["width"] == 640
        assert "formats" not in info

    def test_a_carousel_is_a_playlist_whose_entries_point_at_the_post(self) -> None:
        items = [
            {"media_type": 2, "video_versions": [{"type": 101, "url": "https://cdn/a.mp4"}]},
            {
                "media_type": 1,
                "image_versions2": {
                    "candidates": [{"url": "https://cdn/b.jpg", "width": 720, "height": 900}]
                },
            },
            "junk",
            {"media_type": 1, "image_versions2": {"candidates": []}},
        ]
        post = video_post(video_versions=[], media_type=8, carousel_media=items)

        info = build_info(post, code=CODE, url=URL)

        assert info["_type"] == "playlist"
        assert info["id"] == CODE
        assert [e["id"] for e in info["entries"]] == [f"{CODE}_1", f"{CODE}_2"]
        assert all(e["webpage_url"] == URL for e in info["entries"]), "same page: one album"
        assert info["entries"][0]["formats"][0]["url"] == "https://cdn/a.mp4"
        assert info["entries"][1]["ext"] == "jpg"
        assert info["entries"][1]["title"].endswith("(2)")

    def test_a_post_with_nothing_fetchable_is_refused_plainly(self) -> None:
        post = video_post(video_versions=[], image_versions2={"candidates": []})

        with pytest.raises(ExtractorError):
            build_info(post, code=CODE, url=URL)

    def test_no_caption_names_the_author(self) -> None:
        post = video_post(caption=None)

        assert build_info(post, code=CODE, url=URL)["title"] == "Threads post by @instagram"
        assert caption_of(post) is None

    def test_an_item_without_media_is_none(self) -> None:
        assert item_info({}, entry_id="x", title="t") is None


class TestUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.threads.com/@instagram/post/DQNCKbZjq-v",
            "https://www.threads.net/@zuck/post/DSVRshjkbtK",
            "https://threads.com/post/DQNCKbZjq-v?xmt=abc",
        ],
    )
    def test_post_links_are_taken(self, url: str) -> None:
        assert ThreadsMediahubIE.suitable(url)

    def test_a_profile_is_not(self) -> None:
        assert not ThreadsMediahubIE.suitable("https://www.threads.com/@instagram")
