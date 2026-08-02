"""Use case: describe what is at a URL, without spending bandwidth on it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.dto import SourceSummary
from mediahub.application.download.quality import build_quality_options

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.dto import ProbeSourceQuery
    from mediahub.application.download.ports import DownloaderPort


class ProbeSource:
    """Read a source's metadata and work out what may be offered for it.

    This is the cheap half of acquisition and the reason admission is possible:
    size, duration and liveness are all known before anything is downloaded.
    A caller shows the result to a person, who picks a quality.
    """

    def __init__(
        self,
        *,
        downloader: DownloaderPort,
        max_bytes: int | None = None,
        allow_merge: bool = False,
    ) -> None:
        """Wire the use case to the engine and this deployment's ceiling.

        ``allow_merge`` is needed here even though this use case downloads
        nothing: it decides whether the size shown against a rung includes the
        audio track that will be attached to it. The menu and the acquisition
        must agree, or the number on the button is not the number delivered.
        """
        self._downloader = downloader
        self._max_bytes = max_bytes
        self._allow_merge = allow_merge

    async def execute(self, request: ProbeSourceQuery) -> SourceSummary:
        """Return a description of the source and the choices it supports.

        Args:
            request: The raw URL a caller supplied.

        Returns:
            A summary suitable for showing to a person.

        Raises:
            InvalidUrlError: If the URL is refused by policy.
            UnsupportedProviderError: If no extractor claims it.
            MetadataUnavailableError: If the source cannot be described.
        """
        metadata = await self._downloader.probe(request.url)
        qualities = build_quality_options(
            metadata, max_bytes=self._max_bytes, allow_merge=self._allow_merge
        )
        thumbnail = metadata.best_thumbnail()

        logger.bind(
            provider=metadata.provider,
            kind=metadata.kind.value,
            options=len(qualities),
        ).info("Probed source")

        return SourceSummary(
            url=metadata.url,
            provider=metadata.provider,
            title=metadata.title,
            kind=metadata.kind,
            is_live=metadata.is_live,
            is_playlist=metadata.is_playlist,
            qualities=qualities,
            duration_seconds=metadata.duration_seconds,
            thumbnail_url=None if thumbnail is None else thumbnail.url,
            expected_bytes=metadata.expected_bytes,
        )
