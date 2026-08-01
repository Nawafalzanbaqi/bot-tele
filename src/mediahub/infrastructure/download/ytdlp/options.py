"""Builds yt-dlp option dictionaries.

Kept separate from the adapter, and pure, so the options can be asserted in
tests without instantiating an engine. Several of them are security controls
rather than tuning, and those are called out individually below - an innocuous
looking default here is the difference between "writes into the lease" and
"writes into the user's home directory".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mediahub.application.download.ports import DownloadRequest
    from mediahub.shared.config.settings import DownloadSettings

OUTPUT_TEMPLATE = "%(id).60s.%(ext)s"
"""Names are generated from the provider's item id, never from its title.

Titles are attacker-controlled and arrive with slashes, control characters and
right-to-left overrides in them. The id is short, opaque and safe, and the real
title is preserved in metadata where it cannot become a path.
"""


def base_options(settings: DownloadSettings) -> dict[str, Any]:
    """Return options shared by probing and downloading.

    Every entry marked *security* is load-bearing; changing one changes what the
    engine is allowed to do to the device.
    """
    options: dict[str, Any] = {
        # Quiet: MediaHub owns its own logging and progress reporting.
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "consoletitle": False,
        # security: never write a cache outside the workspace (yt-dlp otherwise
        # uses ~/.cache/yt-dlp).
        "cachedir": False,
        # security: no shell, no external programs, no post-processing that
        # could execute something. FFmpeg belongs to a different subsystem.
        "postprocessors": [],
        "prefer_ffmpeg": False,
        "exec_cmd": [],
        # security: do not pretend to be somewhere else. Bypassing geo-blocks is
        # a deliberate operator decision, not a default.
        "geo_bypass": False,
        # Fail loudly rather than skipping entries; the caller decides.
        "ignoreerrors": False,
        "no_overwrites": False,
        "allow_unplayable_formats": False,
        "check_formats": False,
        # Names are generated, never taken from the source.
        "restrictfilenames": True,
        "windowsfilenames": True,
        "trim_file_name": 100,
        "socket_timeout": settings.socket_timeout_seconds,
        "retries": settings.retries,
        "fragment_retries": settings.fragment_retries,
        "extractor_retries": settings.extractor_retries,
    }
    if settings.user_agent:
        options["http_headers"] = {"User-Agent": settings.user_agent}
    return options


def build_probe_options(
    settings: DownloadSettings,
    *,
    allow_playlist: bool = False,
    socket_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Return options for a metadata-only extraction.

    ``extract_flat`` is what keeps a probe cheap: a playlist is recognised and
    counted without resolving every entry, so probing a channel costs one
    request rather than five hundred.
    """
    options = base_options(settings)
    options.update(
        {
            "skip_download": True,
            "noplaylist": not allow_playlist,
            "extract_flat": "in_playlist",
            "writethumbnail": False,
        }
    )
    if socket_timeout_seconds is not None:
        options["socket_timeout"] = socket_timeout_seconds
    return options


def build_download_options(
    settings: DownloadSettings,
    request: DownloadRequest,
    *,
    directory: Path,
    format_expression: str,
    progress_hook: Callable[[Mapping[str, Any]], None],
    postprocessor_hook: Callable[[Mapping[str, Any]], None],
) -> dict[str, Any]:
    """Return options for an actual download into ``directory``.

    Args:
        settings: Engine configuration.
        request: What the caller asked for.
        directory: The workspace lease directory. **Nothing may be written
            outside it**, which is why both ``home`` and ``temp`` are pinned
            here - yt-dlp otherwise places part-files next to the process's
            working directory.
        format_expression: Result of the format-selection translation.
        progress_hook: Receives yt-dlp download hook payloads.
        postprocessor_hook: Receives yt-dlp post-processor hook payloads.
    """
    options = base_options(settings)
    options.update(
        {
            # security: confine every write, including temporary part-files.
            "paths": {"home": str(directory), "temp": str(directory)},
            "outtmpl": {"default": OUTPUT_TEMPLATE, "thumbnail": OUTPUT_TEMPLATE},
            "format": format_expression,
            "noplaylist": not request.allow_playlist,
            "continuedl": request.resume,
            "writethumbnail": request.include_thumbnail,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            # A belt to the streaming ceiling's braces: yt-dlp refuses formats
            # that declare a larger size, and the hook stops those that lie.
            "max_filesize": request.max_bytes,
            "concurrent_fragment_downloads": settings.concurrent_fragments,
        }
    )
    if not request.allow_playlist:
        options["playlist_items"] = "1"
    if request.socket_timeout_seconds is not None:
        options["socket_timeout"] = request.socket_timeout_seconds
    if settings.rate_limit_bytes_per_second:
        options["ratelimit"] = settings.rate_limit_bytes_per_second
    return options
