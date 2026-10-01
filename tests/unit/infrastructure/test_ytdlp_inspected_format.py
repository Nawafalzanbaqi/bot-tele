"""What ffprobe saw fills in what the extractor left unknown about the file taken."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.ports import DownloadRequest
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.download.ytdlp.inspection import StreamReport
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, download_script, factory_for, video_info

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

URL = "https://example.com/watch?v=abc123"


class ScriptedInspector:
    def __init__(self, answer: StreamReport | None) -> None:
        self.answer = answer

    async def inspect(self, path: Path) -> StreamReport | None:
        return self.answer


@pytest.fixture(autouse=True)
def _reset_engine_registry() -> Iterator[None]:
    FakeYoutubeDL.instances.clear()
    yield
    FakeYoutubeDL.instances.clear()


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    with FilesystemWorkspace(tmp_path / "ws").lease(label="t") as leased:
        yield leased


def engine(taken: dict[str, object], inspector: ScriptedInspector) -> YtDlpDownloader:
    return YtDlpDownloader(
        DownloadSettings(enabled=True, probe_attempts=1, progress_interval_seconds=0.0),
        url_policy=UrlPolicy(),
        address_guard=None,
        youtube_dl_factory=factory_for(
            info=video_info(requested_downloads=[taken]), script=download_script()
        ),
        inspector=inspector,
    )


def seen(codec: str, width: int, height: int) -> StreamReport:
    return StreamReport(
        duration_seconds=125.0,
        video_codecs=(codec,),
        audio_codecs=("aac",),
        width=width,
        height=height,
    )


class TestInspectionFillsTheGaps:
    async def test_frame_and_codec_come_from_the_file_when_the_extractor_had_none(
        self, scope: WorkspaceScope
    ) -> None:
        """Instagram's progressive files and every Threads file: no size, no codec declared."""
        result = await engine(
            {"format_id": "1", "ext": "mp4"}, ScriptedInspector(seen("h264", 720, 1280))
        ).fetch(DownloadRequest(url=URL), scope)

        taken = result.selected_format
        assert (taken.width, taken.height) == (720, 1280)
        assert taken.video_codec == "avc1", "in the vocabulary the quality policy reads"

    async def test_what_the_extractor_declared_is_kept(self, scope: WorkspaceScope) -> None:
        declared = {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1.64",
            "width": 1920,
            "height": 1080,
        }

        result = await engine(declared, ScriptedInspector(seen("vp9", 1, 1))).fetch(
            DownloadRequest(url=URL), scope
        )

        taken = result.selected_format
        assert (taken.width, taken.height, taken.video_codec) == (1920, 1080, "avc1.64")

    async def test_without_an_inspection_nothing_changes(self, scope: WorkspaceScope) -> None:
        result = await engine({"format_id": "1", "ext": "mp4"}, ScriptedInspector(None)).fetch(
            DownloadRequest(url=URL), scope
        )

        assert result.selected_format.width is None
