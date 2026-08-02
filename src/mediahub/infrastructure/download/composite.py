"""Two engines, one port: video first, pictures when it finds none.

The video engine handles almost everything and is the one to ask first. What it
cannot do it says so precisely - :class:`NoPlayableMediaError` means "this post
was read successfully and holds no stream", which is exactly the shape of a
photo post - and that is the signal to ask the image engine instead.

**The fallback is narrow on purpose.** Only "no playable media" and "no
extractor for this" hand over. A private video, a deleted post, an expired
session or a network block fail identically in the second engine, and trying it
anyway would double the time to report something already final while replacing a
precise message with a vaguer one.

Ordering is not configurable and should not be. Asking the image engine first
would strip the audio from every video on the platforms both engines claim,
which is a silent downgrade nobody would think to look for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.download.errors import (
    DownloadError,
    NoPlayableMediaError,
    UnsupportedProviderError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.ports import (
        DownloadCapabilities,
        DownloaderPort,
        DownloadRequest,
        DownloadResult,
        MediaMetadata,
        ProgressCallback,
    )
    from mediahub.application.workspace.ports import WorkspaceScope

_HANDOVER: Final[tuple[type[DownloadError], ...]] = (
    NoPlayableMediaError,
    UnsupportedProviderError,
)
"""The only two refusals that mean "ask the other engine".

Both say the same thing in different words: *this* engine has nothing to fetch
here. Every other failure is about the source or the network and would repeat."""


class CompositeDownloader:
    """Tries the video engine, then the image engine."""

    __slots__ = ("_images", "_video")

    def __init__(self, video: DownloaderPort, images: DownloaderPort) -> None:
        """Wire the two engines in the only order that is correct."""
        self._video = video
        self._images = images

    @property
    def name(self) -> str:
        """Return both engine names, because either may have done the work."""
        return f"{self._video.name}+{self._images.name}"

    def capabilities(self) -> DownloadCapabilities:
        """Return the video engine's capabilities.

        Deliberately not a merge. Every capability here describes what can be
        done with a *stream* - format selection, audio extraction, resume - and
        the image engine has none of them; reporting the union would promise
        callers a rendition ladder for a photograph.
        """
        return self._video.capabilities()

    def supports(self, url: str) -> bool:
        """Return whether either engine claims this URL."""
        return self._video.supports(url) or self._images.supports(url)

    async def probe(self, url: str, *, timeout_seconds: float | None = None) -> MediaMetadata:
        """Read metadata, falling back to the image engine when asked to."""
        try:
            return await self._video.probe(url, timeout_seconds=timeout_seconds)
        except _HANDOVER as refusal:
            if not self._images.supports(url):
                raise
            logger.bind(url_host=_host(url), reason=refusal.code).info(
                "No stream here; asking the image engine"
            )
            return await self._images.probe(url, timeout_seconds=timeout_seconds)

    async def fetch(
        self,
        request: DownloadRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        """Download, falling back to the image engine when asked to."""
        try:
            return await self._video.fetch(
                request, workspace, on_progress=on_progress, cancellation=cancellation
            )
        except _HANDOVER:
            if not self._images.supports(request.url):
                raise
            return await self._images.fetch(
                request, workspace, on_progress=on_progress, cancellation=cancellation
            )


def _host(url: str) -> str:
    """Return a URL's host, for a log line that must not carry the full URL."""
    authority = url.split("//", maxsplit=1)[-1]
    return authority.split("/", maxsplit=1)[0].split("?", maxsplit=1)[0].lower()
