"""The download engine contract.

This module defines *what* MediaHub needs from a download engine. It contains no
transfer logic and names no engine: the word "yt-dlp" does not appear here, and
must not. Every caller - the future worker, the REST API, the CLI, a Telegram
handler - depends on this module and nothing below it.

The contract in one paragraph: given a URL, an engine can say whether it
:meth:`~DownloaderPort.supports` it, can :meth:`~DownloaderPort.probe` it for
metadata without spending bandwidth, and can :meth:`~DownloaderPort.fetch` it
into a workspace lease while reporting progress and honouring cancellation.
Everything it returns is an immutable DTO; everything it raises is a typed
error from :mod:`mediahub.application.download.errors`.

Design notes worth keeping:

* **Probe and fetch are separate.** Admission needs size and duration *before* a
  job exists; discovering that a file is 40 GB after downloading it is the
  failure this split prevents.
* **Progress is a synchronous callback.** Engine work runs in a worker thread,
  so the callback must be cheap and non-blocking. An async consumer should hand
  the update to a queue or ``loop.call_soon_threadsafe``.
* **The engine writes only into the lease it is given.** It never chooses a
  location and never keeps anything.
* **Enums from the domain cross this boundary.** They are closed value sets and
  part of the published language. Aggregates never cross it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from mediahub.application.download.errors import (
    InvalidDownloadResultError,
    InvalidFormatSelectionError,
)
from mediahub.application.workspace.ports import ArtifactRole

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from datetime import datetime

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.workspace.ports import ArtifactRef, WorkspaceScope
    from mediahub.domain.media.enums import MediaType

PERCENT: Final[int] = 100


# --------------------------------------------------------------------------- #
# Stages and selection                                                         #
# --------------------------------------------------------------------------- #


class DownloadStage(StrEnum):
    """Where an engine is in its own pipeline.

    Reported on every progress update so a UI can say something more useful
    than "working". Mirrors the acquisition pipeline
    (``docs/architecture/07-download-pipeline.md``) at the granularity a single
    engine call can observe.
    """

    VALIDATING = "validating"
    PROBING = "probing"
    SELECTING = "selecting"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    VERIFYING = "verifying"
    COMPLETED = "completed"


class FormatPreference(StrEnum):
    """How the caller wants the format chosen.

    Attributes:
        BEST: Best overall, subject to the constraints in the selection.
        AUDIO_ONLY: An audio-only stream. No transcoding is performed - the
            stream arrives in its native container. Converting it (to MP3, say)
            is the Processing subsystem's job, not the engine's.
        VIDEO_ONLY: A video-only stream, for callers that will supply audio.
        SPECIFIC: Exactly the format the caller names.
    """

    BEST = "best"
    AUDIO_ONLY = "audio_only"
    VIDEO_ONLY = "video_only"
    SPECIFIC = "specific"


@dataclass(frozen=True, slots=True)
class FormatSelection:
    """What the caller wants downloaded.

    Expressed as *intent*, never as an engine-specific format expression -
    translating this into whatever an engine understands is the adapter's job.

    Attributes:
        preference: The overall strategy.
        format_id: Required when ``preference`` is ``SPECIFIC``; forbidden
            otherwise, so a stale id cannot silently override a strategy.
        max_height: Cap on vertical resolution, e.g. ``720``.
        max_filesize_bytes: Skip formats known to be larger than this.
        prefer_container: Preferred container, e.g. ``mp4``. A hint, not a
            requirement.
        allow_merge: Permit selecting separate video and audio streams that must
            be merged afterwards. **Defaults to false** because merging needs
            FFmpeg, which the engine does not own.
        prefer_compatible: Prefer codecs an ordinary consumer player can decode,
            accepting a larger file for the same resolution. Matters as soon as
            merging is on: the *best* streams a platform offers are increasingly
            AV1 or VP9 with Opus audio, which are efficient and which most phone
            players and chat clients cannot play - so the result arrives, is the
            right resolution, and does not open. A newer codec is not a better
            download if nothing renders it.
    """

    preference: FormatPreference = FormatPreference.BEST
    format_id: str | None = None
    max_height: int | None = None
    max_filesize_bytes: int | None = None
    prefer_container: str | None = None
    allow_merge: bool = False
    prefer_compatible: bool = False

    def __post_init__(self) -> None:
        """Reject selections that contradict themselves."""
        if self.preference is FormatPreference.SPECIFIC and not self.format_id:
            message = "a specific selection requires a format_id"
            raise InvalidFormatSelectionError(message)
        if self.preference is not FormatPreference.SPECIFIC and self.format_id:
            message = "format_id is only meaningful with a specific selection"
            raise InvalidFormatSelectionError(message)
        if self.max_height is not None and self.max_height <= 0:
            message = "max_height must be positive"
            raise InvalidFormatSelectionError(message)
        if self.max_filesize_bytes is not None and self.max_filesize_bytes <= 0:
            message = "max_filesize_bytes must be positive"
            raise InvalidFormatSelectionError(message)

    @classmethod
    def best(cls, *, allow_merge: bool = False, prefer_compatible: bool = False) -> FormatSelection:
        """Select the best available quality."""
        return cls(
            preference=FormatPreference.BEST,
            allow_merge=allow_merge,
            prefer_compatible=prefer_compatible,
        )

    @classmethod
    def audio_only(cls, *, prefer_compatible: bool = False) -> FormatSelection:
        """Select the best audio-only stream, in its native container."""
        return cls(preference=FormatPreference.AUDIO_ONLY, prefer_compatible=prefer_compatible)

    @classmethod
    def up_to_height(
        cls, height: int, *, allow_merge: bool = False, prefer_compatible: bool = False
    ) -> FormatSelection:
        """Select the best quality no taller than ``height`` pixels."""
        return cls(
            preference=FormatPreference.BEST,
            max_height=height,
            allow_merge=allow_merge,
            prefer_compatible=prefer_compatible,
        )

    @classmethod
    def specific(cls, format_id: str) -> FormatSelection:
        """Select exactly one format, by the id reported during probing."""
        return cls(preference=FormatPreference.SPECIFIC, format_id=format_id)

    @property
    def wants_audio_only(self) -> bool:
        """Return whether the caller asked for audio without video."""
        return self.preference is FormatPreference.AUDIO_ONLY


# --------------------------------------------------------------------------- #
# Metadata                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VideoFormat:
    """One downloadable video rendition.

    Attributes:
        format_id: Engine-specific identifier, usable in
            :meth:`FormatSelection.specific`.
        container: File container, e.g. ``mp4``.
        video_codec: Codec name, or ``None`` if unknown.
        audio_codec: Codec of the muxed audio track, or ``None`` when the
            rendition carries no audio.
        width: Frame width in pixels.
        height: Frame height in pixels.
        fps: Frames per second.
        bitrate_kbps: Total bitrate, when reported.
        filesize_bytes: Size, exact or estimated.
        filesize_is_estimate: Whether ``filesize_bytes`` was estimated. Estimates
            are hints from an untrusted party and must never be the only size
            check.
        quality_label: Human label such as ``1080p``.
    """

    format_id: str
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: float | None = None
    filesize_bytes: int | None = None
    filesize_is_estimate: bool = False
    quality_label: str | None = None

    @property
    def has_audio(self) -> bool:
        """Return whether this rendition already carries an audio track."""
        return self.audio_codec is not None

    @property
    def resolution(self) -> str | None:
        """Return ``WIDTHxHEIGHT`` when both dimensions are known."""
        if self.width is None or self.height is None:
            return None
        return f"{self.width}x{self.height}"


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """One downloadable audio rendition.

    Attributes:
        format_id: Engine-specific identifier.
        container: File container, e.g. ``m4a``.
        audio_codec: Codec name.
        bitrate_kbps: Bitrate, when reported.
        sample_rate_hz: Sample rate, when reported.
        channels: Channel count, when reported.
        filesize_bytes: Size, exact or estimated.
        filesize_is_estimate: Whether ``filesize_bytes`` was estimated.
        language: Track language, when the source declares one.
    """

    format_id: str
    container: str | None = None
    audio_codec: str | None = None
    bitrate_kbps: float | None = None
    sample_rate_hz: int | None = None
    channels: int | None = None
    filesize_bytes: int | None = None
    filesize_is_estimate: bool = False
    language: str | None = None


@dataclass(frozen=True, slots=True)
class Thumbnail:
    """A poster image advertised by the source.

    Attributes:
        url: Where the image can be fetched. Subject to the same URL policy as
            any other fetch.
        width: Width in pixels, when known.
        height: Height in pixels, when known.
        thumbnail_id: Engine-specific identifier, when provided.
    """

    url: str
    width: int | None = None
    height: int | None = None
    thumbnail_id: str | None = None

    @property
    def pixels(self) -> int:
        """Return the pixel count, or ``0`` when dimensions are unknown."""
        if self.width is None or self.height is None:
            return 0
        return self.width * self.height


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """Everything learned about a source without downloading its payload.

    This is the answer to "what is at this URL, and do we want it?". It is the
    input to admission: size, duration, liveness and deliverability are all
    decided from here, before a job exists.

    Attributes:
        url: The canonical URL that was probed.
        provider: Extractor or platform identity, as reported by the engine.
        provider_item_id: The source's own identifier for the item.
        title: Title as advertised, normalised only for whitespace.
        kind: Broad media category.
        duration_ms: Duration, when the source declares one.
        is_live: Whether this is a live stream. Live sources have no end and are
            refused unless explicitly allowed.
        is_playlist: Whether the URL denotes a collection rather than one item.
        entry_count: Number of entries when ``is_playlist`` is true - or, when
            ``from_playlist`` is true, the size of the collection this item was
            taken from.
        from_playlist: Whether this item was reached through a collection URL,
            of which it is the first entry. Set so the caller can say "this was
            a playlist; here is its first video" instead of silently treating
            the two as the same request.
        uploader: Channel or account name.
        upload_date: Publication date, when known (UTC).
        description: Description, truncated by the adapter.
        age_limit: Age restriction declared by the source.
        thumbnails: Poster images, largest first.
        video_formats: Available video renditions.
        audio_formats: Available audio-only renditions.
        expected_bytes: Best available size estimate for the default selection.
        probed_at: When the probe was performed (UTC).
    """

    url: str
    provider: str
    title: str
    kind: MediaType
    probed_at: datetime
    provider_item_id: str | None = None
    duration_ms: int | None = None
    is_live: bool = False
    is_playlist: bool = False
    entry_count: int | None = None
    from_playlist: bool = False
    uploader: str | None = None
    upload_date: datetime | None = None
    description: str | None = None
    age_limit: int | None = None
    thumbnails: tuple[Thumbnail, ...] = ()
    video_formats: tuple[VideoFormat, ...] = ()
    audio_formats: tuple[AudioFormat, ...] = ()
    expected_bytes: int | None = None

    @property
    def has_video(self) -> bool:
        """Return whether any video rendition is available."""
        return bool(self.video_formats)

    @property
    def has_audio(self) -> bool:
        """Return whether any audio-only rendition is available."""
        return bool(self.audio_formats)

    @property
    def duration_seconds(self) -> float | None:
        """Return the duration in seconds, when known."""
        if self.duration_ms is None:
            return None
        return self.duration_ms / 1000

    def available_qualities(self) -> tuple[str, ...]:
        """Return the distinct video quality labels, tallest first.

        Intended for "what can I ask for?" in a UI, so it deliberately reports
        labels rather than format ids: several ids usually share one quality.
        """
        seen: dict[int, str] = {}
        for video in self.video_formats:
            if video.height is None:
                continue
            seen.setdefault(video.height, video.quality_label or f"{video.height}p")
        return tuple(label for _, label in sorted(seen.items(), reverse=True))

    def best_thumbnail(self) -> Thumbnail | None:
        """Return the largest advertised thumbnail, if any."""
        if not self.thumbnails:
            return None
        return max(self.thumbnails, key=lambda thumbnail: thumbnail.pixels)


# --------------------------------------------------------------------------- #
# Progress                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """One observation of an in-flight download.

    Emitted through a caller-supplied callback. Updates are **throttled** by the
    adapter: a chunk-rate callback would flood any consumer and, on a device
    that persists progress, would destroy an SD card
    (``docs/architecture/05-component-communication.md`` §5.9).

    Attributes:
        stage: What the engine is doing.
        downloaded_bytes: Bytes written so far for the current artifact.
        total_bytes: Expected total, when known. Often an estimate, sometimes
            absent entirely.
        total_is_estimate: Whether ``total_bytes`` was estimated.
        speed_bps: Instantaneous speed in bytes per second, when known.
        eta_seconds: Engine's estimate of time remaining, when known.
        filename: Name of the file currently being written, when known.
        fragment_index: Index of the current fragment, for fragmented formats.
        fragment_count: Total fragments, for fragmented formats.
    """

    stage: DownloadStage
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    total_is_estimate: bool = False
    speed_bps: float | None = None
    eta_seconds: float | None = None
    filename: str | None = None
    fragment_index: int | None = None
    fragment_count: int | None = None

    @property
    def percentage(self) -> float | None:
        """Return completion in percent, or ``None`` if the total is unknown."""
        if not self.total_bytes:
            return None
        ratio = min(self.downloaded_bytes / self.total_bytes, 1.0)
        return round(ratio * PERCENT, 2)


type ProgressCallback = Callable[[DownloadProgress], None]
"""Invoked as a download advances.

Must be cheap, non-blocking and must not raise: it is called from the engine's
worker thread, and an exception there would abort the download.
"""


# --------------------------------------------------------------------------- #
# Request and result                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Everything an engine needs to perform one download.

    Attributes:
        url: The source to fetch. Validated by the engine before any socket is
            opened, whether or not the caller validated it first.
        selection: Which rendition to take.
        max_bytes: Hard ceiling, enforced **while streaming**. A declared size is
            a hint from an untrusted party; this is the guarantee.
        timeout_seconds: Wall-clock budget for the whole download.
        socket_timeout_seconds: Per-connection read timeout, which is what turns
            a stalled source into a failure instead of a hung worker.
        include_thumbnail: Also download the poster image as a separate artifact.
        allow_live: Permit live sources. Off by default: a live stream has no
            end, and a byte ceiling is a poor way to discover that.
        allow_playlist: Permit a URL that denotes a collection. Off by default,
            so a playlist link cannot silently enqueue five hundred files.
        resume: Continue a partial download left by a previous attempt in the
            same lease.
    """

    url: str
    selection: FormatSelection = field(default_factory=FormatSelection)
    max_bytes: int | None = None
    timeout_seconds: float | None = None
    socket_timeout_seconds: float | None = None
    include_thumbnail: bool = False
    allow_live: bool = False
    allow_playlist: bool = False
    resume: bool = True

    def __post_init__(self) -> None:
        """Reject impossible budgets."""
        if self.max_bytes is not None and self.max_bytes <= 0:
            message = "max_bytes must be positive"
            raise InvalidFormatSelectionError(message)
        for label, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("socket_timeout_seconds", self.socket_timeout_seconds),
        ):
            if value is not None and value <= 0:
                message = f"{label} must be positive"
                raise InvalidFormatSelectionError(message)


@dataclass(frozen=True, slots=True)
class SelectedFormat:
    """The rendition an engine actually downloaded.

    Recorded separately from the request because "best" is resolved at download
    time and the answer is worth keeping: it explains the file that arrived.

    Attributes:
        format_id: Identifier of the chosen rendition.
        container: Container of the produced file.
        video_codec: Video codec, or ``None`` for audio-only downloads.
        audio_codec: Audio codec, when present.
        width: Frame width, when applicable.
        height: Frame height, when applicable.
        bitrate_kbps: Bitrate, when reported.
        is_audio_only: Whether the result carries no video.
    """

    format_id: str
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    bitrate_kbps: float | None = None
    is_audio_only: bool = False


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """What an engine reports after a successful download.

    Attributes:
        url: The canonical URL that was fetched.
        provider: Extractor or platform identity.
        artifacts: Every file produced, inside the caller's lease.
        metadata: Metadata as known at download time.
        selected_format: The rendition that was actually taken.
        total_bytes: Total size of every artifact.
        started_at: When the download began (UTC).
        finished_at: When it completed (UTC).
        resumed: Whether a partial file from a previous attempt was continued.
        via_proxy: Whether the bytes came through the configured egress proxy
            rather than the direct path. Reported to the user, because which
            path worked is the one fact that explains a slow or a failed fetch.
    """

    url: str
    provider: str
    artifacts: tuple[ArtifactRef, ...]
    metadata: MediaMetadata
    selected_format: SelectedFormat
    total_bytes: int
    started_at: datetime
    finished_at: datetime
    resumed: bool = False
    via_proxy: bool = False

    def __post_init__(self) -> None:
        """Enforce that exactly one artifact is the media itself."""
        primaries = [
            artifact for artifact in self.artifacts if artifact.role is ArtifactRole.PRIMARY
        ]
        if len(primaries) != 1:
            message = (
                f"a download result must contain exactly one primary artifact, got {len(primaries)}"
            )
            raise InvalidDownloadResultError(message)

    @property
    def primary(self) -> ArtifactRef:
        """Return the media artifact itself."""
        return next(
            artifact for artifact in self.artifacts if artifact.role is ArtifactRole.PRIMARY
        )

    @property
    def duration_seconds(self) -> float:
        """Return how long the download took."""
        return (self.finished_at - self.started_at).total_seconds()


# --------------------------------------------------------------------------- #
# Capabilities and the port                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DownloadCapabilities:
    """What an engine can do, given how it is configured.

    Reported rather than assumed, so that a caller (and the future processing
    planner) can adapt instead of discovering a limitation by failing.

    Attributes:
        engine: Engine name, for logs and diagnostics.
        version: Engine version.
        supports_probe: Metadata can be read without downloading.
        supports_format_selection: Renditions can be chosen.
        supports_audio_only: Audio-only streams can be requested.
        supports_resume: Partial downloads can be continued.
        supports_playlists: Collections can be enumerated.
        supports_live: Live sources can be captured.
        supports_thumbnails: Poster images can be downloaded.
        requires_external_merger: Selecting separate video and audio streams
            needs an external tool (FFmpeg) that this engine does not own.
        max_concurrent_fragments: How many fragments may be fetched in parallel.
    """

    engine: str
    version: str
    supports_probe: bool = True
    supports_format_selection: bool = True
    supports_audio_only: bool = True
    supports_resume: bool = True
    supports_playlists: bool = False
    supports_live: bool = False
    supports_thumbnails: bool = True
    requires_external_merger: bool = True
    max_concurrent_fragments: int = 1


class DownloaderPort(Protocol):
    """Performs metadata probing and byte transfer for one class of sources.

    Implementations must:

    * write only inside the workspace lease they are given;
    * enforce ``max_bytes`` while streaming, not afterwards;
    * check the cancellation token often enough to stop within about a second;
    * raise a typed
      :class:`~mediahub.application.download.errors.DownloadError`, never a raw
      library exception;
    * classify every failure as transient, permanent or policy, because the
      caller's retry decision depends on it.
    """

    @property
    def name(self) -> str:
        """Return the engine's name, for logs and selection."""
        ...

    def capabilities(self) -> DownloadCapabilities:
        """Return what this engine can currently do."""
        ...

    def supports(self, url: str) -> bool:
        """Return whether this engine can handle ``url``.

        Must be pure, fast and free of I/O: it is called in selection loops.
        """
        ...

    async def probe(self, url: str, *, timeout_seconds: float | None = None) -> MediaMetadata:
        """Read metadata without downloading the payload.

        Raises:
            InvalidUrlError: If the URL is refused by policy.
            UnsupportedProviderError: If no extractor claims the URL.
            MetadataUnavailableError: If the source cannot be described.
            DownloadTimeoutError: If the probe exceeds its budget.
            ProviderError: If the provider failed transiently.
        """
        ...

    async def fetch(
        self,
        request: DownloadRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        """Download the requested rendition into ``workspace``.

        Raises:
            InvalidUrlError: If the URL is refused by policy.
            UnsupportedProviderError: If no extractor claims the URL.
            FormatUnavailableError: If the requested rendition does not exist.
            SizeLimitExceededError: If the transfer exceeds ``max_bytes``.
            DownloadCancelledError: If cancellation was requested.
            DownloadTimeoutError: If the transfer exceeds its budget.
            DownloadFailedError: If the transfer failed for any other reason.
        """
        ...
