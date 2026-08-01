"""Format selection, metadata mapping and error classification.

These three modules carry the adapter's real intelligence and are pure, so they
are tested exhaustively here without an engine, a network or a filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mediahub.application.download.errors import (
    DownloadFailedError,
    FormatUnavailableError,
    MetadataUnavailableError,
    ProviderError,
    UnsupportedProviderError,
)
from mediahub.application.download.ports import FormatSelection
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.media.enums import MediaType
from mediahub.infrastructure.download.ytdlp.errors import classify, extract_retry_after
from mediahub.infrastructure.download.ytdlp.format_selection import build_format_expression
from mediahub.infrastructure.download.ytdlp.mapping import (
    split_formats,
    to_metadata,
    to_selected_format,
    to_thumbnails,
)
from tests.support.ytdlp_fakes import playlist_info, video_info

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class TestFormatExpression:
    def test_best_prefers_a_single_file(self) -> None:
        expression = build_format_expression(FormatSelection.best())

        assert "+" not in expression, "merging requires FFmpeg and must be opt-in"
        assert expression.endswith("/b")

    def test_best_with_merge_allows_separate_streams(self) -> None:
        expression = build_format_expression(FormatSelection.best(allow_merge=True))

        assert "bv*+ba" in expression
        assert expression.endswith("/b"), "must still fall back to a single file"

    def test_height_cap_is_applied(self) -> None:
        assert "[height<=720]" in build_format_expression(FormatSelection.up_to_height(720))

    def test_audio_only_never_requests_video(self) -> None:
        expression = build_format_expression(FormatSelection.audio_only())

        assert expression.startswith("ba")
        assert "bv" not in expression

    def test_specific_keeps_a_fallback(self) -> None:
        expression = build_format_expression(FormatSelection.specific("137"))

        assert expression.startswith("137/")

    def test_container_preference_degrades_gracefully(self) -> None:
        expression = build_format_expression(FormatSelection(prefer_container="mp4"))

        assert "[ext=mp4]" in expression
        assert expression.endswith("/b")

    def test_filesize_filter_admits_unknown_sizes(self) -> None:
        expression = build_format_expression(FormatSelection(max_filesize_bytes=1000))

        assert "[filesize<?1000]" in expression, "unknown sizes must not be excluded here"


class TestFormatMapping:
    def test_splits_video_and_audio_and_drops_storyboards(self) -> None:
        videos, audios = split_formats(video_info()["formats"])

        assert [video.format_id for video in videos] == ["137", "18"]
        assert [audio.format_id for audio in audios] == ["140"]

    def test_video_fields_are_mapped(self) -> None:
        videos, _ = split_formats(video_info()["formats"])
        best = videos[0]

        assert best.height == 1080
        assert best.resolution == "1920x1080"
        assert best.video_codec == "avc1.640028"
        assert best.audio_codec is None, "'none' must become None"
        assert best.filesize_is_estimate is True

    def test_audio_fields_are_mapped(self) -> None:
        _, audios = split_formats(video_info()["formats"])
        track = audios[0]

        assert track.bitrate_kbps == 128.0
        assert track.sample_rate_hz == 44100
        assert track.channels == 2
        assert track.language == "en"
        assert track.filesize_is_estimate is False

    def test_missing_fields_become_none(self) -> None:
        videos, _ = split_formats([{"format_id": "x", "vcodec": "avc1"}])

        assert videos[0].height is None
        assert videos[0].filesize_bytes is None

    def test_thumbnails_are_ordered_largest_first(self) -> None:
        thumbnails = to_thumbnails(video_info()["thumbnails"])

        assert thumbnails[0].width == 1920
        assert thumbnails[0].pixels > thumbnails[1].pixels

    def test_thumbnails_without_a_url_are_dropped(self) -> None:
        assert to_thumbnails([{"width": 10}]) == ()


class TestMetadataMapping:
    def test_maps_a_video(self) -> None:
        metadata = to_metadata(video_info(), url="https://example.com/a", probed_at=NOW)

        assert metadata.provider == "testsite"
        assert metadata.provider_item_id == "abc123"
        assert metadata.title == "A Test Video"
        assert metadata.kind is MediaType.VIDEO
        assert metadata.duration_ms == 125_000
        assert metadata.expected_bytes == 12_000_000
        assert metadata.available_qualities() == ("1080p", "360p")
        assert metadata.is_live is False
        assert metadata.is_playlist is False

    def test_upload_date_is_timezone_aware(self) -> None:
        metadata = to_metadata(video_info(), url="https://example.com/a", probed_at=NOW)

        assert metadata.upload_date is not None
        assert metadata.upload_date.tzinfo is not None

    def test_timestamp_wins_over_upload_date(self) -> None:
        metadata = to_metadata(
            video_info(timestamp=1_767_225_600), url="https://example.com/a", probed_at=NOW
        )

        assert metadata.upload_date is not None
        assert metadata.upload_date.year == 2026

    def test_live_status_is_detected_either_way(self) -> None:
        by_flag = to_metadata(video_info(is_live=True), url="u", probed_at=NOW)
        by_status = to_metadata(video_info(live_status="is_live"), url="u", probed_at=NOW)

        assert by_flag.is_live
        assert by_status.is_live

    def test_playlists_are_recognised_and_counted(self) -> None:
        metadata = to_metadata(playlist_info(42), url="https://example.com/p", probed_at=NOW)

        assert metadata.is_playlist
        assert metadata.entry_count == 42

    def test_audio_only_source_is_classified_as_audio(self) -> None:
        info = video_info(
            formats=[
                {
                    "format_id": "140",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                }
            ]
        )

        assert to_metadata(info, url="u", probed_at=NOW).kind is MediaType.AUDIO

    def test_an_almost_empty_info_dict_still_maps(self) -> None:
        metadata = to_metadata({}, url="https://example.com/a", probed_at=NOW)

        assert metadata.title == "untitled"
        assert metadata.provider == "generic"
        assert metadata.kind is MediaType.OTHER
        assert metadata.expected_bytes is None

    def test_hostile_values_do_not_crash_the_mapping(self) -> None:
        info = {
            "title": 12345,
            "duration": "not a number",
            "formats": "not a list",
            "thumbnails": {"nope": True},
            "age_limit": -1,
        }

        metadata = to_metadata(info, url="u", probed_at=NOW)

        assert metadata.title == "untitled"
        assert metadata.duration_ms is None
        assert metadata.video_formats == ()
        assert metadata.age_limit is None

    def test_description_is_truncated(self) -> None:
        metadata = to_metadata(video_info(description="x" * 10_000), url="u", probed_at=NOW)

        assert metadata.description is not None
        assert len(metadata.description) <= 2000


class TestSelectedFormatMapping:
    def test_prefers_requested_downloads(self) -> None:
        info = video_info(
            requested_downloads=[
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                    "width": 1920,
                    "height": 1080,
                }
            ]
        )

        selected = to_selected_format(info)

        assert selected.format_id == "137"
        assert selected.height == 1080
        assert selected.is_audio_only is False

    def test_audio_only_is_detected(self) -> None:
        info = {"requested_downloads": [{"format_id": "140", "vcodec": "none", "ext": "m4a"}]}

        assert to_selected_format(info).is_audio_only is True

    def test_falls_back_to_the_top_level(self) -> None:
        assert to_selected_format({"format_id": "18", "ext": "mp4"}).format_id == "18"

    def test_unknown_format_is_named_rather_than_crashing(self) -> None:
        assert to_selected_format({}).format_id == "unknown"


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("ERROR: Unsupported URL: https://example.com/x", UnsupportedProviderError),
            ("Requested format is not available", FormatUnavailableError),
            ("Video unavailable", MetadataUnavailableError),
            ("This video is private", MetadataUnavailableError),
            ("Sign in to confirm your age", MetadataUnavailableError),
            (
                "The uploader has not made this video available in your country",
                MetadataUnavailableError,
            ),
            ("HTTP Error 429: Too Many Requests", ProviderError),
            ("HTTP Error 503: Service Unavailable", ProviderError),
            ("The read operation timed out", ProviderError),
            ("[Errno 104] Connection reset by peer", ProviderError),
            ("Something nobody has ever seen before", DownloadFailedError),
        ],
    )
    def test_messages_map_to_the_right_class(self, message: str, expected: type[Exception]) -> None:
        assert isinstance(classify(Exception(message), url="u"), expected)

    def test_unknown_failures_stay_retryable(self) -> None:
        error = classify(Exception("a novel failure"), url="u")

        assert error.kind is FailureKind.TRANSIENT, "unknown must never mean permanent"

    def test_exception_class_names_are_honoured(self) -> None:
        class UnsupportedError(Exception):
            pass

        assert isinstance(classify(UnsupportedError("x"), url="u"), UnsupportedProviderError)

    def test_wrapped_causes_are_inspected(self) -> None:
        inner = TimeoutError("timed out")
        outer = Exception("Unable to download webpage")
        outer.__cause__ = inner

        assert isinstance(classify(outer, url="u"), ProviderError)

    def test_already_typed_errors_pass_through(self) -> None:
        original = FormatUnavailableError("nope")

        assert classify(original, url="u") is original

    def test_provider_is_recorded(self) -> None:
        error = classify(Exception("Video unavailable"), url="u", provider="testsite")

        assert error.provider == "testsite"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("please retry after 42 seconds", 42.0),
            ("Retry-After: 7", 7.0),
            ("retry_after 1.5", 1.5),
            ("no hint here", None),
            ("retry after 0", None),
        ],
    )
    def test_retry_after_is_extracted(self, text: str, expected: float | None) -> None:
        assert extract_retry_after(text) == expected

    def test_retry_after_reaches_the_error(self) -> None:
        error = classify(Exception("HTTP Error 429, retry after 30"), url="u")

        assert error.retry_after_seconds == 30.0
