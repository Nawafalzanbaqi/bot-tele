"""The yt-dlp adapter, with the engine replaced at its narrowest seam.

Everything under test is the real adapter: real options, real hooks, real
classification, real verification and real cleanup. Only ``YoutubeDL`` itself is
a fake, so no test touches the network.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.common.cancellation import CancellationSource
from mediahub.application.download.errors import (
    ConnectionBlockedError,
    DownloadCancelledError,
    DownloadError,
    DownloadTimeoutError,
    LiveSourceNotAllowedError,
    MetadataUnavailableError,
    PlaylistNotAllowedError,
    ProviderError,
    SizeLimitExceededError,
    UnsupportedProviderError,
)
from mediahub.application.download.ports import (
    DownloadProgress,
    DownloadRequest,
    DownloadStage,
    FormatSelection,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.media.enums import MediaType
from mediahub.domain.sources.errors import BlockedAddressError, UnsupportedSchemeError
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.security.address_guard import DnsAddressGuard
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import (
    FakeYoutubeDL,
    download_script,
    factory_for,
    playlist_info,
    video_info,
    writes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit

URL = "https://example.com/watch?v=abc123"


@pytest.fixture(autouse=True)
def _reset_engine_registry() -> Iterator[None]:
    FakeYoutubeDL.instances.clear()
    yield
    FakeYoutubeDL.instances.clear()


@pytest.fixture
def settings() -> DownloadSettings:
    return DownloadSettings(
        enabled=True,
        probe_attempts=1,
        progress_interval_seconds=0.0,
        download_timeout_seconds=30.0,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> FilesystemWorkspace:
    return FilesystemWorkspace(tmp_path / "workspace")


@pytest.fixture
def scope(workspace: FilesystemWorkspace) -> Iterator[WorkspaceScope]:
    with workspace.lease(label="test") as leased:
        yield leased


def build(
    settings: DownloadSettings,
    *,
    info: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
    script: Sequence[Callable[[FakeYoutubeDL], None]] = (),
) -> YtDlpDownloader:
    """Build the adapter with a scripted engine and no DNS guard."""
    return YtDlpDownloader(
        settings,
        url_policy=UrlPolicy(),
        address_guard=None,
        youtube_dl_factory=factory_for(info=info, error=error, script=script),
    )


class TestEngineSurface:
    def test_reports_its_name_and_capabilities(self, settings: DownloadSettings) -> None:
        engine = build(settings)

        assert engine.name == "yt-dlp"
        capabilities = engine.capabilities()
        assert capabilities.engine == "yt-dlp"
        assert capabilities.supports_probe
        assert capabilities.supports_audio_only
        assert capabilities.requires_external_merger, "merging needs FFmpeg, which is not ours"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com/a", True),
            ("http://example.com/a", True),
            ("file:///etc/passwd", False),
            ("not a url", False),
            ("http://127.0.0.1/a", False),
        ],
    )
    def test_supports_is_a_cheap_policy_check(
        self, settings: DownloadSettings, url: str, expected: bool
    ) -> None:
        assert build(settings).supports(url) is expected

    def test_supports_never_builds_an_engine(self, settings: DownloadSettings) -> None:
        build(settings).supports("https://example.com/a")

        assert FakeYoutubeDL.instances == [], "supports() must not perform I/O"


class TestProbe:
    async def test_returns_mapped_metadata(self, settings: DownloadSettings) -> None:
        metadata = await build(settings, info=video_info()).probe(URL)

        assert metadata.provider == "testsite"
        assert metadata.kind is MediaType.VIDEO
        assert metadata.title == "A Test Video"
        assert metadata.available_qualities() == ("1080p", "360p")
        assert metadata.url == URL

    async def test_does_not_download(self, settings: DownloadSettings) -> None:
        await build(settings, info=video_info()).probe(URL)

        engine = FakeYoutubeDL.instances[0]
        assert engine.extract_calls == [(URL, False)]
        assert engine.options["skip_download"] is True
        assert engine.closed, "the engine must be released"

    async def test_an_empty_collection_is_described_not_refused(
        self, settings: DownloadSettings
    ) -> None:
        metadata = await build(settings, info=playlist_info(7)).probe(URL)

        assert metadata.is_playlist
        assert metadata.entry_count == 7
        assert metadata.from_playlist is False

    async def test_probe_options_resolve_a_video_link_to_its_video_not_its_list(
        self, settings: DownloadSettings
    ) -> None:
        """``watch?v=X&list=Y`` means X. And a flat list is read one entry deep."""
        await build(settings, info=video_info()).probe(URL)

        options = FakeYoutubeDL.instances[0].options
        assert options["noplaylist"] is True
        assert options["playlistend"] == 1
        assert options["extract_flat"] == "in_playlist"


class TestACollectionResolvesToItsFirstEntry:
    """A playlist link is a request for *a* video, and the first is the only defensible guess."""

    ENTRY = "https://example.com/watch?v=first111"

    def _engine(self, settings: DownloadSettings, *, entry_url: str, entry_info: Any) -> Any:
        """An engine that answers the collection URL with a flat list and the entry with a video."""
        collection = playlist_info(
            7, entries=[{"_type": "url", "url": entry_url, "id": "first111", "title": "First"}]
        )

        class ByUrl(FakeYoutubeDL):
            def extract_info(self, url: str, *, download: bool = True) -> Any:
                self.extract_calls.append((url, download))
                self.info = dict(entry_info) if url == entry_url else dict(collection)
                return self.info

        return YtDlpDownloader(
            settings,
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=ByUrl,
        )

    async def test_the_first_entry_is_probed_and_described(
        self, settings: DownloadSettings
    ) -> None:
        engine = self._engine(settings, entry_url=self.ENTRY, entry_info=video_info(id="first111"))

        metadata = await engine.probe(URL)

        assert metadata.is_playlist is False
        assert metadata.from_playlist is True
        assert metadata.entry_count == 7, "the size of the list it came from"
        assert metadata.url == self.ENTRY, "the acquisition must fetch the entry, not the list"
        assert metadata.title == "A Test Video"
        assert metadata.video_formats, "the entry was fully probed, not read flat"
        calls = [call for engine_ in FakeYoutubeDL.instances for call in engine_.extract_calls]
        assert [url for url, _ in calls] == [URL, self.ENTRY]
        assert all(download is False for _, download in calls)

    async def test_an_entry_that_may_not_be_fetched_leaves_the_collection_described(
        self, settings: DownloadSettings
    ) -> None:
        """The entry URL came from a third party's page and gets the scrutiny of typed input."""
        engine = self._engine(
            settings, entry_url="ftp://example.com/first", entry_info=video_info()
        )

        metadata = await engine.probe(URL)

        assert metadata.is_playlist is True
        assert metadata.from_playlist is False

    async def test_a_collection_of_collections_stops_at_one_level(
        self, settings: DownloadSettings
    ) -> None:
        engine = self._engine(settings, entry_url=self.ENTRY, entry_info=playlist_info(3))

        metadata = await engine.probe(URL)

        assert metadata.is_playlist is True
        assert metadata.entry_count == 7

    async def test_invalid_url_is_refused_before_any_engine_is_built(
        self, settings: DownloadSettings
    ) -> None:
        with pytest.raises(UnsupportedSchemeError):
            await build(settings, info=video_info()).probe("file:///etc/passwd")

        assert FakeYoutubeDL.instances == []

    async def test_empty_result_is_a_typed_error(self, settings: DownloadSettings) -> None:
        with pytest.raises(MetadataUnavailableError):
            await build(settings, info=None).probe(URL)

    async def test_engine_failure_is_classified(self, settings: DownloadSettings) -> None:
        engine = build(settings, error=Exception("ERROR: Unsupported URL: x"))

        with pytest.raises(UnsupportedProviderError):
            await engine.probe(URL)

    async def test_transient_failures_are_retried(self) -> None:
        settings = DownloadSettings(enabled=True, probe_attempts=3, probe_backoff_seconds=0.0)
        engine = build(settings, error=Exception("HTTP Error 503: Service Unavailable"))

        with pytest.raises(ProviderError):
            await engine.probe(URL)

        assert len(FakeYoutubeDL.instances) == 3

    async def test_permanent_failures_are_not_retried(self) -> None:
        settings = DownloadSettings(enabled=True, probe_attempts=3, probe_backoff_seconds=0.0)
        engine = build(settings, error=Exception("Video unavailable"))

        with pytest.raises(MetadataUnavailableError):
            await engine.probe(URL)

        assert len(FakeYoutubeDL.instances) == 1, "a permanent failure must not be retried"


class TestFetchHappyPath:
    async def test_produces_a_verified_primary_artifact(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(
            settings,
            info=video_info(requested_downloads=[{"format_id": "18", "ext": "mp4"}]),
            script=download_script(name="abc123.mp4", size_bytes=2048, chunks=(512, 1024)),
        )

        result = await engine.fetch(DownloadRequest(url=URL), scope)

        assert result.primary.name == "abc123.mp4"
        assert result.primary.role is ArtifactRole.PRIMARY
        assert result.total_bytes == 2048
        assert result.provider == "testsite"
        assert result.selected_format.format_id == "18"
        assert result.duration_seconds >= 0

    async def test_writes_only_inside_the_lease(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=download_script())

        await engine.fetch(DownloadRequest(url=URL), scope)

        options = FakeYoutubeDL.instances[0].options
        assert options["paths"]["home"] == str(scope.directory())
        assert options["paths"]["temp"] == str(scope.directory())
        assert options["cachedir"] is False, "yt-dlp must not write to ~/.cache"

    async def test_thumbnail_is_a_separate_artifact(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(
            settings,
            info=video_info(requested_downloads=[{"format_id": "18", "ext": "mp4"}]),
            script=download_script(extras=[("abc123.jpg", 128)]),
        )

        result = await engine.fetch(DownloadRequest(url=URL, include_thumbnail=True), scope)

        roles = {artifact.name: artifact.role for artifact in result.artifacts}
        assert roles["abc123.jpg"] is ArtifactRole.THUMBNAIL
        assert FakeYoutubeDL.instances[0].options["writethumbnail"] is True

    async def test_audio_only_selection_reaches_the_engine(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(
            settings,
            info=video_info(requested_downloads=[{"format_id": "140", "vcodec": "none"}]),
            script=download_script(name="abc123.m4a"),
        )

        result = await engine.fetch(
            DownloadRequest(url=URL, selection=FormatSelection.audio_only()), scope
        )

        assert FakeYoutubeDL.instances[0].options["format"].startswith("ba")
        assert result.selected_format.is_audio_only

    async def test_specific_quality_reaches_the_engine(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=download_script())

        await engine.fetch(
            DownloadRequest(url=URL, selection=FormatSelection.specific("137")), scope
        )

        assert FakeYoutubeDL.instances[0].options["format"].startswith("137/")

    async def test_byte_ceiling_is_passed_to_the_engine(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=download_script())

        await engine.fetch(DownloadRequest(url=URL, max_bytes=5_000_000), scope)

        assert FakeYoutubeDL.instances[0].options["max_filesize"] == 5_000_000


class TestProgress:
    async def test_reports_stages_and_bytes(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        seen: list[DownloadProgress] = []
        engine = build(
            settings,
            info=video_info(),
            script=download_script(size_bytes=2048, chunks=(512, 1024, 2048)),
        )

        await engine.fetch(DownloadRequest(url=URL), scope, on_progress=seen.append)

        stages = [update.stage for update in seen]
        assert stages[0] is DownloadStage.VALIDATING
        assert DownloadStage.DOWNLOADING in stages
        assert stages[-1] is DownloadStage.COMPLETED

        downloading = [u for u in seen if u.stage is DownloadStage.DOWNLOADING]
        assert downloading[-1].downloaded_bytes == 2048
        assert downloading[-1].percentage == 100.0

        # Speed and ETA come from the engine's in-flight ticks; its final
        # "finished" payload carries neither, which is why they are optional.
        in_flight = [u for u in downloading if u.speed_bps is not None]
        assert in_flight, "at least one in-flight update should report speed"
        assert in_flight[-1].speed_bps == 1024.0
        assert in_flight[-1].eta_seconds == 3.0

    async def test_progress_never_leaks_a_filesystem_path(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        seen: list[DownloadProgress] = []
        engine = build(settings, info=video_info(), script=download_script(chunks=(1,)))

        await engine.fetch(DownloadRequest(url=URL), scope, on_progress=seen.append)

        names = [update.filename for update in seen if update.filename]
        assert names, "at least one update should name the file"
        assert all("/" not in name and "\\" not in name for name in names)

    async def test_a_missing_callback_is_fine(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=download_script(chunks=(1, 2)))

        result = await engine.fetch(DownloadRequest(url=URL), scope)

        assert result.total_bytes > 0


class TestCancellation:
    async def test_cancelled_download_raises_and_cleans_up(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        source = CancellationSource()

        def cancel_midway(engine: FakeYoutubeDL) -> None:
            engine.write_file("abc123.mp4.part", 512)
            source.cancel()
            engine.progress({"status": "downloading", "downloaded_bytes": 512, "total_bytes": 4096})

        engine = build(settings, info=video_info(), script=[cancel_midway])

        with pytest.raises(DownloadCancelledError):
            await engine.fetch(DownloadRequest(url=URL), scope, cancellation=source.token)

        assert scope.names() == (), "a cancelled attempt must leave no partial files"

    async def test_cancellation_before_the_first_chunk_is_honoured(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        source = CancellationSource()
        source.cancel()
        engine = build(settings, info=video_info(), script=download_script())

        with pytest.raises(DownloadCancelledError):
            await engine.fetch(DownloadRequest(url=URL), scope, cancellation=source.token)

    async def test_pre_existing_files_survive_a_failure(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        scope.path_for("keepme.txt").write_bytes(b"important")
        source = CancellationSource()
        source.cancel()
        engine = build(settings, info=video_info(), script=download_script())

        with pytest.raises(DownloadCancelledError):
            await engine.fetch(DownloadRequest(url=URL), scope, cancellation=source.token)

        assert scope.names() == ("keepme.txt",)


class TestGuards:
    async def test_size_ceiling_is_enforced_while_streaming(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        def overshoot(engine: FakeYoutubeDL) -> None:
            engine.write_file("abc123.mp4.part", 10)
            engine.progress(
                {"status": "downloading", "downloaded_bytes": 5_000, "total_bytes": None}
            )

        engine = build(settings, info=video_info(), script=[overshoot])

        with pytest.raises(SizeLimitExceededError) as excinfo:
            await engine.fetch(DownloadRequest(url=URL, max_bytes=1_000), scope)

        assert excinfo.value.limit_bytes == 1_000
        assert excinfo.value.observed_bytes == 5_000
        assert scope.names() == ()

    async def test_timeout_produces_a_typed_error(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        def slow(engine: FakeYoutubeDL) -> None:
            del engine
            time.sleep(0.2)

        engine = build(settings, info=video_info(), script=[slow])

        with pytest.raises(DownloadTimeoutError):
            await engine.fetch(DownloadRequest(url=URL, timeout_seconds=0.05), scope)

    async def test_live_sources_are_refused_by_default(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(is_live=True), script=download_script())

        with pytest.raises(LiveSourceNotAllowedError):
            await engine.fetch(DownloadRequest(url=URL), scope)

        assert scope.names() == ()

    async def test_live_sources_can_be_allowed(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(is_live=True), script=download_script())

        result = await engine.fetch(DownloadRequest(url=URL, allow_live=True), scope)

        assert result.total_bytes > 0

    async def test_playlists_are_refused_by_default(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=playlist_info(500), script=download_script())

        with pytest.raises(PlaylistNotAllowedError) as excinfo:
            await engine.fetch(DownloadRequest(url=URL), scope)

        assert excinfo.value.entry_count == 500

    async def test_playlist_expansion_is_disabled_in_options(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=download_script())

        await engine.fetch(DownloadRequest(url=URL), scope)

        options = FakeYoutubeDL.instances[0].options
        assert options["noplaylist"] is True
        assert options["playlist_items"] == "1"


class TestVerification:
    async def test_a_download_that_produced_nothing_fails(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=())

        with pytest.raises(MetadataUnavailableError):
            await engine.fetch(DownloadRequest(url=URL), scope)

    async def test_an_empty_file_fails_verification(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=writes("abc123.mp4", 0))

        with pytest.raises(MetadataUnavailableError):
            await engine.fetch(DownloadRequest(url=URL), scope)

        assert scope.names() == ()

    async def test_only_partial_files_fails(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        engine = build(settings, info=video_info(), script=writes("abc123.mp4.part", 64))

        with pytest.raises(MetadataUnavailableError):
            await engine.fetch(DownloadRequest(url=URL), scope)

    async def test_engine_failure_cleans_up_partial_output(
        self, settings: DownloadSettings, scope: WorkspaceScope
    ) -> None:
        def fail_after_writing(engine: FakeYoutubeDL) -> None:
            engine.write_file("abc123.mp4.part", 128)
            message = "HTTP Error 503: Service Unavailable"
            raise RuntimeError(message)

        engine = build(settings, info=video_info(), script=[fail_after_writing])

        with pytest.raises(ProviderError):
            await engine.fetch(DownloadRequest(url=URL), scope)

        assert scope.names() == ()


class TestAddressGuard:
    async def test_a_host_resolving_to_loopback_is_refused(
        self, settings: DownloadSettings
    ) -> None:
        policy = UrlPolicy()
        guard = DnsAddressGuard(policy, resolver=lambda host, port: ["127.0.0.1"])
        engine = YtDlpDownloader(
            settings,
            url_policy=policy,
            address_guard=guard,
            youtube_dl_factory=factory_for(info=video_info()),
        )

        with pytest.raises(BlockedAddressError):
            await engine.probe("https://sneaky.example.com/a")

        assert FakeYoutubeDL.instances == []


class TestConcurrency:
    async def test_two_downloads_do_not_share_a_lease(
        self, settings: DownloadSettings, workspace: FilesystemWorkspace
    ) -> None:
        engine = build(settings, info=video_info(), script=download_script())

        async def run() -> int:
            with workspace.lease(label="job") as leased:
                result = await engine.fetch(DownloadRequest(url=URL), leased)
                return result.total_bytes

        first, second = await asyncio.gather(run(), run())

        assert first == second > 0
        homes = {engine.options["paths"]["home"] for engine in FakeYoutubeDL.instances}
        assert len(homes) == 2, "each download must get its own directory"


class TestEgressIsUsedOnlyWhereItIsNeeded:
    """A tunnel is slower and usually metered; most sources do not need one.

    So nothing is routed through it by default. A host earns the egress by
    proving it needs one - a connection that opened and was reset - and is then
    remembered, so the cost of learning is a single fast failure paid once.
    """

    @staticmethod
    def _settings(**overrides: object) -> DownloadSettings:
        base: dict[str, object] = {
            "enabled": True,
            "probe_attempts": 1,
            "progress_interval_seconds": 0.0,
            "proxy": "http://vpn:8888",
        }
        base.update(overrides)
        return DownloadSettings(**base)  # type: ignore[arg-type]

    async def test_an_ordinary_source_is_fetched_directly(self) -> None:
        downloader = build(self._settings(), info=video_info())

        await downloader.probe(URL)

        assert FakeYoutubeDL.instances[0].options.get("proxy") is None, (
            "a source that works must not be sent down the tunnel"
        )

    async def test_a_listed_host_goes_through_the_egress_immediately(self) -> None:
        """No wasted first attempt for a host already known to need it."""
        downloader = build(self._settings(proxy_hosts=("example.com",)), info=video_info())

        await downloader.probe(URL)

        assert FakeYoutubeDL.instances[0].options.get("proxy") == "http://vpn:8888"
        assert len(FakeYoutubeDL.instances) == 1, "it must not try direct first"

    async def test_a_subdomain_of_a_listed_host_is_covered(self) -> None:
        downloader = build(self._settings(proxy_hosts=("example.com",)), info=video_info())

        await downloader.probe("https://cdn.example.com/watch?v=abc123")

        assert FakeYoutubeDL.instances[0].options.get("proxy") == "http://vpn:8888"

    async def test_a_reset_connection_is_retried_through_the_egress(self) -> None:
        """The whole point: the engine discovers what needs routing."""
        reset = OSError("Unable to download webpage: [Errno 104] Connection reset by peer")
        attempts: list[Mapping[str, Any]] = []

        def factory(options: Mapping[str, Any]) -> FakeYoutubeDL:
            attempts.append(options)
            failing = options.get("proxy") is None
            return FakeYoutubeDL(
                options, info=None if failing else video_info(), error=reset if failing else None
            )

        downloader = YtDlpDownloader(
            self._settings(),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory,
        )

        metadata = await downloader.probe(URL)

        assert metadata.title == "A Test Video"
        assert [options.get("proxy") for options in attempts] == [None, "http://vpn:8888"]

    async def test_the_lesson_is_remembered_for_the_next_request(self) -> None:
        """Learning must cost one failure, not one per download."""
        reset = OSError("[Errno 104] Connection reset by peer")
        attempts: list[Mapping[str, Any]] = []

        def factory(options: Mapping[str, Any]) -> FakeYoutubeDL:
            attempts.append(options)
            failing = options.get("proxy") is None
            return FakeYoutubeDL(
                options, info=None if failing else video_info(), error=reset if failing else None
            )

        downloader = YtDlpDownloader(
            self._settings(),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory,
        )
        await downloader.probe(URL)
        attempts.clear()

        await downloader.probe(URL)

        assert [options.get("proxy") for options in attempts] == ["http://vpn:8888"]

    async def test_an_ordinary_failure_is_not_retried_through_the_egress(self) -> None:
        """A private video fails identically through a tunnel; do not pay twice."""
        downloader = build(self._settings(), error=RuntimeError("This video is private"))

        with pytest.raises(MetadataUnavailableError):
            await downloader.probe(URL)

        assert len(FakeYoutubeDL.instances) == 1

    async def test_without_an_egress_a_reset_is_simply_reported(self) -> None:
        downloader = build(
            DownloadSettings(enabled=True, probe_attempts=1),
            error=OSError("[Errno 104] Connection reset by peer"),
        )

        with pytest.raises(ConnectionBlockedError):
            await downloader.probe(URL)


class TestADownloadSurvivesAFlakyExtractor:
    """A probe succeeding does not mean the download will.

    Extraction runs again when the download starts, and some extractors fail a
    measurable share of the time for no visible reason: TikTok's web path was
    measured at 6 successes in 8 from the device this runs on. Without a retry
    here a quarter of perfectly good links failed outright, which from the
    outside is indistinguishable from "this link is broken".
    """

    @staticmethod
    def _settings(**overrides: object) -> DownloadSettings:
        base: dict[str, object] = {
            "enabled": True,
            "probe_attempts": 1,
            "probe_backoff_seconds": 0.0,
            "progress_interval_seconds": 0.0,
        }
        base.update(overrides)
        return DownloadSettings(**base)  # type: ignore[arg-type]

    def _flaky_factory(self, failures: int) -> tuple[Callable[..., FakeYoutubeDL], list[int]]:
        """Return a factory that fails the first ``failures`` calls."""
        calls: list[int] = []

        def factory(options: Mapping[str, Any]) -> FakeYoutubeDL:
            calls.append(1)
            failing = len(calls) <= failures
            return FakeYoutubeDL(
                options,
                info=None if failing else video_info(),
                error=RuntimeError("Unexpected response from webpage request") if failing else None,
                script=() if failing else writes("abc123.mp4", 2048),
            )

        return factory, calls

    async def test_a_transient_extraction_failure_is_retried(self, scope: WorkspaceScope) -> None:
        factory, calls = self._flaky_factory(failures=2)
        downloader = YtDlpDownloader(
            self._settings(download_attempts=3),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory,
        )

        result = await downloader.fetch(
            DownloadRequest(url=URL, selection=FormatSelection.best()), scope
        )

        assert len(calls) == 3, "it must keep trying while attempts remain"
        assert result.primary.size_bytes == 2048

    async def test_it_gives_up_after_the_configured_attempts(self, scope: WorkspaceScope) -> None:
        factory, calls = self._flaky_factory(failures=99)
        downloader = YtDlpDownloader(
            self._settings(download_attempts=2),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory,
        )

        with pytest.raises(DownloadError):
            await downloader.fetch(
                DownloadRequest(url=URL, selection=FormatSelection.best()), scope
            )

        assert len(calls) == 2

    async def test_a_permanent_failure_is_not_retried(self, scope: WorkspaceScope) -> None:
        """A private video fails identically on the second attempt."""
        downloader = build(
            self._settings(download_attempts=3), error=RuntimeError("This video is private")
        )

        with pytest.raises(MetadataUnavailableError):
            await downloader.fetch(
                DownloadRequest(url=URL, selection=FormatSelection.best()), scope
            )

        assert len(FakeYoutubeDL.instances) == 1, "retrying a refusal only delays the answer"
