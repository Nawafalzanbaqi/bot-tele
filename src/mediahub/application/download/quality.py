"""Turns engine metadata into the handful of choices a person is offered.

Pure, and deliberately in the application layer rather than in an interface:
"which qualities do we offer?" must give the same answer in Telegram, in the
API and in the CLI. An interface that built its own list would drift, and a
button labelled 1080p would mean something different depending on where it was
tapped.

Two functions, one contract between them: :func:`build_quality_options` decides
what may be offered, and :func:`selection_for` turns a chosen key back into an
engine-neutral :class:`~mediahub.application.download.ports.FormatSelection`.
The keys are short and opaque because they travel in places with tight size
limits - a Telegram callback payload is 64 bytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mediahub.application.download.dto import QualityOption
from mediahub.application.download.errors import FormatUnavailableError
from mediahub.application.download.ports import FormatSelection

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.download.ports import MediaMetadata

BEST_KEY: Final[str] = "best"
AUDIO_KEY: Final[str] = "audio"
OFFERED_HEIGHTS: Final[tuple[int, ...]] = (2160, 1440, 1080, 720, 480, 360)
"""Heights worth offering. Anything else is rounded down to one of these, so a
source with fourteen renditions still produces a readable list."""

MAX_OPTIONS: Final[int] = 6


def build_quality_options(
    metadata: MediaMetadata, *, max_bytes: int | None = None, allow_merge: bool = False
) -> tuple[QualityOption, ...]:
    """Return the choices to offer for a source, best first.

    Args:
        metadata: What the engine reported about the source.
        max_bytes: Ceiling this deployment can handle. Renditions known to be
            larger are dropped, because offering a choice that will certainly
            be refused wastes the user's time and a download slot. Renditions
            of *unknown* size are kept - an absent size is not a large one.
        allow_merge: Whether this deployment combines separate streams. It
            changes the *sizes*, not just the list: a video-only rendition is
            delivered with an audio track attached, so the number shown against
            it must include that track. Reporting the video stream alone
            understates the higher rungs badly - the ones where a platform stops
            offering a muxed file at all - and a size the user cannot trust is
            worse than no size, because they plan around it.

    Returns:
        Between one and :data:`MAX_OPTIONS` options. Always includes
        :data:`BEST_KEY` when any video exists, and :data:`AUDIO_KEY` when an
        audio-only rendition exists.
    """
    options: list[QualityOption] = []
    audio_overhead = _merge_audio_bytes(metadata) if allow_merge else 0

    if metadata.has_video:
        options.append(
            QualityOption(
                key=BEST_KEY,
                label="Best available",
                format_id=None,
                height=None,
                approx_bytes=metadata.expected_bytes,
                is_audio_only=False,
            )
        )

    seen_heights: set[int] = set()
    for video in metadata.video_formats:
        if video.height is None:
            continue
        bucket = _bucket(video.height)
        if bucket is None or bucket in seen_heights:
            continue
        estimated = _delivered_bytes(video.filesize_bytes, video.has_audio, audio_overhead)
        if _exceeds(estimated, max_bytes):
            continue
        seen_heights.add(bucket)
        options.append(
            QualityOption(
                key=f"h{bucket}",
                label=f"{bucket}p",
                format_id=None,
                height=bucket,
                approx_bytes=estimated,
                is_audio_only=False,
            )
        )

    if metadata.has_audio:
        best_audio = metadata.audio_formats[0]
        if not _exceeds(best_audio.filesize_bytes, max_bytes):
            options.append(
                QualityOption(
                    key=AUDIO_KEY,
                    label="Audio only",
                    format_id=None,
                    height=None,
                    approx_bytes=best_audio.filesize_bytes,
                    is_audio_only=True,
                )
            )

    return tuple(options[:MAX_OPTIONS])


def selection_for(
    key: str,
    options: Sequence[QualityOption],
    *,
    allow_merge: bool = False,
    prefer_compatible: bool = False,
) -> FormatSelection:
    """Return the engine-neutral selection a chosen key means.

    Args:
        key: The key of an option previously offered.
        options: The options that were offered, used to validate the key.
        allow_merge: Whether separate video and audio streams may be combined,
            which requires a merger on the device. **This is what makes the
            higher rungs mean what they say.** Above roughly 720p every large
            platform ships video and audio separately; without merging, a
            request for 1080p quietly resolves to the best *already-muxed*
            rendition - usually 720p - and nothing reports that the button did
            not do what it said.
        prefer_compatible: Whether to prefer codecs an ordinary player can
            decode over the newest ones a platform offers. Set for any
            destination people actually watch things in; see
            :class:`~mediahub.application.download.ports.FormatSelection`.

    Returns:
        The selection to hand to the download engine.

    Raises:
        FormatUnavailableError: If the key was never offered. This is the
            correct answer for a stale button tapped an hour later: the
            renditions may well have changed, and honouring a key that is no
            longer in the list would download something nobody chose.
    """
    chosen = next((option for option in options if option.key == key), None)
    if chosen is None:
        message = f"'{key}' is no longer an available quality for this source"
        raise FormatUnavailableError(message)

    if chosen.is_audio_only:
        return FormatSelection.audio_only(prefer_compatible=prefer_compatible)
    if chosen.height is not None:
        return FormatSelection.up_to_height(
            chosen.height, allow_merge=allow_merge, prefer_compatible=prefer_compatible
        )
    return FormatSelection.best(allow_merge=allow_merge, prefer_compatible=prefer_compatible)


def _merge_audio_bytes(metadata: MediaMetadata) -> int:
    """Return the size of the audio track a merge would attach.

    The *smallest* known audio rendition, not the best. Two reasons, and they
    point the same way: the engine's fallback picks whatever audio fits, and a
    size shown to a person should err towards being beaten rather than missed.
    Zero when nothing declares a size, which keeps an unknown from being
    presented as a certainty.
    """
    sizes = [
        audio.filesize_bytes for audio in metadata.audio_formats if audio.filesize_bytes is not None
    ]
    return min(sizes) if sizes else 0


def _delivered_bytes(video_bytes: int | None, has_audio: bool, audio_overhead: int) -> int | None:
    """Return what will actually be delivered for one video rendition."""
    if video_bytes is None:
        return None
    if has_audio:
        return video_bytes
    return video_bytes + audio_overhead


def _bucket(height: int) -> int | None:
    """Round a height down to the nearest offered rung."""
    for rung in OFFERED_HEIGHTS:
        if height >= rung:
            return rung
    return None


def _exceeds(size: int | None, ceiling: int | None) -> bool:
    """Return whether a known size is above a known ceiling."""
    return size is not None and ceiling is not None and size > ceiling
