"""The still-image download engine.

Separate from the video engine because the two answer different questions, not
because one is a fallback for the other's bugs: yt-dlp fetches streams, and
gallery-dl fetches pictures. Both sit behind
:class:`~mediahub.application.download.ports.DownloaderPort`.
"""

from __future__ import annotations

from mediahub.infrastructure.download.gallerydl.downloader import (
    ENGINE_NAME,
    GalleryDlDownloader,
)

__all__ = ["ENGINE_NAME", "GalleryDlDownloader"]
