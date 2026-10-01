"""The engine gets the URL shape its extractor matches; everything else is untouched."""

from __future__ import annotations

import pytest

from mediahub.infrastructure.download.ytdlp.rewrites import engine_url

SPOTLIGHT_ID = "W7_EDlXWTBiXAEEniNoMPwAAYdWxvYnBhaHR3AaARhNpsAaARhNmWAAAAAQ"


class TestEngineUrl:
    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.snapchat.com/@al20258600/spotlight/{SPOTLIGHT_ID}",
            f"https://snapchat.com/@some.user/spotlight/{SPOTLIGHT_ID}?share_id=abc",
            f"https://WWW.Snapchat.com/@x/spotlight/{SPOTLIGHT_ID}",
        ],
    )
    def test_a_profile_scoped_spotlight_link_becomes_the_canonical_one(self, url: str) -> None:
        rewritten = engine_url(url)

        assert rewritten.lower().endswith(f"/spotlight/{SPOTLIGHT_ID}".lower())
        assert rewritten.lower().startswith("https://")
        assert "/@" not in rewritten, "the profile segment is what the extractor rejects"
        assert "share_id" not in rewritten

    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.snapchat.com/spotlight/{SPOTLIGHT_ID}",
            "https://www.snapchat.com/add/someone",
            "https://www.youtube.com/watch?v=abc",
            "https://example.com/@user/spotlight/not-snapchat",
        ],
    )
    def test_every_other_link_is_passed_through_unchanged(self, url: str) -> None:
        assert engine_url(url) == url
