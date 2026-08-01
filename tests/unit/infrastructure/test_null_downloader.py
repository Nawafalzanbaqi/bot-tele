"""The placeholder engine fails loudly, predictably, and to the same contract.

This is the executable version of the promise in the README: when no engine is
configured the system says so, rather than pretending or crashing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mediahub.application.common.errors import FeatureNotAvailableError
from mediahub.application.download.errors import DownloaderNotConfiguredError
from mediahub.application.download.ports import DownloadRequest
from mediahub.domain.download.enums import FailureKind
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.downloader.null_downloader import NullDownloader
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings

pytestmark = pytest.mark.unit

URL = "https://example.com/a.mp4"


def test_supports_nothing() -> None:
    assert NullDownloader().supports(URL) is False


def test_capabilities_report_no_capability() -> None:
    capabilities = NullDownloader().capabilities()

    assert capabilities.engine == "null"
    assert not capabilities.supports_probe
    assert not capabilities.supports_format_selection


async def test_probe_raises_a_typed_error() -> None:
    with pytest.raises(DownloaderNotConfiguredError) as excinfo:
        await NullDownloader().probe(URL)

    assert excinfo.value.code == "downloader_not_configured"
    assert URL in excinfo.value.message


async def test_fetch_raises_a_typed_error(tmp_path: Path) -> None:
    workspace = FilesystemWorkspace(tmp_path / "ws")

    with (
        workspace.lease(label="test") as scope,
        pytest.raises(DownloaderNotConfiguredError) as excinfo,
    ):
        await NullDownloader().fetch(DownloadRequest(url=URL), scope)

    assert excinfo.value.kind is FailureKind.POLICY


def test_it_is_reported_as_an_unavailable_feature() -> None:
    # The HTTP layer maps FeatureNotAvailableError to 501; inheriting from both
    # hierarchies is what keeps that mapping working without a special case.
    assert isinstance(DownloaderNotConfiguredError(URL), FeatureNotAvailableError)


def test_both_engines_satisfy_the_same_port() -> None:
    # A structural check: whatever the real engine exposes, the null one must
    # expose too, or callers cannot treat them interchangeably.
    real = YtDlpDownloader(DownloadSettings())
    null = NullDownloader()

    for member in ("name", "capabilities", "supports", "probe", "fetch"):
        assert hasattr(null, member), member
        assert hasattr(real, member), member
