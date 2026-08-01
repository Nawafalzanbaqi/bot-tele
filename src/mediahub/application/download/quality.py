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
    metadata: MediaMetadata, *, max_bytes: int | None = None
) -> tuple[QualityOption, ...]:
    """Return the choices to offer for a source, best first.

    Args:
        metadata: What the engine reported about the source.
        max_bytes: Ceiling this deployment can handle. Renditions known to be
            larger are dropped, because offering a choice that will certainly
            be refused wastes the user's time and a download slot. Renditions
            of *unknown* size are kept - an absent size is not a large one.

    Returns:
        Between one and :data:`MAX_OPTIONS` options. Always includes
        :data:`BEST_KEY` when any video exists, and :data:`AUDIO_KEY` when an
        audio-only rendition exists.
    """
    options: list[QualityOption] = []

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
        if _exceeds(video.filesize_bytes, max_bytes):
            continue
        seen_heights.add(bucket)
        options.append(
            QualityOption(
                key=f"h{bucket}",
                label=f"{bucket}p",
                format_id=None,
                height=bucket,
                approx_bytes=video.filesize_bytes,
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


def selection_for(key: str, options: Sequence[QualityOption]) -> FormatSelection:
    """Return the engine-neutral selection a chosen key means.

    Args:
        key: The key of an option previously offered.
        options: The options that were offered, used to validate the key.

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
        return FormatSelection.audio_only()
    if chosen.height is not None:
        return FormatSelection.up_to_height(chosen.height)
    return FormatSelection.best()


def _bucket(height: int) -> int | None:
    """Round a height down to the nearest offered rung."""
    for rung in OFFERED_HEIGHTS:
        if height >= rung:
            return rung
    return None


def _exceeds(size: int | None, ceiling: int | None) -> bool:
    """Return whether a known size is above a known ceiling."""
    return size is not None and ceiling is not None and size > ceiling
