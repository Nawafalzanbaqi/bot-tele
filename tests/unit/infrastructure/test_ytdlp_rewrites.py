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
        ("url", "expected"),
        [
            ("https://vimeo.com/393756517", "https://player.vimeo.com/video/393756517"),
            ("https://www.vimeo.com/76979871/", "https://player.vimeo.com/video/76979871"),
            ("https://vimeo.com/393756517?share=copy", "https://player.vimeo.com/video/393756517"),
            (
                "https://vimeo.com/123456789/abcdef0123",
                "https://player.vimeo.com/video/123456789?h=abcdef0123",
            ),
        ],
    )
    def test_a_plain_vimeo_link_goes_to_the_player(self, url: str, expected: str) -> None:
        """The plain page demands a login since 2026; the player does not."""
        assert engine_url(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://vimeo.com/channels/keypeele/75629013",
            "https://player.vimeo.com/video/393756517",
            "https://vimeo.com/showcase/1234567",
        ],
    )
    def test_vimeo_links_that_already_work_are_untouched(self, url: str) -> None:
        assert engine_url(url) == url

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
