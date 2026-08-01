"""Placeholder download engine.

Implements :class:`~mediahub.application.download.ports.DownloaderPort` by
declining every request. It is installed when ``download.enabled`` is false, so
the container is always fully wired and the missing capability is visible in the
API (``501 Not Implemented``) instead of hidden behind an ``AttributeError``.

It is also the reference for what "does nothing, correctly" looks like: it
implements the whole port, so the type checker proves the real engine and the
null one agree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.errors import DownloaderNotConfiguredError
from mediahub.application.download.ports import DownloadCapabilities

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.ports import (
        DownloadRequest,
        DownloadResult,
        MediaMetadata,
        ProgressCallback,
    )
    from mediahub.application.workspace.ports import WorkspaceScope

ENGINE_NAME = "null"


class NullDownloader:
    """A downloader that supports nothing and fetches nothing."""

    __slots__ = ()

    @property
    def name(self) -> str:
        """Return the engine's name."""
        return ENGINE_NAME

    def capabilities(self) -> DownloadCapabilities:
        """Report that this engine can do nothing at all."""
        return DownloadCapabilities(
            engine=ENGINE_NAME,
            version="0",
            supports_probe=False,
            supports_format_selection=False,
            supports_audio_only=False,
            supports_resume=False,
            supports_playlists=False,
            supports_live=False,
            supports_thumbnails=False,
            requires_external_merger=False,
            max_concurrent_fragments=1,
        )

    def supports(self, url: str) -> bool:
        """Return ``False`` for every URL - no engine is configured."""
        del url
        return False

    async def probe(self, url: str, *, timeout_seconds: float | None = None) -> MediaMetadata:
        """Refuse to probe, with a typed and actionable error.

        Raises:
            DownloaderNotConfiguredError: Always.
        """
        del timeout_seconds
        logger.bind(url=url).warning("Probe requested but no download engine is configured")
        raise DownloaderNotConfiguredError(url)

    async def fetch(
        self,
        request: DownloadRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        """Refuse the transfer, with a typed and actionable error.

        Raises:
            DownloaderNotConfiguredError: Always.
        """
        del workspace, on_progress, cancellation
        logger.bind(url=request.url).warning("Download requested but no engine is configured")
        raise DownloaderNotConfiguredError(request.url)
