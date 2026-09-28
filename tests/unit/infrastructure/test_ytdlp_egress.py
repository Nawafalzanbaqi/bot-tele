"""Which path a request takes, and what it tells the caller about it.

Direct first, the egress proxy only for failures that name the exit address as
the problem, a browser fingerprint wherever a bot wall is likely - and the result
says which path actually worked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    MetadataUnavailableError,
)
from mediahub.application.download.ports import DownloadRequest, FormatSelection
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import ProxyPolicy, YtDlpDownloader
from mediahub.infrastructure.download.ytdlp.options import base_options
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, video_info, writes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit

URL = "https://example.com/watch?v=abc123"
PROXY = "http://vpn:8888"


@pytest.fixture(autouse=True)
def _reset_engine_registry() -> Iterator[None]:
    FakeYoutubeDL.instances.clear()
    yield
    FakeYoutubeDL.instances.clear()


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    with FilesystemWorkspace(tmp_path / "workspace").lease(label="test") as leased:
        yield leased


def settings(**overrides: object) -> DownloadSettings:
    base: dict[str, object] = {
        "enabled": True,
        "probe_attempts": 1,
        "probe_backoff_seconds": 0.0,
        "download_attempts": 1,
        "progress_interval_seconds": 0.0,
        "proxy": PROXY,
    }
    base.update(overrides)
    return DownloadSettings(**base)  # type: ignore[arg-type]


def failing_directly(
    error: BaseException, *, script: tuple[Callable[[FakeYoutubeDL], None], ...] = ()
) -> tuple[Callable[[Mapping[str, Any]], FakeYoutubeDL], list[Mapping[str, Any]]]:
    """Return a factory whose engine fails on the direct path and succeeds via the proxy."""
    attempts: list[Mapping[str, Any]] = []

    def factory(options: Mapping[str, Any]) -> FakeYoutubeDL:
        attempts.append(options)
        direct = options.get("proxy") is None
        return FakeYoutubeDL(
            options,
            info=None if direct else video_info(),
            error=error if direct else None,
            script=() if direct else script,
        )

    return factory, attempts


def downloader(config: DownloadSettings, factory: Callable[..., FakeYoutubeDL]) -> YtDlpDownloader:
    return YtDlpDownloader(
        config, url_policy=UrlPolicy(), address_guard=None, youtube_dl_factory=factory
    )


class TestWhatEscalates:
    @pytest.mark.parametrize(
        "message",
        [
            "HTTP Error 403: Forbidden",
            "Unable to download webpage: HTTP Error 403: Forbidden",
            "Your IP address is blocked from accessing this post",
            "Unable to extract universal data for rehydration",
            "The uploader has not made this video available in your country",
            "[Errno 104] Connection reset by peer",
        ],
        ids=["403", "403-webpage", "ip-blocked", "bot-wall", "geo", "reset"],
    )
    async def test_an_address_level_refusal_is_retried_through_the_egress(
        self, message: str
    ) -> None:
        factory, attempts = failing_directly(RuntimeError(message))

        metadata = await downloader(settings(), factory).probe(URL)

        assert metadata.title == "A Test Video"
        assert [options.get("proxy") for options in attempts] == [None, PROXY]

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("This video is private", MetadataUnavailableError),
            ("Sign in to confirm your age", AuthenticationRequiredError),
            ("The video has been deleted", MetadataUnavailableError),
        ],
        ids=["private", "age-gate", "deleted"],
    )
    async def test_a_content_level_refusal_is_final(
        self, message: str, expected: type[Exception]
    ) -> None:
        """These fail identically through a tunnel; paying twice buys nothing."""
        factory, attempts = failing_directly(RuntimeError(message))

        with pytest.raises(expected):
            await downloader(settings(), factory).probe(URL)

        assert [options.get("proxy") for options in attempts] == [None]


class TestImpersonation:
    async def test_the_proxied_attempt_presents_a_browser(self) -> None:
        factory, attempts = failing_directly(RuntimeError("HTTP Error 403: Forbidden"))

        await downloader(settings(), factory).probe(URL)

        direct, proxied = attempts
        assert direct.get("impersonate") is None, "an ordinary host is fetched as ourselves"
        assert proxied.get("impersonate") == "chrome"

    async def test_hosts_that_fingerprint_the_handshake_get_it_directly(self) -> None:
        attempts: list[Mapping[str, Any]] = []

        def succeeding(options: Mapping[str, Any]) -> FakeYoutubeDL:
            attempts.append(options)
            return FakeYoutubeDL(options, info=video_info())

        config = settings(impersonate_hosts=("example.com",))
        await downloader(config, succeeding).probe("https://www.example.com/watch?v=1")

        assert attempts[-1].get("proxy") is None, "listed for impersonation, not for routing"
        assert attempts[-1].get("impersonate") == "chrome"

    async def test_impersonation_can_be_switched_off(self) -> None:
        factory, attempts = failing_directly(RuntimeError("HTTP Error 403: Forbidden"))

        await downloader(settings(impersonate=None), factory).probe(URL)

        assert all(options.get("impersonate") is None for options in attempts)

    def test_the_option_is_a_plain_string_for_the_factory_to_convert(self) -> None:
        options = base_options(settings(), impersonate="chrome")

        assert options["impersonate"] == "chrome"
        assert "impersonate" not in base_options(settings())

    def test_the_policy_reads_host_lists_case_insensitively(self) -> None:
        policy = ProxyPolicy(
            PROXY, ("Example.com",), impersonate="chrome", impersonate_hosts=(".TikTok.com",)
        )

        assert policy.for_host("cdn.example.com") == PROXY
        assert policy.impersonation_for("www.tiktok.com", proxied=False) == "chrome"
        assert policy.impersonation_for("example.org", proxied=False) is None
        assert policy.impersonation_for("example.org", proxied=True) == "chrome"


class TestTheResultNamesThePath:
    async def test_a_direct_fetch_says_so(self, scope: WorkspaceScope) -> None:
        def factory(options: Mapping[str, Any]) -> FakeYoutubeDL:
            return FakeYoutubeDL(options, info=video_info(), script=writes("abc123.mp4", 2048))

        result = await downloader(settings(), factory).fetch(
            DownloadRequest(url=URL, selection=FormatSelection.best()), scope
        )

        assert result.via_proxy is False

    async def test_an_escalated_fetch_says_so(self, scope: WorkspaceScope) -> None:
        factory, attempts = failing_directly(
            RuntimeError("HTTP Error 403: Forbidden"), script=writes("abc123.mp4", 2048)
        )

        result = await downloader(settings(), factory).fetch(
            DownloadRequest(url=URL, selection=FormatSelection.best()), scope
        )

        assert result.via_proxy is True
        assert [options.get("proxy") for options in attempts] == [None, PROXY]
