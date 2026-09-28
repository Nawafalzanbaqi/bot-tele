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


def base_options(
    settings: DownloadSettings, *, proxy: str | None = None, impersonate: str | None = None
) -> dict[str, Any]:
    """Return options shared by probing and downloading.

    Every entry marked *security* is load-bearing; changing one changes what the
    engine is allowed to do to the device.

    Args:
        settings: Engine configuration.
        proxy: Egress to route this request through, or ``None`` for the direct
            path. Passed per call rather than read from ``settings`` because
            **which** requests need an egress is a decision, and it is not this
            module's: see
            :class:`~mediahub.infrastructure.download.ytdlp.downloader.ProxyPolicy`.
        impersonate: Browser fingerprint to present (a curl_cffi target name
            such as ``chrome``), or ``None`` for yt-dlp's own client. Also a per
            call decision, made by the same policy. Kept as a string here so
            this module stays free of yt-dlp imports; the engine factory turns
            it into the library's own target type.
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
        # security: no shell, and no *arbitrary* post-processing. The list is
        # empty rather than absent so nothing this module did not ask for can
        # run; yt-dlp's own merger is not on it and is invoked internally when
        # a format expression names two streams.
        "postprocessors": [],
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
    if settings.cookies_file is not None:
        # Presented to every source, on both probe and download - a platform
        # that hides media from an anonymous session hides it at *probe* time,
        # which is where the failure is reported as "no video in this post".
        options["cookiefile"] = str(settings.cookies_file)
    if proxy:
        options["proxy"] = proxy
    if impersonate:
        options["impersonate"] = impersonate
    return options


def build_probe_options(
    settings: DownloadSettings,
    *,
    socket_timeout_seconds: float | None = None,
    proxy: str | None = None,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """Return options for a metadata-only extraction.

    ``extract_flat`` is what keeps a probe cheap: a playlist is recognised and
    counted without resolving every entry, so probing a channel costs one
    request rather than five hundred. ``playlistend`` keeps even that flat list
    to one entry, which is the only one the adapter goes on to resolve.

    ``noplaylist`` is what makes a *video* link that happens to carry a
    playlist parameter mean the video: ``watch?v=X&list=Y`` is the clip the
    person is looking at, not the first of the list beside it. A URL that is
    only a playlist is unaffected and still comes back as a collection.
    """
    options = base_options(settings, proxy=proxy, impersonate=impersonate)
    options.update(
        {
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "playlistend": 1,
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
    proxy: str | None = None,
    impersonate: str | None = None,
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
        proxy: Egress to route this download through, or ``None`` for direct.
        impersonate: Browser fingerprint to present, or ``None``.
    """
    options = base_options(settings, proxy=proxy, impersonate=impersonate)
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
            # Where two streams are combined, the result is MP4. Without this
            # yt-dlp keeps whichever container the streams came in - usually
            # WebM - and the file arrives complete, at the right resolution, and
            # refuses to play in a chat client. With H.264 and AAC selected
            # first (see `format_selection`) this is a remux, not a re-encode:
            # seconds on a Pi rather than minutes.
            "merge_output_format": "mp4",
        }
    )
    if not request.allow_playlist:
        options["playlist_items"] = "1"
    if request.socket_timeout_seconds is not None:
        options["socket_timeout"] = request.socket_timeout_seconds
    if settings.rate_limit_bytes_per_second:
        options["ratelimit"] = settings.rate_limit_bytes_per_second
    return options
