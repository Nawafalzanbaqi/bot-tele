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

from typing import TYPE_CHECKING

from mediahub.application.download.ports import FormatPreference

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.ports import FormatSelection


def build_format_expression(selection: FormatSelection) -> str:
    """Return the yt-dlp ``format`` expression for a selection.

    Args:
        selection: What the caller wants.

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
        return f"{_merged(selection)}/{_best_single(selection)}"
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
        return f"{constrained}/b{_constraints(selection, include_container=False)}/b"
    return f"{constrained}/b"


def _merged(selection: FormatSelection) -> str:
    """Return an expression for separate video and audio streams."""
    video = f"bv*{_constraints(selection, include_container=False)}"
    return f"{video}+ba"


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
