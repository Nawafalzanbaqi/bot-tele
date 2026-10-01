"""Beeg, through the store API the site uses itself (2026).

yt-dlp's built-in extractor asks ``facts/file/<id>`` with the id exactly as it
appears in the link and reads ``hls_resources`` from the answer. Both broke in
2026: the API refuses an id with a leading zero ("invalid integer"), and the
answer no longer carries ``hls_resources`` at all (yt-dlp issue #17680, open
since 2026-08-30, no fix). What the site does instead - read from its bundle on
2026-10-01 - is ask ``video/play_url/<id>`` for a signed string and hand
``https://video.beeg.com/<that string>`` to hls.js as the master playlist. That
is all this extractor does.

Lives in ``yt_dlp_plugins`` so the weekly yt-dlp bump cannot wipe it; it takes
precedence over the built-in extractor for the same links.
"""

from __future__ import annotations

from typing import Any, Final

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, int_or_none, unified_timestamp

STORE: Final[str] = "https://store.externulls.com"
VIDEOS: Final[str] = "https://video.beeg.com"
_HEADERS: Final[dict[str, str]] = {"Referer": "https://beeg.com/", "Origin": "https://beeg.com"}


def normalise_id(raw: str) -> str:
    """Return the id the store API accepts: digits only, no sign, no leading zeros.

    Old links are ``beeg.com/-0983946056129650``; the API wants ``983946056129650``.
    """
    digits = raw.strip().lstrip("-").lstrip("0")
    return digits or "0"


def manifest_url(play_url: str) -> str:
    """Return the HLS master playlist URL for a ``video/play_url`` answer."""
    return f"{VIDEOS}/{play_url.strip()}"


def title_of(facts: dict[str, Any]) -> str | None:
    """Return the title from the file's ``sf_name`` column, when present."""
    file = facts.get("file")
    if not isinstance(file, dict):
        return None
    for entry in file.get("data") or ():
        if isinstance(entry, dict) and entry.get("cd_column") == "sf_name":
            value = entry.get("cd_value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def tags_of(facts: dict[str, Any]) -> list[str]:
    """Return the tag names, in the order the API lists them."""
    return [
        tag["tg_name"]
        for tag in facts.get("tags") or ()
        if isinstance(tag, dict) and isinstance(tag.get("tg_name"), str)
    ]


class BeegMediahubIE(InfoExtractor):  # type: ignore[misc]
    """Beeg videos via the 2026 store API."""

    IE_NAME = "beeg:mediahub"
    _VALID_URL = r"https?://(?:www\.)?beeg\.(?:com(?:/video)?)/-?(?P<id>\d+)"

    def _real_extract(self, url: str) -> dict[str, Any]:
        """Describe the video and its HLS renditions."""
        video_id = normalise_id(self._match_id(url))
        facts = self._download_json(
            f"{STORE}/facts/file/{video_id}", video_id, "Downloading file facts", headers=_HEADERS
        )
        if not isinstance(facts, dict) or not isinstance(facts.get("file"), dict):
            message = "Beeg's store API returned no file for this id"
            raise ExtractorError(message, expected=True)
        play_url = self._download_webpage(
            f"{STORE}/video/play_url/{video_id}", video_id, "Downloading play URL", headers=_HEADERS
        )
        if "key=" not in play_url:
            message = "Beeg's store API returned no play URL for this video"
            raise ExtractorError(message, expected=True)
        formats = self._extract_m3u8_formats(
            manifest_url(play_url), video_id, "mp4", m3u8_id="hls", headers=_HEADERS
        )
        file = facts["file"]
        first_fact = next(
            (fact for fact in facts.get("fc_facts") or () if isinstance(fact, dict)), {}
        )
        return {
            "id": video_id,
            "title": title_of(facts) or f"beeg {video_id}",
            "formats": formats,
            "duration": int_or_none(file.get("fl_duration")),
            "width": int_or_none(file.get("fl_width")),
            "height": int_or_none(file.get("fl_height")),
            "timestamp": unified_timestamp(first_fact.get("fc_created")),
            "view_count": int_or_none(first_fact.get("fc_st_views")),
            "tags": tags_of(facts),
            "age_limit": 18,
        }
