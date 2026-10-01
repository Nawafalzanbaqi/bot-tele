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
from mediahub.domain.media.enums import MediaType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.download.ports import MediaMetadata, VideoFormat

BEST_KEY: Final[str] = "best"
AUDIO_KEY: Final[str] = "audio"
ORIGINAL_KEY: Final[str] = "orig"
"""Whatever the source is, as published - no rendition to choose between.

Images, stories, photo slideshows and direct links to a file all report no
streams at all. They are not unfetchable; there is simply nothing to pick, so
the menu offers one entry rather than none.
"""
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

_STREAMLESS_KINDS: Final[frozenset[MediaType]] = frozenset({MediaType.IMAGE, MediaType.OTHER})
"""Kinds that legitimately publish no streams, and are still fetchable."""


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
                # What the *engine* is asked for, which is not what the rung is
                # called. See `_short_side`: on a vertical video the two differ,
                # and using the label here caps the download below the rendition
                # it promises.
                height=video.height or bucket,
                approx_bytes=estimated,
                is_audio_only=False,
            )
        )

    if not options and not metadata.has_audio and metadata.kind in _STREAMLESS_KINDS:
        # No streams were enumerated, and the probe says this is not the sort
        # of thing that has any: an image post, a story, a photo slideshow or a
        # bare link to a file. They are perfectly fetchable and used to be
        # refused with "nothing here can be fetched" - wrong, and unactionable.
        #
        # Narrowed by kind on purpose. A *video* with no formats really is a
        # failure, and offering to fetch it would replace an honest refusal at
        # probe time with a download that fails later having spent a slot.
        options.append(
            QualityOption(
                key=ORIGINAL_KEY,
                label="Original",
                format_id=None,
                height=None,
                approx_bytes=metadata.expected_bytes,
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
    if chosen.key == ORIGINAL_KEY:
        # No rendition to choose and nothing to merge: take what is published.
        # Asking for a merge here would make the engine look for a second
        # stream that does not exist and fall through its alternatives.
        return FormatSelection.best()
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

    original = next((option for option in options if option.key == ORIGINAL_KEY), None)
    if original is not None:
        # One entry, because the source published one thing. Weighing it
        # against a ceiling would only ever mean refusing it, and the engine
        # enforces the real limit while streaming anyway.
        return original

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

    if not rungs and unbounded is not None:
        # No rung was *offered*, which is not the same as every rung being too
        # large: a 352x640 clip sits below the lowest rung on its short side,
        # so the ladder is empty while the video plainly exists. Until
        # 2026-10-01 this fell through to the audio fallback below and a
        # Reddit clip arrived as a 190 KB m4a. The honest answer is the best
        # rendition there is; the engine still enforces the ceiling while
        # streaming.
        return unbounded

    # Every known rung is too large. Sound is a real answer - a two-hour talk
    # that will not fit as video usually still fits as audio - and it beats
    # refusing outright.
    if audio:
        return audio[0]
    return rungs[-1] if rungs else (unbounded or options[0])


def _short_side(video: VideoFormat) -> int | None:
    """Return the dimension a quality label actually names.

    **The short side, not the stored height.** "1080p" has always meant 1080
    lines across the narrow dimension: a 1920x1080 film and a 1080x1920 phone
    clip are both 1080p, and every platform, player and person calls them that.

    Reading ``height`` instead breaks on vertical video, which is most of what
    TikTok, Reels, Shorts and X carry. A 1080x1920 rendition has ``height``
    1920, so it was bucketed as *1440p* - and the damage was not only the wrong
    word on a button. The rung's engine constraint was that same number, so
    asking for "1440p" capped the download at 1440 pixels tall and **excluded
    the 1920-tall rendition it was named after**, quietly delivering 720p while
    reporting 1440p. Measured on a real TikTok source: 6.22 MB taken when
    16.86 MB was published.
    """
    if video.width is None or video.height is None:
        return video.height
    return min(video.width, video.height)


def _by_height(formats: Sequence[VideoFormat]) -> list[tuple[int, list[VideoFormat]]]:
    """Group renditions by the rung they round down to, largest first.

    Grouping rather than taking the first of each height is the difference
    between a size and a shrug: a platform lists several renditions per
    resolution and only some of them declare a size, so picking whichever came
    first leaves the rung marked "unknown" while a sibling that knows its own
    size goes unread.
    """
    buckets: dict[int, list[VideoFormat]] = {}
    for video in formats:
        side = _short_side(video)
        if side is None:
            continue
        rung = _bucket(side)
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


COMPATIBLE_VIDEO_CODECS: Final[tuple[str, ...]] = ("avc1", "h264", "hvc1", "hev1", "hevc", "h265")
"""Codec prefixes a chat client plays inline: H.264 first, H.265 close behind.

VP9 and AV1 are deliberately absent. They are smaller at the same resolution and
most phone players and chat clients cannot decode them; a file in one of them is
delivered as a document rather than an inline video, and the user is told why.
"""


def is_compatible_codec(codec: str | None) -> bool:
    """Return whether a video codec name is one an ordinary player decodes."""
    return (codec or "").lower().startswith(COMPATIBLE_VIDEO_CODECS)


def _is_compatible(video: VideoFormat) -> bool:
    """Return whether an ordinary player can be expected to decode this."""
    return is_compatible_codec(video.video_codec)


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
