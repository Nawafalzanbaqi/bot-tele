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

    from mediahub.application.download.ports import MediaMetadata, VideoFormat

BEST_KEY: Final[str] = "best"
AUDIO_KEY: Final[str] = "audio"
AUTO_KEY: Final[str] = "auto"
"""Means "decide for me: the best rung that can actually be delivered".

Resolved by :func:`resolve_auto` at acquisition time rather than when the menu
is built, because the answer depends on the destination's ceiling and only the
acquisition knows which destination it is sending to. It is deliberately *not*
the same as :data:`BEST_KEY`: "best available" asks the engine for the best
rendition full stop, which on a long source is routinely larger than any chat
service will accept, and the failure arrives after the download rather than
before it.
"""
OFFERED_HEIGHTS: Final[tuple[int, ...]] = (2160, 1440, 1080, 720, 480, 360)
"""Heights worth offering. Anything else is rounded down to one of these, so a
source with fourteen renditions still produces a readable list."""

MAX_OPTIONS: Final[int] = 6


def build_quality_options(
    metadata: MediaMetadata,
    *,
    max_bytes: int | None = None,
    allow_merge: bool = False,
    prefer_compatible: bool = False,
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
        prefer_compatible: Whether the engine will prefer H.264. It must be the
            same answer given to :func:`selection_for`, because the size shown
            has to be the size of the rendition actually taken: H.264 is
            consistently larger than AV1 at the same resolution, so quoting one
            and downloading the other understates every rung.

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

    for bucket, candidates in _by_height(metadata.video_formats):
        video = _representative(candidates, prefer_compatible=prefer_compatible)
        estimated = _delivered_bytes(video.filesize_bytes, video.has_audio, audio_overhead)
        if _exceeds(estimated, max_bytes):
            continue
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


def resolve_auto(options: Sequence[QualityOption], *, ceiling: int | None) -> QualityOption:
    """Return the highest-quality option that will fit ``ceiling``.

    Args:
        options: What the source offers, best first.
        ceiling: Largest deliverable size, or ``None`` for no limit.

    Returns:
        The best option that fits. Options of *unknown* size are treated as
        candidates rather than skipped - an absent size is not a large one, and
        the engine enforces the real ceiling while streaming, so guessing
        pessimistically here would refuse renditions that would have been fine.

    Raises:
        FormatUnavailableError: If the source offers nothing at all.
    """
    if not options:
        message = "this source offers nothing that can be fetched"
        raise FormatUnavailableError(message)

    rungs = [option for option in options if option.height is not None]
    audio = [option for option in options if option.is_audio_only]
    unbounded = next((option for option in options if option.key == BEST_KEY), None)

    if ceiling is None:
        # Nothing to weigh against, so "best" means best.
        return unbounded or (rungs[0] if rungs else options[0])

    # `BEST_KEY` is deliberately **not** a candidate once a ceiling exists. It
    # asks the engine for the best rendition full stop, and its declared size -
    # when it has one at all - describes the source rather than the rendition.
    # It is precisely the choice that overshoots and fails after the download.
    fits = [
        option for option in rungs if option.approx_bytes is None or option.approx_bytes <= ceiling
    ]
    if fits:
        # Rungs are emitted tallest-first, so the first that fits is the best.
        return fits[0]

    # Every known rung is too large. Sound is a real answer - a two-hour talk
    # that will not fit as video usually still fits as audio - and it beats
    # refusing outright.
    if audio:
        return audio[0]
    if rungs:
        return rungs[-1]
    return unbounded or options[0]


def _by_height(formats: Sequence[VideoFormat]) -> list[tuple[int, list[VideoFormat]]]:
    """Group renditions by the rung they round down to, tallest first.

    Grouping rather than taking the first of each height is the difference
    between a size and a shrug: a platform lists several renditions per
    resolution and only some of them declare a size, so picking whichever came
    first leaves the rung marked "unknown" while a sibling that knows its own
    size goes unread.
    """
    buckets: dict[int, list[VideoFormat]] = {}
    for video in formats:
        if video.height is None:
            continue
        rung = _bucket(video.height)
        if rung is not None:
            buckets.setdefault(rung, []).append(video)
    return sorted(buckets.items(), key=lambda item: item[0], reverse=True)


def _representative(candidates: Sequence[VideoFormat], *, prefer_compatible: bool) -> VideoFormat:
    """Return the rendition whose size should be shown for a rung.

    It must be the one the engine will actually take, or the number is a
    plausible lie. Preference order: a declared size first, because a rung whose
    size is unknown cannot be planned around; then the codec the selection will
    ask for, since H.264 is consistently larger than AV1 at the same resolution
    and quoting the AV1 figure would understate every rung by a third.
    """
    sized = [video for video in candidates if video.filesize_bytes is not None] or list(candidates)
    if prefer_compatible:
        compatible = [video for video in sized if _is_compatible(video)]
        if compatible:
            return max(compatible, key=lambda video: video.filesize_bytes or 0)
    return max(sized, key=lambda video: video.filesize_bytes or 0)


def _is_compatible(video: VideoFormat) -> bool:
    """Return whether an ordinary player can be expected to decode this."""
    codec = (video.video_codec or "").lower()
    return codec.startswith(("avc1", "h264"))


def _merge_audio_bytes(metadata: MediaMetadata) -> int:
    """Return the size of the audio track a merge would attach.

    The **largest** known audio rendition, because that is the one that gets
    attached: the merge asks for the best audio available, so quoting a smaller
    stream describes a download that will not happen. Measured against a real
    source, assuming the smallest understated the total by 4% - which is both
    wrong and, near a hard upload ceiling, wrong in the dangerous direction.

    Zero when nothing declares a size, which keeps an unknown from being
    presented as a certainty.
    """
    sizes = [
        audio.filesize_bytes for audio in metadata.audio_formats if audio.filesize_bytes is not None
    ]
    return max(sizes) if sizes else 0


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
