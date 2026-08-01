"""The download engine end to end, through the real workspace.

These exercise the whole flow a caller performs - probe, choose, download,
progress, verify, clean up - with a real filesystem and the real adapter. Only
the network is replaced.

The last test in this module is the one that talks to the real internet. It is
skipped unless ``MEDIAHUB_NETWORK_TESTS=1``, because a test suite that depends
on a third-party site is a test suite that fails for reasons unrelated to the
code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mediahub.application.common.cancellation import CancellationSource
from mediahub.application.download.errors import DownloadCancelledError
from mediahub.application.download.ports import (
    DownloadProgress,
    DownloadRequest,
    DownloadStage,
    FormatSelection,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, download_script, factory_for, video_info

if TYPE_CHECKING:
    from mediahub.application.download.ports import DownloaderPort

pytestmark = pytest.mark.integration

URL = "https://example.com/watch?v=abc123"
NETWORK_TESTS_ENABLED = os.getenv("MEDIAHUB_NETWORK_TESTS") == "1"


@pytest.fixture
def workspace(tmp_path: Path) -> FilesystemWorkspace:
    return FilesystemWorkspace(tmp_path / "workspace")


@pytest.fixture
def engine() -> YtDlpDownloader:
    return YtDlpDownloader(
        DownloadSettings(enabled=True, progress_interval_seconds=0.0, probe_attempts=1),
        url_policy=UrlPolicy(),
        address_guard=None,
        youtube_dl_factory=factory_for(
            info=video_info(requested_downloads=[{"format_id": "18", "ext": "mp4"}]),
            script=download_script(
                name="abc123.mp4",
                size_bytes=8192,
                chunks=(2048, 4096, 8192),
                extras=[("abc123.jpg", 256)],
            ),
        ),
    )


async def test_a_caller_can_complete_the_whole_flow(
    engine: YtDlpDownloader, workspace: FilesystemWorkspace
) -> None:
    """The success criteria from the phase brief, as one test."""
    # 1. Probe a URL, and 2. receive metadata.
    metadata = await engine.probe(URL)
    assert metadata.title == "A Test Video"
    assert metadata.provider == "testsite"

    # 3. Choose a format from what the probe reported.
    qualities = metadata.available_qualities()
    assert qualities == ("1080p", "360p")
    selection = FormatSelection.up_to_height(720)

    updates: list[DownloadProgress] = []

    with workspace.lease(label="acquire", reserve_bytes=metadata.expected_bytes) as scope:
        # 4. Download, and 5. receive progress.
        result = await engine.fetch(
            DownloadRequest(url=URL, selection=selection, include_thumbnail=True),
            scope,
            on_progress=updates.append,
        )

        # 7. Receive a typed result.
        assert result.primary.name == "abc123.mp4"
        assert result.primary.size_bytes == 8192
        assert result.total_bytes == 8192 + 256
        assert result.selected_format.format_id == "18"
        assert {artifact.role for artifact in result.artifacts} == {
            ArtifactRole.PRIMARY,
            ArtifactRole.THUMBNAIL,
        }

        # The files really are on disk, inside the lease.
        primary_path = scope.path_for(result.primary.name)
        assert primary_path.is_file()
        assert primary_path.parent == scope.directory().resolve()

        leased_directory = scope.directory()

    # The lease is gone: nothing is kept after the work finishes.
    assert not leased_directory.exists()

    stages = [update.stage for update in updates]
    assert stages[0] is DownloadStage.VALIDATING
    assert stages[-1] is DownloadStage.COMPLETED
    assert max(update.downloaded_bytes for update in updates) == 8192


async def test_cancellation_leaves_nothing_behind(
    workspace: FilesystemWorkspace,
) -> None:
    """6. Cancel safely."""
    source = CancellationSource()

    def cancel_after_first_chunk(fake: FakeYoutubeDL) -> None:
        fake.write_file("abc123.mp4.part", 1024)
        source.cancel()
        fake.progress({"status": "downloading", "downloaded_bytes": 1024, "total_bytes": 8192})

    engine = YtDlpDownloader(
        DownloadSettings(enabled=True),
        youtube_dl_factory=factory_for(info=video_info(), script=[cancel_after_first_chunk]),
    )

    with workspace.lease(label="cancelled") as scope:
        with pytest.raises(DownloadCancelledError):
            await engine.fetch(DownloadRequest(url=URL), scope, cancellation=source.token)

        assert scope.names() == ()
        directory = scope.directory()

    assert not directory.exists()


async def test_the_engine_is_reachable_through_the_port_alone(
    engine: YtDlpDownloader, workspace: FilesystemWorkspace
) -> None:
    """Any future interface can drive it knowing only the port."""
    port: DownloaderPort = engine

    assert port.supports(URL)
    assert port.capabilities().supports_probe

    with workspace.lease(label="via-port") as scope:
        result = await port.fetch(DownloadRequest(url=URL), scope)

    assert result.total_bytes > 0


@pytest.mark.network
@pytest.mark.skipif(
    not NETWORK_TESTS_ENABLED,
    reason="set MEDIAHUB_NETWORK_TESTS=1 to run tests that reach the internet",
)
async def test_probes_a_real_source(workspace: FilesystemWorkspace) -> None:
    """A real probe against a real site, for manual verification only.

    Deliberately probe-only: downloading real media in a test would be slow,
    large, and legally ambiguous.
    """
    del workspace
    engine = YtDlpDownloader(DownloadSettings(enabled=True, probe_attempts=1))

    metadata = await engine.probe("https://www.youtube.com/watch?v=aqz-KE-bpKQ")

    assert metadata.provider
    assert metadata.title
    assert metadata.video_formats
