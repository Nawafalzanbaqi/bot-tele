"""A scriptable download engine, for exercising the pipeline without a network.

It is a real :class:`~mediahub.application.download.ports.DownloaderPort`: it
writes through ``workspace.open_artifact``, so artifacts arrive atomically and
carry the digest taken from the stream, exactly as the yt-dlp adapter's do. What
it fakes is the internet, and nothing else.

Everything the pipeline has to survive can be scripted here: a probe that fails,
a transfer that fails once and then works, a transfer that leaves a partial file
behind for the next attempt to continue, progress that arrives in chunks, and a
cancellation observed mid-transfer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mediahub.application.download.errors import DownloadCancelledError
from mediahub.application.download.ports import (
    AudioFormat,
    DownloadCapabilities,
    DownloadProgress,
    DownloadResult,
    DownloadStage,
    MediaMetadata,
    SelectedFormat,
    Thumbnail,
    VideoFormat,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.media.enums import MediaType

if TYPE_CHECKING:
    from collections.abc import Callable

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.ports import (
        DownloadRequest,
        ProgressCallback,
    )
    from mediahub.application.workspace.ports import ArtifactRef, WorkspaceScope

RESUME_MARKER = ".resume.partial"
"""Hidden and suffixed, so the workspace treats it as an in-flight temporary and
never lists it as a finished artifact - which is what the real engine's part
files are."""


@dataclass
class FakeDownloader:
    """An engine that writes deterministic bytes and can be told to misbehave."""

    engine_name: str = "fake-engine"
    provider: str = "fakesite"
    title: str = "A Test Video"
    kind: MediaType = MediaType.VIDEO
    size_bytes: int = 4096
    expected_bytes: int | None = 12_000_000
    duration_ms: int | None = 125_000
    thumbnail_bytes: int = 0
    chunks: tuple[int, ...] = ()
    eta_seconds: float | None = 1.0
    speed_bps: float | None = 1024.0
    offers_video: bool = True
    offers_audio: bool = True
    probe_error: BaseException | None = None
    fetch_error: BaseException | None = None
    fetch_error_times: int | None = None
    leave_partial: bool = False
    on_fetch: Callable[[WorkspaceScope], None] | None = None
    probe_calls: int = 0
    fetch_calls: int = 0
    requests: list[DownloadRequest] = field(default_factory=list)
    resumptions: list[bool] = field(default_factory=list)

    # -- The port ------------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the engine's name."""
        return self.engine_name

    def capabilities(self) -> DownloadCapabilities:
        """Return what this engine can do."""
        return DownloadCapabilities(
            engine=self.engine_name,
            version="1.0",
            supports_resume=True,
            supports_thumbnails=True,
            requires_external_merger=False,
        )

    def supports(self, url: str) -> bool:
        """Return whether this engine claims ``url``."""
        del url
        return True

    async def probe(self, url: str, *, timeout_seconds: float | None = None) -> MediaMetadata:
        """Return metadata for ``url``, or raise the scripted failure."""
        del timeout_seconds
        self.probe_calls += 1
        if self.probe_error is not None:
            raise self.probe_error
        return self.metadata_for(url)

    async def fetch(
        self,
        request: DownloadRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        """Write the scripted artifacts into ``workspace``.

        Raises:
            DownloadCancelledError: If the token is set before or during the
                transfer, which is where a real engine notices it.
        """
        self.fetch_calls += 1
        self.requests.append(request)
        started = datetime.now(UTC)

        resumed = self._take_resume_marker(workspace, resume=request.resume)
        self.resumptions.append(resumed)
        self._stop_if_cancelled(cancellation)

        for transferred in self.chunks:
            self._stop_if_cancelled(cancellation)
            self._report(on_progress, transferred)
        if self.on_fetch is not None:
            self.on_fetch(workspace)
        self._stop_if_cancelled(cancellation)
        self._maybe_fail(workspace)

        artifacts = [self._write(workspace, ArtifactRole.PRIMARY, "mp4", self.size_bytes)]
        if self.thumbnail_bytes and request.include_thumbnail:
            artifacts.append(
                self._write(workspace, ArtifactRole.THUMBNAIL, "jpg", self.thumbnail_bytes)
            )

        self._report(on_progress, self.size_bytes, stage=DownloadStage.COMPLETED)
        return DownloadResult(
            url=request.url,
            provider=self.provider,
            artifacts=tuple(artifacts),
            metadata=self.metadata_for(request.url),
            selected_format=SelectedFormat(
                format_id="137",
                container="mp4",
                video_codec="avc1",
                audio_codec="mp4a",
                width=1920,
                height=1080,
                is_audio_only=request.selection.wants_audio_only,
            ),
            total_bytes=sum(artifact.size_bytes for artifact in artifacts),
            started_at=started,
            finished_at=datetime.now(UTC),
            resumed=resumed,
        )

    # -- Scripting helpers ---------------------------------------------------

    def metadata_for(self, url: str) -> MediaMetadata:
        """Return the metadata this engine reports for ``url``."""
        return MediaMetadata(
            url=url,
            provider=self.provider,
            title=self.title,
            kind=self.kind,
            probed_at=datetime.now(UTC),
            provider_item_id="abc123",
            duration_ms=self.duration_ms,
            uploader="Someone",
            thumbnails=(Thumbnail(url="https://cdn.example.com/big.jpg", width=1920, height=1080),),
            video_formats=self._video_formats(),
            audio_formats=self._audio_formats(),
            expected_bytes=self.expected_bytes,
        )

    def _video_formats(self) -> tuple[VideoFormat, ...]:
        """Return the video renditions this source advertises, tallest first."""
        if not self.offers_video:
            return ()
        return (
            VideoFormat(
                format_id="137",
                container="mp4",
                video_codec="avc1",
                audio_codec="mp4a",
                width=1920,
                height=1080,
                filesize_bytes=self.expected_bytes,
                quality_label="1080p",
            ),
            VideoFormat(
                format_id="18",
                container="mp4",
                width=640,
                height=360,
                filesize_bytes=1_000_000,
                quality_label="360p",
            ),
        )

    def _audio_formats(self) -> tuple[AudioFormat, ...]:
        """Return the audio-only renditions this source advertises."""
        if not self.offers_audio:
            return ()
        return (
            AudioFormat(format_id="140", container="m4a", audio_codec="mp4a", filesize_bytes=1),
        )

    def _maybe_fail(self, workspace: WorkspaceScope) -> None:
        """Raise the scripted failure, leaving a partial file if asked to."""
        if self.fetch_error is None:
            return
        if self.fetch_error_times is not None and self.fetch_calls > self.fetch_error_times:
            return
        if self.leave_partial:
            self._leave_resume_marker(workspace)
        raise self.fetch_error

    def _write(
        self,
        workspace: WorkspaceScope,
        role: ArtifactRole,
        extension: str,
        size: int,
    ) -> ArtifactRef:
        """Write one artifact through the workspace and return its handle."""
        with workspace.open_artifact(extension=extension, role=role) as writer:
            writer.write(bytes(size))
        published = writer.published
        assert published is not None
        return published

    def _report(
        self,
        callback: ProgressCallback | None,
        transferred: int,
        *,
        stage: DownloadStage = DownloadStage.DOWNLOADING,
    ) -> None:
        """Send one progress observation, when anyone is listening."""
        if callback is None:
            return
        callback(
            DownloadProgress(
                stage=stage,
                downloaded_bytes=transferred,
                total_bytes=self.size_bytes,
                speed_bps=self.speed_bps,
                eta_seconds=self.eta_seconds,
            )
        )

    def _leave_resume_marker(self, workspace: WorkspaceScope) -> None:
        """Leave behind what a half-finished transfer would leave behind."""
        (workspace.directory() / RESUME_MARKER).write_bytes(b"partial")

    def _take_resume_marker(self, workspace: WorkspaceScope, *, resume: bool) -> bool:
        """Consume a partial file left by an earlier attempt, if resuming."""
        marker = workspace.directory() / RESUME_MARKER
        if not marker.is_file():
            return False
        if not resume:
            marker.unlink()
            return False
        marker.unlink()
        return True

    @staticmethod
    def _stop_if_cancelled(cancellation: CancellationToken | None) -> None:
        """Stop the transfer the way a real engine does: promptly and typed."""
        if cancellation is not None and cancellation.cancelled:
            message = "the transfer was cancelled"
            raise DownloadCancelledError(message)
