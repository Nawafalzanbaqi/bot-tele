"""Translates yt-dlp info dictionaries into MediaHub DTOs.

An info dict is a large, loosely-typed, version-dependent mapping produced by a
third party. This module is the anti-corruption layer for it: every field is
read defensively, every type is coerced, and the result is a small immutable DTO
the rest of the system can trust.

Pure and yt-dlp-free, so the mapping can be tested against recorded fixtures
with no network and no engine installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from mediahub.application.download.ports import (
    AudioFormat,
    MediaMetadata,
    SelectedFormat,
    Thumbnail,
    VideoFormat,
)
from mediahub.domain.media.enums import MediaType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

_NONE_CODEC: Final[str] = "none"
_MAX_DESCRIPTION_LENGTH: Final[int] = 2000
_MAX_TITLE_LENGTH: Final[int] = 500
_MS_PER_SECOND: Final[int] = 1000
_UNPLAYABLE_EXTENSIONS: Final[frozenset[str]] = frozenset({"mhtml"})
_IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset({"jpg", "jpeg", "png", "webp", "gif", "bmp"})


# --------------------------------------------------------------------------- #
# Scalar coercion                                                              #
# --------------------------------------------------------------------------- #


def _text(info: Mapping[str, Any], key: str, *, limit: int | None = None) -> str | None:
    """Return a trimmed string field, or ``None`` when absent or empty."""
    raw = info.get(key)
    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split()) if limit else raw.strip()
    if not value:
        return None
    return value[:limit] if limit else value


def _integer(info: Mapping[str, Any], key: str) -> int | None:
    """Return a non-negative integer field, or ``None``."""
    raw = info.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = int(raw)
    return value if value >= 0 else None


def _number(info: Mapping[str, Any], key: str) -> float | None:
    """Return a non-negative float field, or ``None``."""
    raw = info.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if value >= 0 else None


def _flag(info: Mapping[str, Any], key: str) -> bool:
    """Return a boolean field, treating anything unparsable as ``False``."""
    return bool(info.get(key))


def _codec(info: Mapping[str, Any], key: str) -> str | None:
    """Return a codec name, mapping yt-dlp's ``"none"`` sentinel to ``None``."""
    raw = info.get(key)
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value or value == _NONE_CODEC:
        return None
    return value


def _upload_date(info: Mapping[str, Any]) -> datetime | None:
    """Return the publication date as an aware UTC datetime, when parsable."""
    timestamp = info.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            return datetime.fromtimestamp(float(timestamp), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    raw = info.get("upload_date")
    if isinstance(raw, str) and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Formats                                                                      #
# --------------------------------------------------------------------------- #


def _format_size(entry: Mapping[str, Any]) -> tuple[int | None, bool]:
    """Return ``(size, is_estimate)`` for one format entry."""
    exact = _integer(entry, "filesize")
    if exact is not None:
        return exact, False
    approximate = _integer(entry, "filesize_approx")
    if approximate is not None:
        return approximate, True
    return None, False


def _is_usable(entry: Mapping[str, Any]) -> bool:
    """Return whether a format entry represents real, downloadable media.

    Storyboards and image-strip "formats" are advertised alongside real ones and
    would otherwise pollute the quality list a user is offered.
    """
    if not isinstance(entry.get("format_id"), str):
        return False
    extension = (_text(entry, "ext") or "").lower()
    if extension in _UNPLAYABLE_EXTENSIONS:
        return False
    note = (_text(entry, "format_note") or "").lower()
    return "storyboard" not in note


def _quality_label(entry: Mapping[str, Any], height: int | None) -> str | None:
    """Return a human quality label for a video format."""
    note = _text(entry, "format_note")
    if note and note.lower() not in {"default", "unknown"}:
        return note
    return f"{height}p" if height else None


def to_video_format(entry: Mapping[str, Any]) -> VideoFormat:
    """Map one info-dict format entry to a :class:`VideoFormat`."""
    size, estimated = _format_size(entry)
    height = _integer(entry, "height")
    return VideoFormat(
        format_id=str(entry["format_id"]),
        container=_text(entry, "ext"),
        video_codec=_codec(entry, "vcodec"),
        audio_codec=_codec(entry, "acodec"),
        width=_integer(entry, "width"),
        height=height,
        fps=_number(entry, "fps"),
        bitrate_kbps=_number(entry, "tbr"),
        filesize_bytes=size,
        filesize_is_estimate=estimated,
        quality_label=_quality_label(entry, height),
    )


def to_audio_format(entry: Mapping[str, Any]) -> AudioFormat:
    """Map one info-dict format entry to an :class:`AudioFormat`."""
    size, estimated = _format_size(entry)
    return AudioFormat(
        format_id=str(entry["format_id"]),
        container=_text(entry, "ext"),
        audio_codec=_codec(entry, "acodec"),
        bitrate_kbps=_number(entry, "abr") or _number(entry, "tbr"),
        sample_rate_hz=_integer(entry, "asr"),
        channels=_integer(entry, "audio_channels"),
        filesize_bytes=size,
        filesize_is_estimate=estimated,
        language=_text(entry, "language"),
    )


def split_formats(
    entries: Sequence[Mapping[str, Any]],
) -> tuple[tuple[VideoFormat, ...], tuple[AudioFormat, ...]]:
    """Split raw format entries into video and audio-only renditions.

    Ordering is by descending quality so that "the first one" is always the best
    one, whatever the engine's own ordering happened to be.
    """
    videos: list[VideoFormat] = []
    audios: list[AudioFormat] = []

    for entry in entries:
        if not _is_usable(entry):
            continue
        has_video = _codec(entry, "vcodec") is not None
        has_audio = _codec(entry, "acodec") is not None
        if has_video:
            videos.append(to_video_format(entry))
        elif has_audio:
            audios.append(to_audio_format(entry))

    videos.sort(key=lambda item: (item.height or 0, item.bitrate_kbps or 0), reverse=True)
    audios.sort(key=lambda item: (item.bitrate_kbps or 0, item.sample_rate_hz or 0), reverse=True)
    return tuple(videos), tuple(audios)


def to_thumbnails(entries: Sequence[Mapping[str, Any]]) -> tuple[Thumbnail, ...]:
    """Map thumbnail entries, largest first, discarding unusable ones."""
    thumbnails = [
        Thumbnail(
            url=str(entry["url"]),
            width=_integer(entry, "width"),
            height=_integer(entry, "height"),
            thumbnail_id=_text(entry, "id"),
        )
        for entry in entries
        if isinstance(entry.get("url"), str)
    ]
    thumbnails.sort(key=lambda item: item.pixels, reverse=True)
    return tuple(thumbnails)


# --------------------------------------------------------------------------- #
# Metadata                                                                     #
# --------------------------------------------------------------------------- #


def _classify(
    info: Mapping[str, Any],
    videos: Sequence[VideoFormat],
    audios: Sequence[AudioFormat],
) -> MediaType:
    """Decide the broad media category from what the source offers."""
    extension = (_text(info, "ext") or "").lower()
    if extension in _IMAGE_EXTENSIONS and not videos:
        return MediaType.IMAGE
    if videos:
        return MediaType.VIDEO
    if audios:
        return MediaType.AUDIO
    if _integer(info, "duration") is not None:
        return MediaType.VIDEO
    return MediaType.OTHER


def _expected_bytes(
    info: Mapping[str, Any],
    videos: Sequence[VideoFormat],
    audios: Sequence[AudioFormat],
) -> int | None:
    """Return the best available size estimate for admission decisions."""
    for key in ("filesize", "filesize_approx"):
        value = _integer(info, key)
        if value is not None:
            return value
    if videos and videos[0].filesize_bytes is not None:
        return videos[0].filesize_bytes
    if audios and audios[0].filesize_bytes is not None:
        return audios[0].filesize_bytes
    return None


def to_metadata(info: Mapping[str, Any], *, url: str, probed_at: datetime) -> MediaMetadata:
    """Map a yt-dlp info dictionary to :class:`MediaMetadata`.

    Args:
        info: The dictionary yt-dlp returned.
        url: The canonical URL that was probed.
        probed_at: When the probe happened (UTC).

    Returns:
        A metadata DTO. Missing fields become ``None`` rather than raising: a
        source that omits its duration is normal, not an error.
    """
    entries = info.get("formats")
    raw_formats: Sequence[Mapping[str, Any]] = entries if isinstance(entries, list) else []
    videos, audios = split_formats(raw_formats)

    raw_thumbnails = info.get("thumbnails")
    thumbnails = to_thumbnails(raw_thumbnails if isinstance(raw_thumbnails, list) else [])
    if not thumbnails:
        single = _text(info, "thumbnail")
        if single:
            thumbnails = (Thumbnail(url=single),)

    is_playlist = info.get("_type") == "playlist"
    duration = _number(info, "duration")

    return MediaMetadata(
        url=url,
        provider=(_text(info, "extractor_key") or _text(info, "extractor") or "generic").lower(),
        provider_item_id=_text(info, "id"),
        title=_text(info, "title", limit=_MAX_TITLE_LENGTH) or "untitled",
        kind=_classify(info, videos, audios),
        duration_ms=int(duration * _MS_PER_SECOND) if duration else None,
        is_live=_flag(info, "is_live") or info.get("live_status") == "is_live",
        is_playlist=is_playlist,
        entry_count=_integer(info, "playlist_count") if is_playlist else None,
        uploader=_text(info, "uploader") or _text(info, "channel"),
        upload_date=_upload_date(info),
        description=_text(info, "description", limit=_MAX_DESCRIPTION_LENGTH),
        age_limit=_integer(info, "age_limit"),
        thumbnails=thumbnails,
        video_formats=videos,
        audio_formats=audios,
        expected_bytes=_expected_bytes(info, videos, audios),
        probed_at=probed_at,
    )


def to_selected_format(info: Mapping[str, Any]) -> SelectedFormat:
    """Map the completed-download info dict to the rendition actually taken.

    yt-dlp reports the outcome under ``requested_downloads``; the top level is
    used as a fallback for engines or code paths that do not populate it.
    """
    requested = info.get("requested_downloads")
    source: Mapping[str, Any] = info
    if isinstance(requested, list) and requested and isinstance(requested[0], dict):
        source = requested[0]

    video_codec = _codec(source, "vcodec")
    return SelectedFormat(
        format_id=str(source.get("format_id") or info.get("format_id") or "unknown"),
        container=_text(source, "ext"),
        video_codec=video_codec,
        audio_codec=_codec(source, "acodec"),
        width=_integer(source, "width"),
        height=_integer(source, "height"),
        bitrate_kbps=_number(source, "tbr"),
        is_audio_only=video_codec is None,
    )
