"""Translates engine-neutral intent into a yt-dlp format expression.

Pure string building, no I/O, no yt-dlp import - which makes the most
fiddly logic in the adapter exhaustively testable.

The guiding rule is **prefer a single stream**. Choosing separate video and
audio streams produces a better result but requires merging them with FFmpeg,
which this engine does not own and which the target device pays for in minutes
of CPU. Merging is therefore opt-in per request
(:attr:`~mediahub.application.download.ports.FormatSelection.allow_merge`), and
off by default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mediahub.application.download.ports import FormatPreference

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.ports import FormatSelection


def build_format_expression(
    selection: FormatSelection, *, muxed_before_fallback: bool = False
) -> str:
    """Return the yt-dlp ``format`` expression for a selection.

    Args:
        selection: What the caller wants.
        muxed_before_fallback: When compatibility is asked for, try an
            already-muxed file that is not declared VP9/AV1 before the
            "anything" fallback. For hosts whose adaptive ladder is VP9-only
            while their progressive MP4 is H.264 (Instagram, Facebook), that
            file is the one that plays inline; the engine reports no codec for
            it, so by codec alone it would lose. See
            :data:`MUXED_NOT_INCOMPATIBLE`.

    Returns:
        A yt-dlp format expression. Alternatives are separated by ``/`` so that
        yt-dlp falls back gracefully when the preferred shape is unavailable;
        the final alternative is always permissive, so a source with unusual
        formats still yields something rather than failing.
    """
    if selection.preference is FormatPreference.SPECIFIC:
        # The caller named a format; still provide a fallback so a stale id
        # from an old probe degrades to a usable download rather than nothing.
        return f"{selection.format_id}/{_best_single(selection)}"

    if selection.preference is FormatPreference.AUDIO_ONLY:
        return _audio_only(selection)

    if selection.preference is FormatPreference.VIDEO_ONLY:
        return _video_only(selection)

    if selection.allow_merge:
        merged = _merged(selection, muxed_before_fallback=muxed_before_fallback)
        return f"{merged}/{_best_single(selection)}"
    return _best_single(selection)


def _constraints(selection: FormatSelection, *, include_container: bool = True) -> str:
    """Return the bracketed filters shared by every video expression."""
    filters: list[str] = []
    if selection.max_height is not None:
        filters.append(f"[height<={selection.max_height}]")
    if selection.max_filesize_bytes is not None:
        # `<?` means "unknown size passes" - a format with no declared size is
        # not excluded here, because the hard ceiling is enforced while
        # streaming, where a declared size cannot lie.
        filters.append(f"[filesize<?{selection.max_filesize_bytes}]")
    if include_container and selection.prefer_container:
        filters.append(f"[ext={selection.prefer_container}]")
    return "".join(filters)


def _best_single(selection: FormatSelection) -> str:
    """Return an expression for the best single, already-muxed file."""
    constrained = f"b{_constraints(selection)}"
    if selection.prefer_container:
        # Try the preferred container first, then any container.
        loose = _constraints(selection, include_container=False)
        return f"{constrained}/b{loose}/b{IMAGE_FALLBACK}"
    return f"{constrained}/b{IMAGE_FALLBACK}"


IMAGE_FALLBACK: Final[str] = "/b*"
"""The very last resort: any format at all, including an image.

``b`` means "best file with both video and audio" and refuses a picture
outright; a Threads or TikTok image is a format with neither stream, and
``b*`` is the selector that takes it. Last, so nothing that has a video
changes."""

COMPATIBLE_VIDEO: Final[str] = "[vcodec^=avc1]"
"""H.264. Decoded in hardware by every phone, browser and chat client."""

COMPATIBLE_VIDEO_HEVC: Final[tuple[str, ...]] = ("[vcodec^=hvc1]", "[vcodec^=hev1]")
"""H.265 in its two MP4 signalling flavours.

Second choice, not first: Telegram's clients play it inline on every phone made
in the last decade and it is smaller than H.264 at the same resolution, but a
few desktop clients still hand it to a system decoder that may be missing.
"""

COMPATIBLE_AUDIO: Final[str] = "[acodec^=mp4a]"
"""AAC. The audio half of the same bargain."""

MUXED_NOT_INCOMPATIBLE: Final[str] = "[vcodec!^=?vp0][vcodec!^=?av01]"
"""A muxed file whose codec is *not declared* VP9 or AV1 - unknown passes.

The ``?`` makes an absent codec match: Instagram's progressive renditions and
Facebook's ``sd``/``hd`` carry no codec field at all, and they are the H.264
files (2026-10-01, ffprobe on both). Height is loosened the same way where it
is applied, because those files carry no frame size either."""


def _loose_constraints(selection: FormatSelection) -> str:
    """Return the shared filters with unknown height and size admitted."""
    filters: list[str] = []
    if selection.max_height is not None:
        filters.append(f"[height<=?{selection.max_height}]")
    if selection.max_filesize_bytes is not None:
        filters.append(f"[filesize<?{selection.max_filesize_bytes}]")
    return "".join(filters)


def _merged(selection: FormatSelection, *, muxed_before_fallback: bool = False) -> str:
    """Return an expression for separate video and audio streams.

    When compatibility is asked for, H.264 + AAC is tried **first**, H.265 + AAC
    second, and any codec last. That ordering is the whole point: the streams a
    platform considers best are increasingly AV1 or VP9 with Opus, which are
    smaller for the same resolution and which most players cannot decode.
    Taking them produces a file that is the right resolution, arrives intact,
    and does not play - the least useful of all possible outcomes, because
    nothing reports that anything went wrong. (When only those exist, the
    acquisition sends the result as a document rather than an inline video, and
    says so.)

    The H.264/H.265 + AAC pairs are also what make the result muxable into MP4
    without re-encoding, which on a small device is the difference between
    seconds and minutes.
    """
    constraints = _constraints(selection, include_container=False)
    fallback = f"bv*{constraints}+ba"
    if not selection.prefer_compatible:
        return fallback
    tiers = [f"bv*{COMPATIBLE_VIDEO}{constraints}+ba{COMPATIBLE_AUDIO}"]
    tiers.extend(f"bv*{hevc}{constraints}+ba{COMPATIBLE_AUDIO}" for hevc in COMPATIBLE_VIDEO_HEVC)
    if muxed_before_fallback:
        tiers.append(f"b{MUXED_NOT_INCOMPATIBLE}{_loose_constraints(selection)}")
    tiers.append(fallback)
    return "/".join(tiers)


def _audio_only(selection: FormatSelection) -> str:
    """Return an expression for the best audio-only stream."""
    filters = ""
    if selection.max_filesize_bytes is not None:
        filters = f"[filesize<?{selection.max_filesize_bytes}]"
    if selection.prefer_container:
        return f"ba[ext={selection.prefer_container}]{filters}/ba{filters}/ba/b"
    return f"ba{filters}/ba/b"


def _video_only(selection: FormatSelection) -> str:
    """Return an expression for the best video-only stream."""
    return f"bv*{_constraints(selection, include_container=False)}/bv*/b"
