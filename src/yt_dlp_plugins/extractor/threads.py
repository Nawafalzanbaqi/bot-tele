"""Threads posts, read from the JSON the page already embeds.

Neither yt-dlp nor gallery-dl has a Threads extractor (2026-10-01). A post's
page, served logged-out, carries the post as JSON in its ``data-sjs`` script
blocks - the same shape Instagram uses: ``video_versions`` (progressive H.264
MP4s, best first), ``image_versions2.candidates`` (JPEGs with their sizes),
and ``carousel_media`` for a multi-item post. That is what this reads; nothing
is fetched that a browser would not.

A single video or image becomes one entry. A carousel becomes a playlist whose
entries all point back to the post - the shape the bot delivers as one album.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, int_or_none

_BLOB: Final[re.Pattern[str]] = re.compile(
    r"<script[^>]+type=[\"']application/json[\"'][^>]*data-sjs[^>]*>(.*?)</script>", re.S
)
_MEDIA_KEYS: Final[tuple[str, ...]] = ("video_versions", "image_versions2", "carousel_media")
MAX_CAROUSEL_ITEMS: Final[int] = 20


def find_post(webpage: str, code: str) -> dict[str, Any] | None:
    """Return the post object whose ``code`` is ``code``, from any JSON blob on the page."""
    for blob in _BLOB.findall(webpage):
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        found = _walk(data, code)
        if found is not None:
            return found
    return None


def _walk(node: Any, code: str) -> dict[str, Any] | None:
    """Depth-first search for a media dict carrying ``code``."""
    if isinstance(node, dict):
        if node.get("code") == code and any(key in node for key in _MEDIA_KEYS):
            return node
        for value in node.values():
            found = _walk(value, code)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _walk(value, code)
            if found is not None:
                return found
    return None


def caption_of(post: dict[str, Any]) -> str | None:
    """Return the caption text, when the post has one."""
    caption = post.get("caption")
    if isinstance(caption, dict) and isinstance(caption.get("text"), str):
        text = caption["text"].strip()
        return text or None
    return None


def _title(post: dict[str, Any], code: str) -> str:
    caption = caption_of(post)
    if caption:
        return caption.splitlines()[0][:200]
    user = post.get("user") or {}
    name = user.get("username") if isinstance(user, dict) else None
    return f"Threads post by @{name}" if name else f"Threads post {code}"


def _common(post: dict[str, Any], code: str, url: str) -> dict[str, Any]:
    user: dict[str, Any] = post["user"] if isinstance(post.get("user"), dict) else {}
    return {
        "title": _title(post, code),
        "description": caption_of(post),
        "uploader": user.get("username"),
        "uploader_id": str(user["pk"]) if user.get("pk") is not None else None,
        "timestamp": int_or_none(post.get("taken_at")),
        "like_count": int_or_none(post.get("like_count")),
        "webpage_url": url,
    }


def _thumbnails(item: dict[str, Any]) -> list[dict[str, Any]]:
    versions = item.get("image_versions2")
    candidates = versions.get("candidates") if isinstance(versions, dict) else None
    thumbnails: list[dict[str, Any]] = [
        {
            "url": candidate["url"],
            "width": int_or_none(candidate.get("width")),
            "height": int_or_none(candidate.get("height")),
        }
        for candidate in candidates or ()
        if isinstance(candidate, dict) and isinstance(candidate.get("url"), str)
    ]
    thumbnails.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    return thumbnails


def _video_formats(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the progressive renditions, declared as what they are.

    Threads carries no codec or frame size per version; every version seen
    (2026-10-01, ffprobe) is H.264 + AAC at the post's own frame size, so
    that is declared - the bot refuses to call a video "video" without it.
    """
    formats: list[dict[str, Any]] = []
    for version in item.get("video_versions") or ():
        if not isinstance(version, dict) or not isinstance(version.get("url"), str):
            continue
        kind = int_or_none(version.get("type")) or 0
        formats.append(
            {
                "format_id": f"v{kind}" if kind else "v",
                "url": version["url"],
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a.40.2",
                # Instagram orders 101 (best) .. 103; a higher quality number wins.
                "quality": -kind if kind else None,
                "width": int_or_none(version.get("width"))
                or int_or_none(item.get("original_width")),
                "height": int_or_none(version.get("height"))
                or int_or_none(item.get("original_height")),
            }
        )
    return formats


def item_info(item: dict[str, Any], *, entry_id: str, title: str) -> dict[str, Any] | None:
    """Describe one media item: a video with its renditions, or an image."""
    formats = _video_formats(item)
    thumbnails = _thumbnails(item)
    if formats:
        return {
            "id": entry_id,
            "title": title,
            "formats": formats,
            "thumbnails": thumbnails,
            "width": int_or_none(item.get("original_width")),
            "height": int_or_none(item.get("original_height")),
            "duration": int_or_none(item.get("video_duration")),
        }
    if thumbnails:
        best = thumbnails[0]
        # No codec fields at all: the engine's "best" selector refuses a
        # format that declares neither stream, and so does its "anything"
        # fallback; a picture with unknown codecs is simply taken.
        return {
            "id": entry_id,
            "title": title,
            "url": best["url"],
            "ext": "jpg",
            "width": best.get("width") or int_or_none(item.get("original_width")),
            "height": best.get("height") or int_or_none(item.get("original_height")),
        }
    return None


def build_info(post: dict[str, Any], *, code: str, url: str) -> dict[str, Any]:
    """Turn a post object into a yt-dlp info dict: one entry, or a playlist for a carousel."""
    common = _common(post, code, url)
    carousel = post.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(carousel[:MAX_CAROUSEL_ITEMS], start=1):
            if not isinstance(item, dict):
                continue
            entry = item_info(
                item, entry_id=f"{code}_{index}", title=f"{common['title']} ({index})"
            )
            if entry is not None:
                entries.append({**entry, "webpage_url": url})
        if not entries:
            message = "This Threads post holds nothing that can be fetched"
            raise ExtractorError(message, expected=True)
        return {"_type": "playlist", "id": code, "entries": entries, **common}
    single = item_info(post, entry_id=code, title=common["title"])
    if single is None:
        message = "No video or image could be found in this Threads post"
        raise ExtractorError(message, expected=True)
    return {**single, **common, "id": code}


class ThreadsMediahubIE(InfoExtractor):  # type: ignore[misc]
    """Threads posts: video, image, or carousel, from the page's embedded JSON."""

    IE_NAME = "threads:mediahub"
    _VALID_URL = r"https?://(?:www\.)?threads\.(?:net|com)/(?:@[\w.\-]+/)?post/(?P<id>[\w-]+)"

    def _real_extract(self, url: str) -> dict[str, Any]:
        """Read the post from its page."""
        code = self._match_id(url)
        webpage = self._download_webpage(url, code)
        post = find_post(webpage, code)
        if post is None:
            message = "This Threads post is not on its page: private, deleted, or a login wall"
            raise ExtractorError(message, expected=True)
        return build_info(post, code=code, url=url)
