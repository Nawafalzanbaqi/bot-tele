"""Use case: report what this deployment can currently do."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.application.common.use_case import Query
from mediahub.application.download.dto import CapabilitiesSummary

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.delivery.ports import DeliveryRouter
    from mediahub.application.download.ports import DownloaderPort


class DescribeCapabilities:
    """Combine the engine's and the destination's limits into one answer.

    Worth having as a use case rather than letting each interface read the
    configuration: the number a person needs is the *effective* ceiling - the
    smaller of what can be downloaded and what can be delivered - and computing
    it in three interfaces would produce three answers.
    """

    def __init__(
        self,
        *,
        downloader: DownloaderPort,
        delivery: DeliveryRouter,
        max_item_bytes: int,
        allow_live: bool = False,
        allow_playlist: bool = False,
    ) -> None:
        """Wire the use case to the engine, the destination and the limits."""
        self._downloader = downloader
        self._delivery = delivery
        self._max_item_bytes = max_item_bytes
        self._allow_live = allow_live
        self._allow_playlist = allow_playlist

    async def execute(self, request: Query | None = None) -> CapabilitiesSummary:
        """Return the effective capabilities of this deployment."""
        del request
        engine = self._downloader.capabilities()
        destination = self._delivery.default_capabilities()
        return CapabilitiesSummary(
            engine=engine.engine,
            engine_version=engine.version,
            max_item_bytes=self._max_item_bytes,
            delivery_provider=destination.provider,
            delivery_max_bytes=destination.maximum_file_size,
            effective_max_bytes=min(self._max_item_bytes, destination.maximum_file_size),
            supports_audio_only=engine.supports_audio_only,
            allow_live=self._allow_live,
            allow_playlist=self._allow_playlist,
        )
