"""The completeness check: a file that arrived is not the same as a file that plays.

Pure parts first (parsing ffprobe's JSON, judging a report), then the tool
itself where it is installed, then the adapter refusing to hand over a file the
inspector rejected.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import DownloadIncompleteError
from mediahub.application.download.ports import DownloadRequest, FormatSelection
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.download.ytdlp.inspection import (
    DURATION_TOLERANCE_SECONDS,
    FfprobeInspector,
    StreamReport,
    parse_report,
    verify_complete,
)
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, download_script, factory_for, video_info

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit

URL = "https://example.com/watch?v=abc123"

FFPROBE_OUTPUT = """{
    "programs": [],
    "streams": [
        {"codec_name": "h264", "codec_type": "video", "width": 1920, "height": 1080},
        {"codec_name": "aac", "codec_type": "audio"}
    ],
    "format": {"duration": "125.040000"}
}"""


def report(
    seconds: float | None = 125.0,
    *,
    video: tuple[str, ...] = ("h264",),
    audio: tuple[str, ...] = ("aac",),
) -> StreamReport:
    return StreamReport(duration_seconds=seconds, video_codecs=video, audio_codecs=audio)


class TestParsing:
    def test_reads_streams_and_duration(self) -> None:
        parsed = parse_report(FFPROBE_OUTPUT)

        assert parsed.video_codecs == ("h264",)
        assert parsed.audio_codecs == ("aac",)
        assert parsed.duration_seconds == pytest.approx(125.04)
        assert (parsed.width, parsed.height) == (1920, 1080)
        assert parsed.has_streams

    def test_an_unknown_duration_is_none_not_zero(self) -> None:
        parsed = parse_report('{"streams": [{"codec_type": "video", "codec_name": "mjpeg"}]}')

        assert parsed.duration_seconds is None
        assert parsed.has_video

    @pytest.mark.parametrize("text", ["", "not json", "[]", '{"streams": "nope"}', "null"])
    def test_garbage_is_a_file_with_no_streams(self, text: str) -> None:
        parsed = parse_report(text)

        assert not parsed.has_streams
        assert parsed.duration_seconds is None

    def test_a_negative_duration_is_unknown(self) -> None:
        assert parse_report('{"format": {"duration": "-1"}}').duration_seconds is None


class TestJudgement:
    def test_a_complete_file_passes(self) -> None:
        verify_complete(report(125.0), expected_seconds=125.0, expect_video=True, url=URL)

    def test_a_truncated_file_is_refused_with_both_numbers(self) -> None:
        with pytest.raises(DownloadIncompleteError) as excinfo:
            verify_complete(report(40.0), expected_seconds=125.0, expect_video=True, url=URL)

        error = excinfo.value
        assert error.code == "download_incomplete"
        assert error.kind is FailureKind.TRANSIENT, "a re-fetch usually completes"
        assert error.expected_seconds == 125.0
        assert error.actual_seconds == 40.0

    def test_rounding_and_segment_boundaries_are_tolerated(self) -> None:
        verify_complete(
            report(125.0 - DURATION_TOLERANCE_SECONDS + 0.5),
            expected_seconds=125.0,
            expect_video=True,
            url=URL,
        )

    def test_the_tolerance_grows_with_a_long_source(self) -> None:
        """Five percent of two hours is six minutes; five seconds would refuse real files."""
        verify_complete(report(7200 - 300), expected_seconds=7200, expect_video=True, url=URL)

        with pytest.raises(DownloadIncompleteError):
            verify_complete(report(7200 - 400), expected_seconds=7200, expect_video=True, url=URL)

    def test_a_longer_file_is_not_a_truncation(self) -> None:
        verify_complete(report(130.0), expected_seconds=125.0, expect_video=True, url=URL)

    def test_unknown_durations_are_not_judged(self) -> None:
        verify_complete(report(None), expected_seconds=125.0, expect_video=True, url=URL)
        verify_complete(report(40.0), expected_seconds=None, expect_video=True, url=URL)

    def test_a_file_with_no_stream_is_refused_whatever_its_length(self) -> None:
        with pytest.raises(DownloadIncompleteError):
            verify_complete(
                report(125.0, video=(), audio=()),
                expected_seconds=None,
                expect_video=False,
                url=URL,
            )

    def test_a_video_request_that_yielded_only_audio_is_refused(self) -> None:
        with pytest.raises(DownloadIncompleteError):
            verify_complete(
                report(125.0, video=()), expected_seconds=125.0, expect_video=True, url=URL
            )

    def test_an_audio_request_is_complete_without_a_picture(self) -> None:
        verify_complete(
            report(125.0, video=()), expected_seconds=125.0, expect_video=False, url=URL
        )


class TestTheTool:
    async def test_a_missing_executable_is_a_skipped_check_said_once(self, tmp_path: Path) -> None:
        inspector = FfprobeInspector("definitely-not-an-ffprobe-binary")
        target = tmp_path / "clip.mp4"
        target.write_bytes(b"\0" * 64)

        assert await inspector.inspect(target) is None
        assert await inspector.inspect(target) is None
        assert inspector._missing is True

    @pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not installed here")
    async def test_a_file_that_is_not_media_has_no_streams(self, tmp_path: Path) -> None:
        target = tmp_path / "clip.mp4"
        target.write_bytes(b"<html>not a video</html>" * 100)

        result = await FfprobeInspector().inspect(target)

        assert result is not None
        assert not result.has_streams

    @pytest.mark.skipif(
        shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None,
        reason="ffmpeg and ffprobe are not installed here",
    )
    async def test_a_real_clip_is_described(self, tmp_path: Path) -> None:
        target = tmp_path / "clip.mp4"
        await asyncio.to_thread(
            subprocess.run,
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=2:size=64x64:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "aac",
                "-shortest",
                "-y",
                str(target),
            ],
            check=True,
            timeout=60,
        )

        result = await FfprobeInspector().inspect(target)

        assert result is not None
        assert result.video_codecs == ("h264",)
        assert result.audio_codecs == ("aac",)
        assert result.duration_seconds == pytest.approx(2.0, abs=0.2)
        assert (result.width, result.height) == (64, 64)
        verify_complete(result, expected_seconds=2.0, expect_video=True, url=URL)


class RecordingInspector:
    """Answers with a scripted report and remembers what it was asked to look at."""

    def __init__(self, answer: StreamReport | None) -> None:
        self.answer = answer
        self.seen: list[Path] = []

    async def inspect(self, path: Path) -> StreamReport | None:
        self.seen.append(path)
        return self.answer


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="test") as leased:
        yield leased


def engine(inspector: RecordingInspector) -> YtDlpDownloader:
    FakeYoutubeDL.instances.clear()
    return YtDlpDownloader(
        DownloadSettings(enabled=True, probe_attempts=1, progress_interval_seconds=0.0),
        url_policy=UrlPolicy(),
        address_guard=None,
        youtube_dl_factory=factory_for(info=video_info(), script=download_script()),
        inspector=inspector,
    )


class TestTheAdapterRefusesWhatTheInspectorRejects:
    async def test_a_complete_file_is_delivered_and_the_primary_was_inspected(
        self, scope: WorkspaceScope
    ) -> None:
        inspector = RecordingInspector(report(125.0))

        result = await engine(inspector).fetch(
            DownloadRequest(url=URL, selection=FormatSelection.best()), scope
        )

        assert result.primary.name == "abc123.mp4"
        assert [path.name for path in inspector.seen] == ["abc123.mp4"]

    async def test_a_truncated_file_is_refused_and_removed(self, scope: WorkspaceScope) -> None:
        """The source declares 125 s; the file holds 40. Nothing may survive in the lease."""
        inspector = RecordingInspector(report(40.0))

        with pytest.raises(DownloadIncompleteError):
            await engine(inspector).fetch(
                DownloadRequest(url=URL, selection=FormatSelection.best()), scope
            )

        assert scope.names() == ()

    async def test_an_audio_request_is_judged_as_audio(self, scope: WorkspaceScope) -> None:
        """The taken format says audio-only, so a file without video is complete."""
        FakeYoutubeDL.instances.clear()
        inspector = RecordingInspector(report(125.0, video=()))
        adapter = YtDlpDownloader(
            DownloadSettings(enabled=True, probe_attempts=1, progress_interval_seconds=0.0),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory_for(
                info=video_info(
                    requested_downloads=[{"format_id": "140", "ext": "m4a", "vcodec": "none"}]
                ),
                script=download_script(name="abc123.m4a"),
            ),
            inspector=inspector,
        )

        result = await adapter.fetch(
            DownloadRequest(url=URL, selection=FormatSelection.audio_only()), scope
        )

        assert result.selected_format.is_audio_only is True

    async def test_a_skipped_inspection_does_not_refuse_the_file(
        self, scope: WorkspaceScope
    ) -> None:
        result = await engine(RecordingInspector(None)).fetch(
            DownloadRequest(url=URL, selection=FormatSelection.best()), scope
        )

        assert result.primary.size_bytes == 4096

    async def test_no_inspector_means_no_check(self, scope: WorkspaceScope) -> None:
        FakeYoutubeDL.instances.clear()
        adapter = YtDlpDownloader(
            DownloadSettings(enabled=True, probe_attempts=1, progress_interval_seconds=0.0),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory_for(info=video_info(), script=download_script()),
        )

        result = await adapter.fetch(
            DownloadRequest(url=URL, selection=FormatSelection.best()), scope
        )

        assert result.primary.size_bytes == 4096
