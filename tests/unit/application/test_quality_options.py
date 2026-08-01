"""Which qualities are offered, and what choosing one means."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mediahub.application.download.errors import FormatUnavailableError
from mediahub.application.download.ports import (
    AudioFormat,
    FormatPreference,
    MediaMetadata,
    VideoFormat,
)
from mediahub.application.download.quality import (
    AUDIO_KEY,
    BEST_KEY,
    MAX_OPTIONS,
    build_quality_options,
    selection_for,
)
from mediahub.domain.media.enums import MediaType

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def metadata(**overrides: object) -> MediaMetadata:
    defaults: dict[str, object] = {
        "url": "https://example.com/a",
        "provider": "testsite",
        "title": "A",
        "kind": MediaType.VIDEO,
        "probed_at": NOW,
    }
    defaults.update(overrides)
    return MediaMetadata(**defaults)  # type: ignore[arg-type]


def video(height: int, size: int | None = None) -> VideoFormat:
    return VideoFormat(format_id=f"v{height}", height=height, filesize_bytes=size)


class TestBuildOptions:
    def test_offers_best_plus_one_option_per_resolution(self) -> None:
        options = build_quality_options(
            metadata(video_formats=(video(1080), video(720), video(360)))
        )

        assert [option.key for option in options] == [BEST_KEY, "h1080", "h720", "h360"]

    def test_deduplicates_resolutions(self) -> None:
        options = build_quality_options(
            metadata(video_formats=(video(1080), video(1080), video(1085)))
        )

        assert [option.key for option in options] == [BEST_KEY, "h1080"]

    def test_rounds_odd_heights_down_to_a_known_rung(self) -> None:
        options = build_quality_options(metadata(video_formats=(video(900),)))

        assert options[1].key == "h720"
        assert options[1].label == "720p"

    def test_ignores_formats_without_a_height(self) -> None:
        options = build_quality_options(
            metadata(video_formats=(VideoFormat(format_id="x"), video(480)))
        )

        assert [option.key for option in options] == [BEST_KEY, "h480"]

    def test_offers_audio_when_an_audio_stream_exists(self) -> None:
        options = build_quality_options(
            metadata(audio_formats=(AudioFormat(format_id="a", filesize_bytes=100),))
        )

        assert [option.key for option in options] == [AUDIO_KEY]
        assert options[0].is_audio_only

    def test_drops_renditions_known_to_exceed_the_ceiling(self) -> None:
        options = build_quality_options(
            metadata(video_formats=(video(1080, 900), video(360, 100))),
            max_bytes=500,
        )

        assert [option.key for option in options] == [BEST_KEY, "h360"]

    def test_keeps_renditions_of_unknown_size(self) -> None:
        # An absent size is not a large one; dropping it would hide options.
        options = build_quality_options(metadata(video_formats=(video(1080, None),)), max_bytes=10)

        assert "h1080" in [option.key for option in options]

    def test_is_bounded(self) -> None:
        formats = tuple(video(height) for height in (2160, 1440, 1080, 720, 480, 360))
        options = build_quality_options(
            metadata(video_formats=formats, audio_formats=(AudioFormat(format_id="a"),))
        )

        assert len(options) <= MAX_OPTIONS

    def test_a_source_with_nothing_offers_nothing(self) -> None:
        assert build_quality_options(metadata()) == ()

    def test_option_keys_are_short_enough_for_a_button(self) -> None:
        options = build_quality_options(
            metadata(
                video_formats=(video(2160), video(360)),
                audio_formats=(AudioFormat(format_id="a"),),
            )
        )

        assert all(len(option.key) <= 12 for option in options)


class TestSelectionFor:
    def test_best_maps_to_the_best_selection(self) -> None:
        options = build_quality_options(metadata(video_formats=(video(1080),)))

        selection = selection_for(BEST_KEY, options)

        assert selection.preference is FormatPreference.BEST
        assert selection.max_height is None

    def test_a_height_maps_to_a_capped_selection(self) -> None:
        options = build_quality_options(metadata(video_formats=(video(1080), video(720))))

        assert selection_for("h720", options).max_height == 720

    def test_audio_maps_to_an_audio_only_selection(self) -> None:
        options = build_quality_options(metadata(audio_formats=(AudioFormat(format_id="a"),)))

        assert selection_for(AUDIO_KEY, options).wants_audio_only

    def test_an_unoffered_key_is_refused(self) -> None:
        options = build_quality_options(metadata(video_formats=(video(720),)))

        with pytest.raises(FormatUnavailableError):
            selection_for("h4320", options)

    def test_never_asks_for_a_merge(self) -> None:
        # Merging needs FFmpeg, which is a different subsystem's job.
        options = build_quality_options(metadata(video_formats=(video(1080),)))

        assert not selection_for(BEST_KEY, options).allow_merge
