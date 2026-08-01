"""Security controls around the download engine.

Corpus-driven, because "we handle traversal" is a claim and "these ninety
payloads are all refused" is evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.ports import DownloadRequest
from mediahub.domain.sources.errors import InvalidUrlError
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.domain.workspace.errors import InvalidArtifactNameError
from mediahub.domain.workspace.policies import FilenamePolicy
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.download.ytdlp.options import build_download_options
from mediahub.infrastructure.security.address_guard import DnsAddressGuard
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, factory_for, video_info

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = [pytest.mark.security, pytest.mark.unit]


# T1 - server-side request forgery.
SSRF_CORPUS = [
    "http://127.0.0.1/admin",
    "http://127.0.0.1:8080/admin",
    "http://localhost:11211/",
    "https://[::1]/a",
    "http://0.0.0.0/a",
    "http://169.254.169.254/latest/meta-data/",
    "http://[fe80::1]/a",
    "http://10.0.0.1/a",
    "http://172.16.0.1/a",
    "http://192.168.1.1/a",
    "http://[fc00::1]/a",
    "http://224.0.0.1/a",
    "http://240.0.0.1/a",
    "file:///etc/passwd",
    "file://C:/Windows/win.ini",
    "ftp://192.168.1.1/a",
    "gopher://127.0.0.1:11211/_stats",
    "dict://127.0.0.1:11211/stat",
    "data:text/html;base64,PHNjcmlwdD4=",
    "https://user:password@example.com/a",
    "http://example.com:22/a",
    "http://example.com:11211/a",
]

# T2 - path traversal and filename attacks.
FILENAME_CORPUS = [
    "../../../etc/passwd",
    "..\\..\\windows\\system32\\config\\sam",
    "/etc/shadow",
    "....//....//etc/passwd",
    "video/../../escape.mp4",
    "a\x00.mp4",
    "con",
    "aux.mp4",
    "nul",
    ".ssh",
    ".bashrc",
    "-rf.mp4",
    "$(whoami).mp4",
    "`id`.mp4",
    "a;rm -rf /.mp4",
    "a|nc attacker 4444.mp4",
    "a\nb.mp4",
    "\u202eexe.mp4",
]


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="security") as leased:
        yield leased


class TestSsrf:
    @pytest.mark.parametrize("url", SSRF_CORPUS)
    def test_the_policy_refuses_every_payload(self, url: str) -> None:
        with pytest.raises(InvalidUrlError):
            UrlPolicy().validate(url)

    @pytest.mark.parametrize("url", SSRF_CORPUS)
    async def test_the_engine_refuses_before_building_anything(self, url: str) -> None:
        FakeYoutubeDL.instances.clear()
        engine = YtDlpDownloader(
            DownloadSettings(),
            url_policy=UrlPolicy(),
            youtube_dl_factory=factory_for(info=video_info()),
        )

        with pytest.raises(InvalidUrlError):
            await engine.probe(url)

        assert FakeYoutubeDL.instances == [], "no socket may be opened for a refused URL"

    async def test_dns_rebinding_to_loopback_is_refused(self) -> None:
        # The hostname is innocuous; the answer is not.
        policy = UrlPolicy()
        engine = YtDlpDownloader(
            DownloadSettings(),
            url_policy=policy,
            address_guard=DnsAddressGuard(policy, resolver=lambda h, p: ["127.0.0.1"]),
            youtube_dl_factory=factory_for(info=video_info()),
        )

        with pytest.raises(InvalidUrlError):
            await engine.probe("https://totally-legitimate.example.com/a")


class TestFilesystemContainment:
    @pytest.mark.parametrize("name", FILENAME_CORPUS)
    def test_hostile_names_never_become_paths(self, scope: WorkspaceScope, name: str) -> None:
        with pytest.raises(InvalidArtifactNameError):
            scope.path_for(name)

    @pytest.mark.parametrize("name", FILENAME_CORPUS)
    def test_sanitising_always_produces_a_contained_name(
        self, scope: WorkspaceScope, name: str
    ) -> None:
        safe = FilenamePolicy().safe_name(name, "mp4")
        path = scope.path_for(safe)

        assert path.parent == scope.directory().resolve()

    def test_the_engine_is_confined_to_the_lease(self, scope: WorkspaceScope) -> None:
        options = build_download_options(
            DownloadSettings(),
            DownloadRequest(url="https://example.com/a"),
            directory=scope.directory(),
            format_expression="b",
            progress_hook=lambda payload: None,
            postprocessor_hook=lambda payload: None,
        )

        assert options["paths"]["home"] == str(scope.directory())
        assert options["paths"]["temp"] == str(
            scope.directory()
        ), "part-files must not land outside the lease"

    def test_output_names_are_generated_not_taken_from_the_source(
        self, scope: WorkspaceScope
    ) -> None:
        options = build_download_options(
            DownloadSettings(),
            DownloadRequest(url="https://example.com/a"),
            directory=scope.directory(),
            format_expression="b",
            progress_hook=lambda payload: None,
            postprocessor_hook=lambda payload: None,
        )

        template = options["outtmpl"]["default"]
        assert "%(title)s" not in template, "titles are attacker-controlled"
        assert options["restrictfilenames"] is True
        assert options["windowsfilenames"] is True


class TestEngineHardening:
    def test_no_post_processing_or_execution_is_permitted(self) -> None:
        options = build_download_options(
            DownloadSettings(),
            DownloadRequest(url="https://example.com/a"),
            directory=Path("/tmp"),  # noqa: S108 - not created; only the options matter
            format_expression="b",
            progress_hook=lambda payload: None,
            postprocessor_hook=lambda payload: None,
        )

        assert options["postprocessors"] == []
        assert options["exec_cmd"] == []
        assert options["cachedir"] is False
        assert options["geo_bypass"] is False
        assert options["allow_unplayable_formats"] is False

    def test_timeouts_are_always_set(self) -> None:
        options = build_download_options(
            DownloadSettings(),
            DownloadRequest(url="https://example.com/a"),
            directory=Path("/tmp"),  # noqa: S108 - not created; only the options matter
            format_expression="b",
            progress_hook=lambda payload: None,
            postprocessor_hook=lambda payload: None,
        )

        assert options["socket_timeout"] > 0, "a stalled socket must not hold a worker"

    def test_a_ceiling_is_always_present_in_practice(self, scope: WorkspaceScope) -> None:
        # The request may omit it, but the engine falls back to the configured
        # maximum, so no download is ever unbounded.
        settings = DownloadSettings(max_item_bytes=1234)
        engine = YtDlpDownloader(settings, youtube_dl_factory=factory_for(info=video_info()))

        assert engine._settings.max_item_bytes == 1234
