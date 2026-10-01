"""The cookie jar goes back to owner-only after the engine has rewritten it."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, factory_for, video_info

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

URL = "https://example.com/watch?v=abc123"


@pytest.fixture(autouse=True)
def _reset_engine_registry() -> Iterator[None]:
    FakeYoutubeDL.instances.clear()
    yield
    FakeYoutubeDL.instances.clear()


class TestCookieJarMode:
    async def test_a_probe_leaves_the_jar_owner_only(self, tmp_path: Path) -> None:
        """yt-dlp rewrites the jar on close through the umask; 0644 was the result."""
        jar = tmp_path / "cookies.txt"
        jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        jar.chmod(0o644)
        settings = DownloadSettings(
            enabled=True, probe_attempts=1, probe_backoff_seconds=0.0, cookies_file=jar
        )
        engine = YtDlpDownloader(
            settings,
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory_for(info=video_info()),
        )

        await engine.probe(URL)

        assert stat.S_IMODE(jar.stat().st_mode) == 0o600

    async def test_without_a_jar_nothing_is_touched(self, tmp_path: Path) -> None:
        settings = DownloadSettings(
            enabled=True, probe_attempts=1, probe_backoff_seconds=0.0
        )
        engine = YtDlpDownloader(
            settings,
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory_for(info=video_info()),
        )

        metadata = await engine.probe(URL)

        assert metadata.title == "A Test Video"
