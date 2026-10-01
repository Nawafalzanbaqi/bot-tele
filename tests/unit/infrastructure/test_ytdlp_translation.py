"""Format selection, metadata mapping and error classification.

These three modules carry the adapter's real intelligence and are pure, so they
are tested exhaustively here without an engine, a network or a filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    ConnectionBlockedError,
    ContentRemovedError,
    DownloadFailedError,
    DrmProtectedError,
    FormatUnavailableError,
    GeoRestrictedError,
    MetadataUnavailableError,
    NoPlayableMediaError,
    ProviderError,
    RateLimitedError,
    SiteChallengeError,
    UnsupportedProviderError,
)
from mediahub.application.download.ports import DownloadRequest, FormatSelection
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
from mediahub.infrastructure.download.ytdlp.options import build_download_options
from mediahub.shared.config.settings import DownloadSettings
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

    def test_unknown_codecs_with_a_frame_size_are_video(self) -> None:
        """Twitch clips: vcodec and acodec both None (unknown), height known."""
        videos, audios = split_formats(
            [
                {"format_id": "1080", "ext": "mp4", "height": 1080},
                {"format_id": "720", "ext": "mp4", "height": 720, "vcodec": None, "acodec": None},
            ]
        )

        assert [video.format_id for video in videos] == ["1080", "720"]
        assert audios == ()

    def test_a_stream_declared_absent_is_not_video(self) -> None:
        """"none" is a statement; a missing codec is a shrug. Only the shrug counts."""
        videos, audios = split_formats(
            [{"format_id": "a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "height": 0}]
        )

        assert videos == ()
        assert [audio.format_id for audio in audios] == ["a"]

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
            # Age gates are fixed by signing in, so they are reported as
            # something the user can act on rather than as "unavailable".
            ("Sign in to confirm your age", AuthenticationRequiredError),
            (
                "The uploader has not made this video available in your country",
                GeoRestrictedError,
            ),
            ("HTTP Error 429: Too Many Requests", RateLimitedError),
            # Throttling, not a login wall. This phrase used to be classified
            # as a permanent authentication failure and was never retried.
            ("Please wait a few minutes before you try again.", RateLimitedError),
            ("This video is DRM protected", DrmProtectedError),
            ("Requested format is not available. DRM protected: Widevine", DrmProtectedError),
            ("Sign in to confirm you’re not a bot", AuthenticationRequiredError),  # noqa: RUF001
            ("HTTP Error 503: Service Unavailable", ProviderError),
            ("The read operation timed out", ProviderError),
            ("[Errno 104] Connection reset by peer", ConnectionBlockedError),
            # A browser challenge in front of the content, in the engine's
            # own words for PornHub and in Cloudflare's interstitial wording.
            ("PhantomJS not found, please install it", SiteChallengeError),
            ("Just a moment... Checking your browser before accessing", SiteChallengeError),
            ("Something nobody has ever seen before", DownloadFailedError),
        ],
    )
    def test_messages_map_to_the_right_class(self, message: str, expected: type[Exception]) -> None:
        assert isinstance(classify(Exception(message), url="u"), expected)

    def test_a_js_challenge_is_not_read_as_a_removed_post(self) -> None:
        """"PhantomJS not found" carried the bare "not found" of a deleted post until 2026-10-01."""
        message = "ERROR: [PornHub] 1: PhantomJS not found, please install it"
        error = classify(Exception(message), url="u")

        assert not isinstance(error, ContentRemovedError)
        assert error.code == "site_challenge"
        assert not error.is_retryable, "the same exit gets the same page again"

    def test_unknown_failures_stay_retryable(self) -> None:
        error = classify(Exception("a novel failure"), url="u")

        assert error.kind is FailureKind.TRANSIENT, "unknown must never mean permanent"

    def test_a_bare_404_inside_a_number_is_not_content_removed(self) -> None:
        """A bare "404" was a substring match: a byte count could bury a real link."""
        error = classify(Exception("Downloaded 404096 bytes before the stream stalled"), url="u")

        assert not isinstance(error, ContentRemovedError)
        assert error.is_retryable

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


class TestPlayableOutput:
    """The result has to open on the device it is sent to.

    These pin the fix for a real report: 1080p arrived, was genuinely 1920x1080,
    and would not play - AV1 video with Opus audio in a WebM container, which
    most phone players and chat clients cannot decode. A newer codec is not a
    better download if nothing renders it.
    """

    def test_a_compatible_merge_asks_for_h264_and_aac_first(self) -> None:
        expression = build_format_expression(
            FormatSelection.up_to_height(1080, allow_merge=True, prefer_compatible=True)
        )

        assert expression.startswith("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]")

    def test_it_still_falls_back_to_any_codec(self) -> None:
        """A source with no H.264 must yield something rather than nothing."""
        expression = build_format_expression(
            FormatSelection.up_to_height(1080, allow_merge=True, prefer_compatible=True)
        )

        assert "/bv*[height<=1080]+ba" in expression
        assert expression.endswith("/b")

    def test_hevc_is_the_second_choice_before_anything_goes(self) -> None:
        """H.265 plays inline too, and is smaller; VP9/AV1 come only when nothing else exists."""
        expression = build_format_expression(
            FormatSelection.up_to_height(1080, allow_merge=True, prefer_compatible=True)
        )
        tiers = expression.split("/")

        assert tiers[0].startswith("bv*[vcodec^=avc1]")
        assert tiers[1] == "bv*[vcodec^=hvc1][height<=1080]+ba[acodec^=mp4a]"
        assert tiers[2] == "bv*[vcodec^=hev1][height<=1080]+ba[acodec^=mp4a]"
        assert tiers[3] == "bv*[height<=1080]+ba"
        assert "vp9" not in expression
        assert "av01" not in expression

    def test_compatibility_is_opt_in(self) -> None:
        """A destination that plays anything should not pay for the preference."""
        expression = build_format_expression(FormatSelection.up_to_height(1080, allow_merge=True))

        assert "avc1" not in expression
        assert "mp4a" not in expression

    def test_the_merged_container_is_mp4(self) -> None:
        """WebM is what the streams arrive in and is not what may be delivered."""
        options = build_download_options(
            DownloadSettings(),
            DownloadRequest(url="https://example.com/a"),
            directory=Path("lease"),
            format_expression="bv*+ba",
            progress_hook=lambda _: None,
            postprocessor_hook=lambda _: None,
        )

        assert options["merge_output_format"] == "mp4"


SOURCE = "https://example.com/a"


class TestFailuresNameTheirCause:
    """A refusal must say which of several very different things happened.

    All of these used to collapse into "I could not read that link", which is
    true and useless: the user cannot tell a deleted post from one that needs
    signing in, so they re-check the link for something only cookies fix.
    """

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Unable to extract universal data for rehydration", AuthenticationRequiredError),
            ("Your IP address is blocked from accessing this post", AuthenticationRequiredError),
            ("NSFW tweet requires authentication", AuthenticationRequiredError),
            ("This account is private", AuthenticationRequiredError),
            ("Use --cookies-from-browser or --cookies", AuthenticationRequiredError),
        ],
        ids=["tiktok-rehydration", "tiktok-ip", "nsfw", "private", "yt-dlp-advice"],
    )
    def test_session_gated_failures_are_recognised(
        self, message: str, expected: type[Exception]
    ) -> None:
        assert isinstance(classify(RuntimeError(message), url=SOURCE), expected)

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("This account has been suspended", ContentRemovedError),
            ("The video has been deleted", ContentRemovedError),
            ("HTTP Error 404: Not Found", ContentRemovedError),
            ("Video not available from your location", GeoRestrictedError),
            ("This content is blocked in your country", GeoRestrictedError),
        ],
        ids=["suspended", "deleted", "404", "geo-location", "geo-country"],
    )
    def test_gone_and_geo_blocked_are_distinguished(
        self, message: str, expected: type[Exception]
    ) -> None:
        assert isinstance(classify(RuntimeError(message), url=SOURCE), expected)

    @pytest.mark.parametrize(
        "message",
        [
            "No video could be found in this tweet",
            "No video could be found in this post",
            "No video formats found!",
            "no formats found for this item",
        ],
        ids=["x-photo-tweet", "bluesky-photo-post", "pinterest-image-pin", "generic"],
    )
    def test_a_photo_only_post_is_not_reported_as_a_login_problem(self, message: str) -> None:
        """The expensive misdiagnosis, and the reason this class exists.

        Every one of these means the post was read and holds no stream. Calling
        it "sign in required" sends someone to re-export cookies that were never
        the problem; calling it "gone" sends them to re-check a link that is
        fine; calling it "that quality is unavailable" sends them to a list of
        qualities that is empty.
        """
        error = classify(RuntimeError(message), url=SOURCE)

        assert isinstance(error, NoPlayableMediaError)
        assert not isinstance(error, AuthenticationRequiredError | ContentRemovedError)
        assert not error.is_retryable

    def test_a_reset_connection_is_not_the_site_being_busy(self) -> None:
        """DNS fine, TCP fine, handshake killed - that is the path, not the site.

        Reported as an ordinary transient failure it becomes "the site is having
        trouble, try later", which is wrong in a way that costs days: it will
        never succeed, because the traffic is not reaching the site at all.
        """
        error = classify(
            OSError("Unable to download webpage: [Errno 104] Connection reset by peer"),
            url=SOURCE,
        )

        assert isinstance(error, ConnectionBlockedError)
        # Still retryable: one reset really can be noise. It is the explanation
        # that differs, not the retry policy.
        assert error.is_retryable

    def test_rate_limiting_stays_transient(self) -> None:
        """It is the one refusal where waiting is the whole instruction."""
        error = classify(RuntimeError("HTTP Error 429: Too Many Requests"), url=SOURCE)

        assert isinstance(error, RateLimitedError)
        assert error.kind is FailureKind.TRANSIENT
