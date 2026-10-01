"""Thumbnails whose URL names no image type are given one before the engine writes them.

Snapchat Spotlight thumbnails are signed CDN URLs whose last path segment ends
in a token (``...IRZXSOY``). yt-dlp derives the file extension from the URL,
reads the token as the extension, judges it unsafe and aborts the *whole*
download - media included - with "The extracted extension ('IRZXSOY') is
unusual and will be skipped for safety reasons" (2026-10-01, every Spotlight
link). The engine honours an explicit ``ext`` on a thumbnail entry, so one is
supplied when the derived one is not an image type. Nothing is relaxed: the
safety check still runs, on a name that passes it.
"""

from __future__ import annotations

from typing import Any, Final

try:
    from yt_dlp.postprocessor import PostProcessor as _EnginePostProcessor
except ImportError:  # pragma: no cover - the engine is an optional install
    _EnginePostProcessor = None

_IMAGE_TYPES: Final[frozenset[str]] = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif"}
)
DEFAULT_THUMBNAIL_EXT: Final[str] = "jpg"


def fix_thumbnail_extensions(info: dict[str, Any]) -> int:
    """Set ``ext`` on every thumbnail whose URL does not name an image type.

    Args:
        info: The engine's info dict, modified in place.

    Returns:
        How many thumbnail entries were given an extension.
    """
    fixed = 0
    for thumbnail in info.get("thumbnails") or ():
        if not isinstance(thumbnail, dict) or thumbnail.get("ext"):
            continue
        url = thumbnail.get("url")
        if not isinstance(url, str):
            continue
        if guessed_extension(url) not in _IMAGE_TYPES:
            thumbnail["ext"] = DEFAULT_THUMBNAIL_EXT
            fixed += 1
    return fixed


def guessed_extension(url: str) -> str:
    """Return the extension the engine would derive from ``url``, lower-cased.

    Mirrors yt-dlp's ``determine_ext``: the text after the last dot of the URL
    with its query removed, when it is purely alphanumeric; otherwise nothing.
    """
    guess = url.partition("?")[0].rpartition(".")[2]
    return guess.lower() if guess.isalnum() else ""


def attach_thumbnail_fixer(engine: Any) -> bool:
    """Register the fixer on a real engine as a ``pre_process`` post-processor.

    The engine runs it after extraction and before anything is written, which
    is where the thumbnail name is decided. A test double without
    ``add_post_processor`` is left alone, and so is an environment without the
    engine installed.

    Returns:
        Whether the fixer was attached.
    """
    add = getattr(engine, "add_post_processor", None)
    if add is None or _EnginePostProcessor is None:
        return False

    class _ThumbnailExtensionFixer(_EnginePostProcessor):  # type: ignore[misc]
        def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
            fix_thumbnail_extensions(info)
            return [], info

    add(_ThumbnailExtensionFixer(), when="pre_process")
    return True
