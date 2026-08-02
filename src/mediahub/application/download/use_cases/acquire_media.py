"""Use case: acquire a source, deliver it, and forget the bytes.

This is where the product's central rule is executed: the local copy exists
only inside a workspace lease, and leaving that lease deletes it. Custody
transfers to the destination, and what survives is a few hundred bytes of
history.

The ordering matters and must not be "optimised": the receipt is obtained and
the journal entry written **inside** the lease, so that if anything fails the
files are still removed; and the lease is released only after the destination
has confirmed. Deleting earlier risks losing both copies.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.delivery.errors import ArtifactTooLargeError
from mediahub.application.delivery.ports import DeliveryKind, DeliveryRequest
from mediahub.application.download.dto import AcquisitionSummary
from mediahub.application.download.journal import JournalEntry
from mediahub.application.download.ports import DownloadRequest
from mediahub.application.download.quality import build_quality_options, selection_for
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.media.enums import MediaType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.common.ports import Clock
    from mediahub.application.delivery.ports import (
        DeliveryCapabilities,
        DeliveryProgressCallback,
        DeliveryRouter,
    )
    from mediahub.application.download.dto import AcquireMediaCommand, QualityOption
    from mediahub.application.download.journal import AcquisitionJournal
    from mediahub.application.download.ports import (
        DownloaderPort,
        MediaMetadata,
        ProgressCallback,
    )
    from mediahub.application.workspace.ports import ArtifactRef, WorkspaceLeasing

_RESERVE_FACTOR = 2.2
"""Input plus any derived artifact plus headroom. Reserving exactly the
expected size leaves nothing for a thumbnail or an under-estimate."""

_DEFAULT_RESERVE_BYTES = 256 * 1024 * 1024
"""Used when the source declares no size, which is common."""

_KIND_MAP: dict[MediaType, DeliveryKind] = {
    MediaType.VIDEO: DeliveryKind.VIDEO,
    MediaType.AUDIO: DeliveryKind.AUDIO,
    MediaType.IMAGE: DeliveryKind.PHOTO,
}


class AcquireMedia:
    """Fetch a source at a chosen quality and hand it to a destination."""

    def __init__(
        self,
        *,
        downloader: DownloaderPort,
        delivery: DeliveryRouter,
        workspace: WorkspaceLeasing,
        journal: AcquisitionJournal,
        clock: Clock,
        max_item_bytes: int | None = None,
        allow_merge: bool = False,
        prefer_compatible: bool = False,
    ) -> None:
        """Wire the use case to its ports.

        ``delivery`` is a *router*, not a provider: the use case names a target
        and lets the router decide which destination owns it. That is what
        allows a new destination to be added without this file changing.

        ``allow_merge`` is a deployment fact, not a per-request one: whether
        this device can combine separate streams depends on whether it has a
        merger, so it is configured once here rather than asked of every caller.
        """
        self._downloader = downloader
        self._delivery = delivery
        self._workspace = workspace
        self._journal = journal
        self._clock = clock
        self._max_item_bytes = max_item_bytes
        self._allow_merge = allow_merge
        self._prefer_compatible = prefer_compatible

    async def execute(
        self,
        request: AcquireMediaCommand,
        *,
        on_progress: ProgressCallback | None = None,
        on_delivery_progress: DeliveryProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AcquisitionSummary:
        """Acquire, deliver, record, and release the local copy.

        Progress and cancellation are keyword-only rather than fields on the
        command: they are live channels, not data, and a command that carried
        them could not be logged, replayed or queued.

        Args:
            request: What to acquire, at what quality, and where to send it.
            on_progress: Optional sink for download progress.
            on_delivery_progress: Optional sink for upload progress. Separate
                from ``on_progress`` because they describe different halves of
                the operation, and on a domestic connection the upload is
                usually the slower one.
            cancellation: Optional token; honoured at the next chunk boundary.

        Returns:
            A summary of what was delivered.

        Raises:
            FormatUnavailableError: If the chosen quality is no longer offered.
            ArtifactTooLargeError: If the result exceeds the destination's
                ceiling.
            DownloadError: If acquisition failed.
            DeliveryError: If the destination refused it.
        """
        started = time.monotonic()

        # Re-probe rather than trusting metadata the caller gathered earlier:
        # format identifiers expire, and a stale one downloads the wrong thing.
        metadata = await self._downloader.probe(request.url)
        options = build_quality_options(
            metadata, max_bytes=self._max_item_bytes, allow_merge=self._allow_merge
        )
        selection = selection_for(
            request.quality_key,
            options,
            allow_merge=self._allow_merge,
            prefer_compatible=self._prefer_compatible,
        )
        chosen = next(option for option in options if option.key == request.quality_key)

        capabilities = self._delivery.capabilities_for(request.target)
        ceiling = self._effective_ceiling(capabilities.maximum_file_size)
        bound = logger.bind(
            provider=metadata.provider,
            quality=chosen.label,
            target=request.target.provider,
        )

        with self._workspace.lease(
            label="acquire", reserve_bytes=self._reservation(metadata)
        ) as scope:
            result = await self._downloader.fetch(
                DownloadRequest(
                    url=request.url,
                    selection=selection,
                    max_bytes=ceiling,
                    include_thumbnail=True,
                ),
                scope,
                on_progress=on_progress,
                cancellation=cancellation,
            )

            self._check_deliverable(result.primary, capabilities)
            receipt = await self._delivery.deliver(
                DeliveryRequest(
                    target=request.target,
                    artifact=result.primary,
                    kind=self._kind_for(metadata, chosen),
                    caption=request.caption or metadata.title,
                    filename=result.primary.name,
                    duration_seconds=_seconds(metadata),
                    width=result.selected_format.width,
                    height=result.selected_format.height,
                    thumbnail=_thumbnail_of(result.artifacts),
                ),
                scope,
                on_progress=on_delivery_progress,
            )

            await self._journal.record(
                JournalEntry(
                    principal=request.requested_by,
                    url=result.url,
                    provider=result.provider,
                    title=metadata.title,
                    quality_label=chosen.label,
                    bytes_delivered=receipt.size_bytes,
                    remote_id=receipt.provider_asset_id,
                    remote_unique_id=receipt.reference.remote_unique_id,
                    message_id=receipt.provider_message_id,
                    delivered_at=receipt.delivered_at,
                )
            )

        # The lease is gone: every local byte with it.
        elapsed = time.monotonic() - started
        bound.bind(
            bytes=receipt.size_bytes,
            seconds=round(elapsed, 2),
            custodian=receipt.can_serve_back,
        ).info("Acquired and delivered; local copy released")

        return AcquisitionSummary(
            url=result.url,
            provider=result.provider,
            title=metadata.title,
            quality_label=chosen.label,
            bytes_delivered=receipt.size_bytes,
            elapsed_seconds=elapsed,
            remote_id=receipt.provider_asset_id,
            remote_unique_id=receipt.reference.remote_unique_id,
            message_id=receipt.provider_message_id,
            delivered_at=receipt.delivered_at,
            local_copy_released=True,
        )

    def _effective_ceiling(self, delivery_limit: int) -> int:
        """Return the smaller of this deployment's and the destination's limit.

        Downloading something the destination will certainly refuse wastes an
        hour and a lease, so the delivery ceiling is applied to the *download*.
        """
        if self._max_item_bytes is None:
            return delivery_limit
        return min(self._max_item_bytes, delivery_limit)

    @staticmethod
    def _check_deliverable(artifact: ArtifactRef, capabilities: DeliveryCapabilities) -> None:
        """Refuse before uploading something the destination cannot accept.

        The provider checks this too. Checking here as well means the failure is
        reported before a slow upload starts, and the message names the real
        limit rather than a transport error.
        """
        if not capabilities.accepts(artifact.size_bytes):
            raise ArtifactTooLargeError(
                capabilities.maximum_file_size,
                artifact.size_bytes,
                provider=capabilities.provider,
            )

    @staticmethod
    def _kind_for(metadata: MediaMetadata, chosen: QualityOption) -> DeliveryKind:
        """Decide how the destination should present the result."""
        if chosen.is_audio_only:
            return DeliveryKind.AUDIO
        return _KIND_MAP.get(metadata.kind, DeliveryKind.DOCUMENT)

    @staticmethod
    def _reservation(metadata: MediaMetadata) -> int:
        """Return how much workspace to reserve before starting."""
        expected = metadata.expected_bytes or _DEFAULT_RESERVE_BYTES
        return int(expected * _RESERVE_FACTOR)


def _seconds(metadata: MediaMetadata) -> int | None:
    """Return a whole-second duration, when one is known."""
    if metadata.duration_seconds is None:
        return None
    return int(metadata.duration_seconds)


def _thumbnail_of(artifacts: tuple[ArtifactRef, ...]) -> ArtifactRef | None:
    """Return the downloaded poster image, if the engine produced one."""
    return next(
        (artifact for artifact in artifacts if artifact.role is ArtifactRole.THUMBNAIL),
        None,
    )
