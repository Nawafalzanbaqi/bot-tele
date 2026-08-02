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
    ORIGINAL_KEY,
    build_quality_options,
    resolve_auto,
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

    def test_does_not_ask_for_a_merge_unless_the_deployment_allows_one(self) -> None:
        # Merging needs FFmpeg. A deployment without one must not request it.
        options = build_quality_options(metadata(video_formats=(video(1080),)))

        assert not selection_for(BEST_KEY, options).allow_merge

    def test_a_merging_deployment_asks_for_one(self) -> None:
        """Otherwise the higher rungs are labels for a quality never delivered.

        Above roughly 720p the large platforms ship video and audio separately.
        Without a merge, "1080p" resolves to the best already-muxed rendition -
        usually 720p - and nothing anywhere reports the substitution.
        """
        options = build_quality_options(metadata(video_formats=(video(1080),)))

        assert selection_for(BEST_KEY, options, allow_merge=True).allow_merge
        assert selection_for("h1080", options, allow_merge=True).allow_merge

    def test_audio_only_never_merges_however_it_is_configured(self) -> None:
        """There is no second stream to merge, and asking would cost a re-encode."""
        options = build_quality_options(metadata(audio_formats=(AudioFormat(format_id="a"),)))

        assert not selection_for(AUDIO_KEY, options, allow_merge=True).allow_merge


class TestSizesAreDeliveredSizes:
    """The number on a button must be the number that arrives.

    Above roughly 720p a platform stops offering a muxed file, so the rendition
    behind those rungs is video *only* and an audio track is attached to it on
    the way. Reporting the video stream alone understates exactly the rungs
    people reach for, and a size that cannot be trusted is worse than no size,
    because it is what they plan around.
    """

    def test_a_video_only_rung_includes_the_audio_it_will_be_given(self) -> None:
        options = build_quality_options(
            metadata(
                video_formats=(video(1080, 40_000_000),),
                audio_formats=(AudioFormat(format_id="a", filesize_bytes=5_000_000),),
            ),
            allow_merge=True,
        )

        assert next(o for o in options if o.key == "h1080").approx_bytes == 45_000_000

    def test_a_rung_that_already_carries_audio_is_not_inflated(self) -> None:
        muxed = VideoFormat(
            format_id="v720", height=720, filesize_bytes=20_000_000, audio_codec="mp4a"
        )
        options = build_quality_options(
            metadata(
                video_formats=(muxed,),
                audio_formats=(AudioFormat(format_id="a", filesize_bytes=5_000_000),),
            ),
            allow_merge=True,
        )

        assert next(o for o in options if o.key == "h720").approx_bytes == 20_000_000

    def test_without_merging_nothing_is_added(self) -> None:
        options = build_quality_options(
            metadata(
                video_formats=(video(1080, 40_000_000),),
                audio_formats=(AudioFormat(format_id="a", filesize_bytes=5_000_000),),
            )
        )

        assert next(o for o in options if o.key == "h1080").approx_bytes == 40_000_000

    def test_a_rung_is_dropped_once_the_audio_pushes_it_over_the_ceiling(self) -> None:
        """The check that failed in production: 48 MB of video fits 50, 53 does not."""
        options = build_quality_options(
            metadata(
                video_formats=(video(1080, 48_000_000), video(720, 20_000_000)),
                audio_formats=(AudioFormat(format_id="a", filesize_bytes=5_000_000),),
            ),
            max_bytes=50_000_000,
            allow_merge=True,
        )

        assert [o.key for o in options if o.height] == ["h720"]

    def test_an_unknown_size_stays_unknown_rather_than_becoming_the_audio_size(self) -> None:
        options = build_quality_options(
            metadata(
                video_formats=(video(1080),),
                audio_formats=(AudioFormat(format_id="a", filesize_bytes=5_000_000),),
            ),
            allow_merge=True,
        )

        assert next(o for o in options if o.key == "h1080").approx_bytes is None

    def test_the_audio_the_merge_will_take_is_the_one_counted(self) -> None:
        """The merge asks for the *best* audio, so that is what arrives.

        Measured against a real source, assuming the smallest understated the
        total by 4% - wrong, and near a hard upload ceiling wrong in the
        direction that admits a download which then fails.
        """
        options = build_quality_options(
            metadata(
                video_formats=(video(1080, 40_000_000),),
                audio_formats=(
                    AudioFormat(format_id="hi", filesize_bytes=9_000_000),
                    AudioFormat(format_id="lo", filesize_bytes=3_000_000),
                ),
            ),
            allow_merge=True,
        )

        assert next(o for o in options if o.key == "h1080").approx_bytes == 49_000_000

    def test_a_rung_uses_a_sibling_that_knows_its_size(self) -> None:
        """Observed on YouTube: the first 1080p listed declares no size.

        Taking whichever rendition came first left the rung showing "unknown"
        while a sibling at the same resolution knew exactly how big it was.
        """
        unsized = VideoFormat(format_id="270", height=1080, video_codec="avc1.640028")
        sized = VideoFormat(
            format_id="137", height=1080, video_codec="avc1.640028", filesize_bytes=37_577_764
        )

        options = build_quality_options(metadata(video_formats=(unsized, sized)))

        assert next(o for o in options if o.key == "h1080").approx_bytes == 37_577_764

    def test_the_size_quoted_is_the_codec_that_will_be_taken(self) -> None:
        """H.264 is around a third larger than AV1 at the same resolution.

        Quoting the AV1 figure and then downloading H.264 understates every
        rung - which is the same class of untruth as the resolution one.
        """
        av1 = VideoFormat(
            format_id="399", height=1080, video_codec="av01.0.08M.08", filesize_bytes=20_793_577
        )
        h264 = VideoFormat(
            format_id="137", height=1080, video_codec="avc1.640028", filesize_bytes=37_577_764
        )
        source = metadata(video_formats=(av1, h264))

        compatible = build_quality_options(source, prefer_compatible=True)
        anything = build_quality_options(source)

        assert next(o for o in compatible if o.key == "h1080").approx_bytes == 37_577_764
        assert next(o for o in anything if o.key == "h1080").approx_bytes == 37_577_764

    def test_rungs_are_still_offered_tallest_first(self) -> None:
        options = build_quality_options(
            metadata(video_formats=(video(360, 1), video(1080, 3), video(720, 2)))
        )

        assert [o.key for o in options if o.height] == ["h1080", "h720", "h360"]


class TestResolveAuto:
    """ "Best quality" means the best one that will actually arrive.

    Asking the engine for the best rendition full stop is a different thing,
    and on a long source it is routinely larger than any chat service accepts -
    so the failure lands after the download rather than before it.
    """

    def test_the_tallest_that_fits_is_chosen(self) -> None:
        options = build_quality_options(
            metadata(video_formats=(video(1080, 90), video(720, 40), video(360, 10)))
        )

        assert resolve_auto(options, ceiling=50).key == "h720"

    def test_with_no_ceiling_the_best_is_chosen(self) -> None:
        options = build_quality_options(metadata(video_formats=(video(1080, 90), video(720, 40))))

        assert resolve_auto(options, ceiling=None).key == BEST_KEY

    def test_a_rung_of_unknown_size_is_a_candidate(self) -> None:
        """An absent size is not a large one, and the engine caps while streaming."""
        options = build_quality_options(metadata(video_formats=(video(1080, None),)))

        assert resolve_auto(options, ceiling=10).key in {BEST_KEY, "h1080"}

    def test_audio_is_the_fallback_when_no_video_fits(self) -> None:
        """A two-hour talk that will not fit as video usually fits as sound."""
        options = build_quality_options(
            metadata(
                video_formats=(video(1080, 900), video(720, 800)),
                audio_formats=(AudioFormat(format_id="a", filesize_bytes=5),),
            ),
            max_bytes=None,
        )

        assert resolve_auto(options, ceiling=10).is_audio_only

    def test_video_is_preferred_over_audio_when_both_fit(self) -> None:
        options = build_quality_options(
            metadata(
                video_formats=(video(720, 20),),
                audio_formats=(AudioFormat(format_id="a", filesize_bytes=5),),
            )
        )

        assert not resolve_auto(options, ceiling=1000).is_audio_only

    def test_a_source_offering_nothing_is_refused(self) -> None:
        with pytest.raises(FormatUnavailableError):
            resolve_auto((), ceiling=None)


class TestSourcesWithNoStreams:
    """Stories, photo posts and bare file links publish no *streams*.

    They are not unfetchable - there is simply nothing to choose between. The
    menu used to be empty for them, so the bot answered "nothing here can be
    fetched" for a story that downloads perfectly.
    """

    def test_an_image_post_offers_one_option(self) -> None:
        options = build_quality_options(metadata(kind=MediaType.IMAGE))

        assert [option.key for option in options] == [ORIGINAL_KEY]

    def test_auto_takes_it(self) -> None:
        options = build_quality_options(metadata(kind=MediaType.IMAGE))

        assert resolve_auto(options, ceiling=1000).key == ORIGINAL_KEY

    def test_it_is_taken_even_when_the_size_is_unknown(self) -> None:
        """A ceiling cannot exclude the only thing on offer.

        Weighing it would only ever mean refusing, and the engine still
        enforces the real limit while streaming.
        """
        options = build_quality_options(metadata(kind=MediaType.IMAGE))

        assert resolve_auto(options, ceiling=1).key == ORIGINAL_KEY

    def test_it_asks_for_no_merge(self) -> None:
        """There is no second stream; asking would send the engine hunting."""
        options = build_quality_options(metadata(kind=MediaType.IMAGE))

        selection = selection_for(ORIGINAL_KEY, options, allow_merge=True, prefer_compatible=True)

        assert not selection.allow_merge
        assert selection.max_height is None

    def test_a_video_source_never_gets_this_entry(self) -> None:
        """It exists for sources with nothing to choose, not as a fallback."""
        options = build_quality_options(metadata(video_formats=(video(720, 10),)))

        assert ORIGINAL_KEY not in [option.key for option in options]

    def test_an_audio_only_source_keeps_its_own_entry(self) -> None:
        options = build_quality_options(
            metadata(audio_formats=(AudioFormat(format_id="a", filesize_bytes=10),))
        )

        assert [option.key for option in options] == [AUDIO_KEY]


class TestVerticalVideoIsLabelledAndFetchedCorrectly:
    """A phone clip is 1080p when it is 1080 wide, not 1440p because it is tall.

    Measured on a real TikTok source before this was fixed: the ladder read
    1440p/1080p/720p for renditions that are actually 1080p and 720p, and
    choosing the top rung capped the engine at 1440 pixels tall - which excluded
    the 1080x1920 rendition the rung was named after. 6.22 MB arrived where
    16.86 MB was published, labelled as the higher quality.
    """

    @staticmethod
    def portrait(width: int, height: int, size: int | None = None) -> VideoFormat:
        return VideoFormat(
            format_id=f"v{width}x{height}", width=width, height=height, filesize_bytes=size
        )

    def _tiktok(self) -> MediaMetadata:
        return metadata(
            video_formats=(
                self.portrait(1080, 1920, 16_857_157),
                self.portrait(720, 1280, 6_525_878),
            )
        )

    def test_rungs_are_named_after_the_short_side(self) -> None:
        options = build_quality_options(self._tiktok())

        assert [option.label for option in options if option.key.startswith("h")] == [
            "1080p",
            "720p",
        ]

    def test_the_top_rung_does_not_exclude_its_own_rendition(self) -> None:
        """The defect that cost quality rather than just wording."""
        options = build_quality_options(self._tiktok())
        top = next(option for option in options if option.key == "h1080")

        selection = selection_for(top.key, options)

        assert selection.max_height is not None
        assert (
            selection.max_height >= 1920
        ), "capping at the label would exclude the 1080x1920 rendition it names"

    def test_a_lower_rung_still_excludes_the_higher_one(self) -> None:
        options = build_quality_options(self._tiktok())
        lower = next(option for option in options if option.key == "h720")

        selection = selection_for(lower.key, options)

        assert selection.max_height is not None
        assert selection.max_height < 1920, "720p must not be able to take the 1080p rendition"

    def test_landscape_video_is_unchanged(self) -> None:
        """The fix must not move the answer for ordinary video."""
        options = build_quality_options(
            metadata(video_formats=(self.portrait(1920, 1080, 40_000_000),))
        )
        rung = next(option for option in options if option.key.startswith("h"))

        assert rung.label == "1080p"
        assert selection_for(rung.key, options).max_height == 1080

    def test_a_format_without_a_width_still_buckets(self) -> None:
        """Some extractors report only a height; that must not vanish."""
        options = build_quality_options(metadata(video_formats=(video(720, 5_000_000),)))

        assert [option.label for option in options if option.key.startswith("h")] == ["720p"]
