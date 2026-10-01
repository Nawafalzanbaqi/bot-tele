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

from mediahub.application.delivery.errors import ArtifactTooLargeError, DeliveryError
from mediahub.application.delivery.ports import DeliveryKind, DeliveryRequest
from mediahub.application.download.dto import AcquisitionSummary, QualityOption, StageTimings
from mediahub.application.download.errors import FormatUnavailableError
from mediahub.application.download.journal import JournalEntry
from mediahub.application.download.ports import DownloadRequest
from mediahub.application.download.quality import (
    AUTO_KEY,
    MAX_KEY,
    ORIGINAL_KEY,
    build_quality_options,
    is_compatible_codec,
    resolve_auto,
    selection_for,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.media.enums import MediaType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.common.ports import Clock
    from mediahub.application.delivery.ports import (
        DeliveryCapabilities,
        DeliveryProgressCallback,
        DeliveryReceipt,
        DeliveryRouter,
    )
    from mediahub.application.download.dto import AcquireMediaCommand
    from mediahub.application.download.journal import AcquisitionJournal
    from mediahub.application.download.ports import (
        DownloaderPort,
        DownloadResult,
        MediaMetadata,
        ProgressCallback,
        SelectedFormat,
    )
    from mediahub.application.workspace.ports import (
        ArtifactRef,
        WorkspaceLeasing,
        WorkspaceScope,
    )

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

_ALBUM_KINDS: frozenset[DeliveryKind] = frozenset({DeliveryKind.PHOTO, DeliveryKind.VIDEO})
"""What a destination groups into one post. Documents and audio travel alone."""

_IMAGE_SUFFIXES: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
_AUDIO_SUFFIXES: tuple[str, ...] = (".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav", ".flac")
_VIDEO_SUFFIXES: tuple[str, ...] = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


def _kind_of_file(name: str) -> DeliveryKind:
    """Decide how a companion file is presented, from its name.

    A companion has no probe of its own - it is the second picture of a
    carousel or the track under a slideshow - so the suffix is what there is.
    """
    lowered = name.lower()
    if lowered.endswith(_IMAGE_SUFFIXES):
        return DeliveryKind.PHOTO
    if lowered.endswith(_AUDIO_SUFFIXES):
        return DeliveryKind.AUDIO
    if lowered.endswith(_VIDEO_SUFFIXES):
        return DeliveryKind.VIDEO
    return DeliveryKind.DOCUMENT


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
        probed_at = time.monotonic()
        options = build_quality_options(
            metadata,
            max_bytes=self._max_item_bytes,
            allow_merge=self._allow_merge,
            prefer_compatible=self._prefer_compatible,
        )
        # The ceiling is resolved *before* the quality is, because "the best one"
        # is not a property of the source - it is the best one this destination
        # will accept, and only the destination knows that number.
        capabilities = self._delivery.capabilities_for(request.target)
        ceiling = self._effective_ceiling(capabilities.maximum_file_size)

        chosen = self._choose(request.quality_key, options, ceiling=ceiling)
        capped_from = self._capped_from(request.quality_key, options, chosen)
        selection = selection_for(
            chosen.key,
            options,
            allow_merge=self._allow_merge,
            prefer_compatible=self._prefer_compatible,
        )

        bound = logger.bind(
            provider=metadata.provider,
            quality=chosen.label,
            target=request.target.provider,
        )

        with self._workspace.lease(
            label="acquire", reserve_bytes=self._reservation(metadata)
        ) as scope:
            download_started = time.monotonic()
            result = await self._downloader.fetch(
                DownloadRequest(
                    url=request.url,
                    selection=selection,
                    max_bytes=ceiling,
                    # An album is fetched whole; its poster would be one more
                    # file to tell apart from the pictures it is a poster of.
                    include_thumbnail=not metadata.is_album,
                    allow_playlist=metadata.is_album,
                ),
                scope,
                on_progress=on_progress,
                cancellation=cancellation,
            )
            downloaded_at = time.monotonic()

            self._check_deliverable(result.primary, capabilities)
            kind = self._kind_for(metadata, chosen, result.selected_format)
            primary = DeliveryRequest(
                target=request.target,
                artifact=result.primary,
                kind=kind,
                caption=request.caption or metadata.title,
                filename=result.primary.name,
                duration_seconds=_seconds(metadata),
                width=result.selected_format.width,
                height=result.selected_format.height,
                thumbnail=_thumbnail_of(result.artifacts),
            )
            # A carousel is one request and several files. Delivering only the
            # first is the difference between "it works" and "it lost half my
            # post", and the caller cannot tell which happened.
            receipt, extra = await self._deliver_all(
                primary,
                result,
                request=request,
                capabilities=capabilities,
                scope=scope,
                on_delivery_progress=on_delivery_progress,
            )
            delivered_at = time.monotonic()

            delivered_label = _delivered_label(chosen, result.selected_format)
            await self._journal.record(
                JournalEntry(
                    principal=request.requested_by,
                    url=result.url,
                    provider=result.provider,
                    title=metadata.title,
                    quality_label=delivered_label,
                    bytes_delivered=receipt.size_bytes,
                    remote_id=receipt.provider_asset_id,
                    remote_unique_id=receipt.reference.remote_unique_id,
                    message_id=receipt.provider_message_id,
                    delivered_at=receipt.delivered_at,
                )
            )

        # The lease is gone: every local byte with it.
        elapsed = time.monotonic() - started
        stages = StageTimings(
            probe_seconds=probed_at - started,
            download_seconds=downloaded_at - download_started,
            deliver_seconds=delivered_at - downloaded_at,
        )
        sent_as_document = (
            kind is DeliveryKind.DOCUMENT and _KIND_MAP.get(metadata.kind) is DeliveryKind.VIDEO
        )
        bound.bind(
            bytes=receipt.size_bytes,
            seconds=round(elapsed, 2),
            probe_s=round(stages.probe_seconds, 2),
            download_s=round(stages.download_seconds, 2),
            deliver_s=round(stages.deliver_seconds, 2),
            custodian=receipt.can_serve_back,
            egress=result.egress,
            codec=result.selected_format.video_codec,
            as_document=sent_as_document,
            capped_from=capped_from,
        ).info("Acquired and delivered; local copy released")

        return AcquisitionSummary(
            url=result.url,
            provider=result.provider,
            title=metadata.title,
            quality_label=delivered_label,
            bytes_delivered=receipt.size_bytes,
            elapsed_seconds=elapsed,
            remote_id=receipt.provider_asset_id,
            remote_unique_id=receipt.reference.remote_unique_id,
            message_id=receipt.provider_message_id,
            delivered_at=receipt.delivered_at,
            local_copy_released=True,
            items_delivered=1 + extra,
            via_proxy=result.via_proxy,
            egress=result.egress,
            capped_from=capped_from,
            sent_as_document=sent_as_document,
            stages=stages,
        )

    @staticmethod
    def _choose(
        key: str, options: Sequence[QualityOption], *, ceiling: int | None
    ) -> QualityOption:
        """Return the option a request names, resolving ``auto`` against the ceiling.

        Raises:
            FormatUnavailableError: If a named key was never offered - the
                correct answer for a stale button tapped an hour later.
        """
        if key == AUTO_KEY:
            return resolve_auto(options, ceiling=ceiling)
        if key == MAX_KEY:
            # Not an offered option: the most the source has, named after the
            # frame once it is known (see _delivered_label).
            return QualityOption(
                key=MAX_KEY,
                label="Max",
                format_id=None,
                height=None,
                approx_bytes=None,
                is_audio_only=False,
            )
        chosen = next((option for option in options if option.key == key), None)
        if chosen is None:
            message = f"'{key}' is no longer an available quality for this source"
            raise FormatUnavailableError(message)
        return chosen

    def _effective_ceiling(self, delivery_limit: int) -> int:
        """Return the smaller of this deployment's and the destination's limit.

        Downloading something the destination will certainly refuse wastes an
        hour and a lease, so the delivery ceiling is applied to the *download*.
        """
        if self._max_item_bytes is None:
            return delivery_limit
        return min(self._max_item_bytes, delivery_limit)

    async def _deliver_all(
        self,
        primary: DeliveryRequest,
        result: DownloadResult,
        *,
        request: AcquireMediaCommand,
        capabilities: DeliveryCapabilities,
        scope: WorkspaceScope,
        on_delivery_progress: DeliveryProgressCallback | None,
    ) -> tuple[DeliveryReceipt, int]:
        """Deliver the primary and every companion; return the receipt and how many more went.

        Where the destination can group and the items are photos or videos,
        the post goes as one album - a carousel arrives as a carousel. Audio
        (a slideshow's track) follows on its own. Otherwise, or when the
        primary has to go as a document, items go one by one as before.
        """
        companions = [
            artifact for artifact in result.artifacts if artifact.role is ArtifactRole.COMPANION
        ]
        if not companions or not capabilities.supports_albums or primary.kind not in _ALBUM_KINDS:
            receipt = await self._delivery.deliver(primary, scope, on_progress=on_delivery_progress)
            extra = await self._deliver_companions(
                result, request=request, capabilities=capabilities, scope=scope
            )
            return receipt, extra

        album: list[DeliveryRequest] = [primary]
        afterwards: list[DeliveryRequest] = []
        for artifact in companions:
            if not capabilities.accepts(artifact.size_bytes):
                logger.bind(name=artifact.name, bytes=artifact.size_bytes).warning(
                    "Skipped an item of this post: the destination will not accept it"
                )
                continue
            item = DeliveryRequest(
                target=request.target,
                artifact=artifact,
                kind=_kind_of_file(artifact.name),
                caption=None,
                filename=artifact.name,
            )
            (album if item.kind in _ALBUM_KINDS else afterwards).append(item)
        receipt = await self._delivery.deliver_album(album, scope, on_progress=on_delivery_progress)
        sent = len(album) - 1
        for item in afterwards:
            try:
                await self._delivery.deliver(item, scope)
            except DeliveryError as error:
                logger.bind(name=item.artifact.name, code=error.code).warning(
                    "Skipped an item of this post: {}", error.message
                )
                continue
            sent += 1
        return receipt, sent

    async def _deliver_companions(
        self,
        result: DownloadResult,
        *,
        request: AcquireMediaCommand,
        capabilities: DeliveryCapabilities,
        scope: WorkspaceScope,
    ) -> int:
        """Send the other items of the same post, and report how many went.

        Sequential rather than concurrent: a chat service rate-limits uploads,
        and racing them turns a carousel into a flood wait. One that the
        destination refuses is skipped rather than failing the whole request -
        the first picture has already arrived, and losing it to be strict about
        the fourth serves nobody.
        """
        companions = [
            artifact for artifact in result.artifacts if artifact.role is ArtifactRole.COMPANION
        ]
        sent = 0
        for artifact in companions:
            if not capabilities.accepts(artifact.size_bytes):
                logger.bind(name=artifact.name, bytes=artifact.size_bytes).warning(
                    "Skipped an item of this post: the destination will not accept it"
                )
                continue
            try:
                await self._delivery.deliver(
                    DeliveryRequest(
                        target=request.target,
                        artifact=artifact,
                        kind=_kind_of_file(artifact.name),
                        caption=None,
                        filename=artifact.name,
                    ),
                    scope,
                )
            except DeliveryError as error:
                logger.bind(name=artifact.name, code=error.code).warning(
                    "Skipped an item of this post: {}", error.message
                )
                continue
            sent += 1
        return sent

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
    def _capped_from(
        key: str, options: Sequence[QualityOption], chosen: QualityOption
    ) -> str | None:
        """Return the label of the best rung when the ceiling forced a lower one.

        Only ``auto`` can be capped: a person who tapped 480p got 480p. And only
        a *rung* counts as the thing skipped - the unbounded "best" entry has no
        size of its own, so there is nothing to say it would not have fitted.
        """
        if key != AUTO_KEY:
            return None
        rungs = [option for option in options if option.height is not None]
        if not rungs:
            return None
        best = rungs[0]
        if chosen.is_audio_only:
            return best.label
        if chosen.height is not None and best.height is not None and chosen.height < best.height:
            return best.label
        return None

    @staticmethod
    def _kind_for(
        metadata: MediaMetadata, chosen: QualityOption, taken: SelectedFormat
    ) -> DeliveryKind:
        """Decide how the destination should present the result.

        A video whose codec the destination cannot play inline goes as a
        document. Sent as a video it would arrive, show a poster, and refuse to
        play - with nothing to say why. As a document the player the person
        already has opens it. An *unknown* codec is not a known-bad one and is
        still sent as video.
        """
        if chosen.is_audio_only:
            return DeliveryKind.AUDIO
        kind = _KIND_MAP.get(metadata.kind, DeliveryKind.DOCUMENT)
        if (
            kind is DeliveryKind.VIDEO
            and taken.video_codec is not None
            and not is_compatible_codec(taken.video_codec)
        ):
            return DeliveryKind.DOCUMENT
        return kind

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


def _delivered_label(chosen: QualityOption, taken: SelectedFormat) -> str:
    """Name the rendition that was taken, not only the rung that was asked for.

    A rung is a ceiling for the engine - "up to 2160p, H.264 first" - and on a
    source whose 2160p is VP9-only the engine correctly takes 1080p H.264. The
    card then said "2160p" for a 1920x1080 file (every YouTube 4K link on
    2026-10-01). When the frame is known and sits *below* the rung, the frame
    is the honest label; "Best available" becomes the frame too. A frame at or
    above the rung keeps the rung's name, and audio and "Original" are left
    alone - they are not frames.
    """
    if chosen.is_audio_only or chosen.key == ORIGINAL_KEY or taken.is_audio_only:
        return chosen.label
    short: int | None = taken.height
    if taken.width is not None and taken.height is not None:
        short = min(taken.width, taken.height)
    if short is None or short <= 0:
        return chosen.label
    rung = int(chosen.key[1:]) if chosen.key[:1] == "h" and chosen.key[1:].isdigit() else None
    if rung is None or short < rung:
        return f"{short}p"
    return chosen.label

