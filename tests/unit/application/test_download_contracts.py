"""The engine's DTOs validate themselves and compute what callers rely on."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mediahub.application.common.cancellation import (
    CancellationReason,
    CancellationSource,
    NullCancellation,
)
from mediahub.application.download.errors import (
    DownloadCancelledError,
    DownloadError,
    InvalidDownloadResultError,
    InvalidFormatSelectionError,
    MetadataUnavailableError,
    ProviderError,
    SizeLimitExceededError,
)
from mediahub.application.download.ports import (
    AudioFormat,
    DownloadProgress,
    DownloadRequest,
    DownloadResult,
    DownloadStage,
    FormatPreference,
    FormatSelection,
    MediaMetadata,
    SelectedFormat,
    Thumbnail,
    VideoFormat,
)
from mediahub.application.workspace.ports import ArtifactRef, ArtifactRole
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.media.enums import MediaType

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class TestFormatSelection:
    def test_defaults_to_best(self) -> None:
        selection = FormatSelection()

        assert selection.preference is FormatPreference.BEST
        assert selection.allow_merge is False

    def test_specific_requires_a_format_id(self) -> None:
        with pytest.raises(InvalidFormatSelectionError):
            FormatSelection(preference=FormatPreference.SPECIFIC)

    def test_format_id_without_specific_is_rejected(self) -> None:
        with pytest.raises(InvalidFormatSelectionError):
            FormatSelection(preference=FormatPreference.BEST, format_id="137")

    def test_a_non_positive_height_is_rejected(self) -> None:
        with pytest.raises(InvalidFormatSelectionError):
            FormatSelection(max_height=0)

    def test_a_non_positive_filesize_cap_is_rejected(self) -> None:
        with pytest.raises(InvalidFormatSelectionError):
            FormatSelection(max_filesize_bytes=-1)

    def test_constructors_build_coherent_selections(self) -> None:
        assert FormatSelection.audio_only().wants_audio_only
        assert FormatSelection.up_to_height(720).max_height == 720
        assert FormatSelection.specific("137").format_id == "137"
        assert FormatSelection.best(allow_merge=True).allow_merge


class TestDownloadRequest:
    def test_defaults_are_conservative(self) -> None:
        request = DownloadRequest(url="https://example.com/a")

        assert request.allow_live is False
        assert request.allow_playlist is False
        assert request.resume is True
        assert request.include_thumbnail is False

    def test_a_non_positive_ceiling_is_rejected(self) -> None:
        with pytest.raises(InvalidFormatSelectionError):
            DownloadRequest(url="https://example.com/a", max_bytes=0)

    def test_a_non_positive_timeout_is_rejected(self) -> None:
        with pytest.raises(InvalidFormatSelectionError):
            DownloadRequest(url="https://example.com/a", timeout_seconds=0)

    def test_a_non_positive_socket_timeout_is_rejected(self) -> None:
        with pytest.raises(InvalidFormatSelectionError):
            DownloadRequest(url="https://example.com/a", socket_timeout_seconds=-5)


class TestDownloadProgress:
    def test_percentage_is_none_without_a_total(self) -> None:
        progress = DownloadProgress(stage=DownloadStage.DOWNLOADING, downloaded_bytes=5)

        assert progress.percentage is None

    def test_percentage_is_rounded(self) -> None:
        progress = DownloadProgress(
            stage=DownloadStage.DOWNLOADING, downloaded_bytes=333, total_bytes=1000
        )

        assert progress.percentage == 33.3

    def test_percentage_is_clamped_at_one_hundred(self) -> None:
        progress = DownloadProgress(
            stage=DownloadStage.DOWNLOADING, downloaded_bytes=1500, total_bytes=1000
        )

        assert progress.percentage == 100.0


class TestMediaMetadata:
    def _metadata(self, **overrides: object) -> MediaMetadata:
        defaults: dict[str, object] = {
            "url": "https://example.com/a",
            "provider": "testsite",
            "title": "A",
            "kind": MediaType.VIDEO,
            "probed_at": NOW,
        }
        defaults.update(overrides)
        return MediaMetadata(**defaults)  # type: ignore[arg-type]

    def test_available_qualities_are_deduplicated_and_ordered(self) -> None:
        metadata = self._metadata(
            video_formats=(
                VideoFormat(format_id="a", height=720, quality_label="720p"),
                VideoFormat(format_id="b", height=1080, quality_label="1080p"),
                VideoFormat(format_id="c", height=720, quality_label="720p60"),
                VideoFormat(format_id="d"),
            )
        )

        assert metadata.available_qualities() == ("1080p", "720p")

    def test_best_thumbnail_is_the_largest(self) -> None:
        metadata = self._metadata(
            thumbnails=(
                Thumbnail(url="small", width=10, height=10),
                Thumbnail(url="big", width=100, height=100),
            )
        )

        best = metadata.best_thumbnail()
        assert best is not None
        assert best.url == "big"

    def test_best_thumbnail_is_none_when_absent(self) -> None:
        assert self._metadata().best_thumbnail() is None

    def test_duration_seconds_converts_from_milliseconds(self) -> None:
        assert self._metadata(duration_ms=125_000).duration_seconds == 125.0

    def test_capability_flags_follow_the_format_lists(self) -> None:
        metadata = self._metadata(
            video_formats=(VideoFormat(format_id="v"),),
            audio_formats=(AudioFormat(format_id="a"),),
        )

        assert metadata.has_video
        assert metadata.has_audio


class TestVideoFormat:
    def test_resolution_needs_both_dimensions(self) -> None:
        assert VideoFormat(format_id="a", width=1920, height=1080).resolution == "1920x1080"
        assert VideoFormat(format_id="a", width=1920).resolution is None

    def test_has_audio_reflects_the_codec(self) -> None:
        assert VideoFormat(format_id="a", audio_codec="aac").has_audio
        assert not VideoFormat(format_id="a").has_audio


class TestDownloadResult:
    def _artifact(self, name: str, role: ArtifactRole) -> ArtifactRef:
        return ArtifactRef(lease_id="lease", name=name, size_bytes=10, role=role)

    def _result(self, artifacts: tuple[ArtifactRef, ...]) -> DownloadResult:
        return DownloadResult(
            url="https://example.com/a",
            provider="testsite",
            artifacts=artifacts,
            metadata=MediaMetadata(
                url="https://example.com/a",
                provider="testsite",
                title="A",
                kind=MediaType.VIDEO,
                probed_at=NOW,
            ),
            selected_format=SelectedFormat(format_id="18"),
            total_bytes=10,
            started_at=NOW,
            finished_at=NOW.replace(minute=1),
        )

    def test_exposes_the_primary_artifact(self) -> None:
        result = self._result(
            (
                self._artifact("thumb.jpg", ArtifactRole.THUMBNAIL),
                self._artifact("video.mp4", ArtifactRole.PRIMARY),
            )
        )

        assert result.primary.name == "video.mp4"
        assert result.duration_seconds == 60.0

    @pytest.mark.parametrize("count", [0, 2])
    def test_requires_exactly_one_primary(self, count: int) -> None:
        artifacts = tuple(
            self._artifact(f"file{index}.mp4", ArtifactRole.PRIMARY) for index in range(count)
        )

        with pytest.raises(InvalidDownloadResultError):
            self._result(artifacts or (self._artifact("t.jpg", ArtifactRole.THUMBNAIL),))


class TestErrorTaxonomy:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (ProviderError("x"), FailureKind.TRANSIENT),
            (MetadataUnavailableError("x"), FailureKind.PERMANENT),
            (DownloadCancelledError("x"), FailureKind.CANCELLED),
            (SizeLimitExceededError(10, 20), FailureKind.POLICY),
        ],
    )
    def test_each_error_declares_its_kind(
        self, error: DownloadError, expected: FailureKind
    ) -> None:
        assert error.kind is expected
        assert error.is_retryable is expected.is_retryable

    def test_every_error_is_catchable_as_one_type(self) -> None:
        message = "boom"

        with pytest.raises(DownloadError):
            raise ProviderError(message)

    def test_size_limit_error_reports_both_numbers(self) -> None:
        error = SizeLimitExceededError(100, 250)

        assert error.limit_bytes == 100
        assert error.observed_bytes == 250
        assert "100" in error.message

    def test_retry_after_is_carried(self) -> None:
        assert ProviderError("x", retry_after_seconds=42.0).retry_after_seconds == 42.0


class TestCancellation:
    def test_source_starts_uncancelled(self) -> None:
        source = CancellationSource()

        assert not source.cancelled
        assert source.reason is None

    def test_cancel_records_the_first_reason(self) -> None:
        source = CancellationSource()

        source.cancel(CancellationReason.REQUESTED)
        source.cancel(CancellationReason.TIMEOUT)

        assert source.cancelled
        assert source.reason is CancellationReason.REQUESTED

    def test_wait_returns_immediately_once_cancelled(self) -> None:
        source = CancellationSource()
        source.cancel()

        assert source.wait(timeout=0.01) is True

    def test_null_token_is_never_cancelled(self) -> None:
        token = NullCancellation()

        assert not token.cancelled
        assert token.reason is None
        assert token.wait(0) is False
