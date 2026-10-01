"""The Beeg plugin reads the 2026 store API the site itself uses."""

from __future__ import annotations

import pytest

from yt_dlp_plugins.extractor.beeg import (
    BeegMediahubIE,
    manifest_url,
    normalise_id,
    tags_of,
    title_of,
)

FACTS = {
    "file": {
        "id": 1277207756,
        "fl_duration": 1114,
        "fl_width": 1280,
        "fl_height": 720,
        "data": [
            {"cd_column": "sf_story", "cd_value": "a story"},
            {"cd_column": "sf_name", "cd_value": "  Czech teens licking each other "},
        ],
    },
    "fc_facts": [
        {"id": 5555196, "fc_created": "2024-03-01T15:55:46.523158Z", "fc_st_views": 20711}
    ],
    "tags": [{"tg_name": "teen"}, {"tg_name": "lesbian"}, {"nope": 1}],
}


class TestIdNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("-0983946056129650", "983946056129650"),
            ("0599050563103750", "599050563103750"),
            ("1277207756", "1277207756"),
            ("-1941093077", "1941093077"),
            ("000", "0"),
        ],
    )
    def test_old_links_lose_their_sign_and_leading_zeros(self, raw: str, expected: str) -> None:
        """The API answers 400 "invalid integer" to a leading zero (2026-10-01)."""
        assert normalise_id(raw) == expected


class TestFacts:
    def test_title_comes_from_the_sf_name_column(self) -> None:
        assert title_of(FACTS) == "Czech teens licking each other"

    def test_no_title_is_none(self) -> None:
        assert title_of({"file": {"data": []}}) is None
        assert title_of({}) is None

    def test_tags_keep_their_order_and_skip_junk(self) -> None:
        assert tags_of(FACTS) == ["teen", "lesbian"]

    def test_the_manifest_is_the_play_url_on_the_video_host(self) -> None:
        """What the site's player hands to hls.js, read from its bundle."""
        play = "key=abc,end=1790918662,limit=10/data=0f2bf1ecf4/media=hls4A/multi=426x240:240p:x\n"

        assert manifest_url(play) == (
            "https://video.beeg.com/key=abc,end=1790918662,limit=10/data=0f2bf1ecf4/media=hls4A/multi=426x240:240p:x"
        )


class TestUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "https://beeg.com/-0983946056129650",
            "https://beeg.com/1277207756",
            "https://www.beeg.com/video/1941093077?t=911-1391",
        ],
    )
    def test_the_links_the_built_in_extractor_took_are_taken_here(self, url: str) -> None:
        assert BeegMediahubIE.suitable(url)

    def test_other_sites_are_not(self) -> None:
        assert not BeegMediahubIE.suitable("https://example.com/-1277207756")
