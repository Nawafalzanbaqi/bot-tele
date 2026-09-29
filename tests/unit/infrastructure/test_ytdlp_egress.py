"""Which path a request takes, and what it tells the caller about it.

Direct first, the egress proxy only for failures that name the exit address as
the problem, a browser fingerprint wherever a bot wall is likely - and the result
says which path actually worked.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    ContentRemovedError,
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
    from collections.abc import Callable, Iterator, Mapping, Sequence
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
    error: BaseException, *, script: Sequence[Callable[[FakeYoutubeDL], None]] = ()
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
            ("The video has been deleted", ContentRemovedError),
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


class TestTheLessonIsKept:
    """A host that needed the egress yesterday goes through it today, first try."""

    async def test_an_escalated_host_is_written_to_the_file(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        factory, attempts = failing_directly(RuntimeError("[Errno 104] Connection reset by peer"))

        await downloader(settings(egress_hosts_file=hosts_file), factory).probe(URL)

        assert [options.get("proxy") for options in attempts] == [None, PROXY]
        assert hosts_file.exists()
        assert "example.com" in _hosts_in(hosts_file)
        assert hosts_file.read_text(encoding="utf-8").startswith("# Hosts the download engine")

    async def test_the_next_process_goes_through_the_egress_first(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("example.com\n", encoding="utf-8")
        factory, attempts = failing_directly(RuntimeError("would have been reset"))

        metadata = await downloader(settings(egress_hosts_file=hosts_file), factory).probe(URL)

        assert metadata.title == "A Test Video"
        assert [options.get("proxy") for options in attempts] == [PROXY], "no direct attempt"

    def test_a_hand_edit_is_picked_up_without_a_restart(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("a.example\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=hosts_file)
        assert policy.for_host("a.example") == PROXY
        assert policy.for_host("b.example") is None

        _rewrite(hosts_file, "# a.example removed on purpose\n.B.EXAMPLE  # added\n\n")

        assert policy.for_host("a.example") is None, "the removed line lets it go direct again"
        assert policy.for_host("b.example") == PROXY, "case, dots and comments are tolerated"
        assert policy.for_host("cdn.b.example") == PROXY, "subdomains are covered"

    def test_deleting_the_file_forgets_everything(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("a.example\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=hosts_file)
        assert policy.for_host("a.example") == PROXY

        hosts_file.unlink()

        assert policy.for_host("a.example") is None
        assert policy.learned_hosts == ()

    def test_configured_and_learned_hosts_are_both_listed(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("learned.example\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, ["Configured.example"], learned_file=hosts_file)

        assert policy.routed() == ("configured.example", "learned.example")

    def test_pin_routes_a_host_and_says_whether_it_was_new(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        policy = ProxyPolicy(PROXY, ["configured.example"], learned_file=hosts_file)

        assert policy.pin("New.Example") is True
        assert policy.pin("new.example") is False, "already routed"
        assert policy.pin("cdn.configured.example") is False, "covered by a configured parent"
        assert policy.for_host("new.example") == PROXY
        assert "new.example" in _hosts_in(hosts_file)

    def test_pin_without_an_egress_does_nothing(self) -> None:
        policy = ProxyPolicy(None)

        assert policy.pin("a.example") is False
        assert policy.routed() == ()

    def test_an_unwritable_file_keeps_the_lesson_for_this_process(self, tmp_path: Path) -> None:
        """A read-only volume must not turn a successful escalation into a crash."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=blocker / "egress-hosts.txt")

        assert policy.escalate("a.example") == PROXY
        assert policy.for_host("a.example") == PROXY

    def test_without_a_file_nothing_is_written(self, tmp_path: Path) -> None:
        policy = ProxyPolicy(PROXY)

        assert policy.escalate("a.example") == PROXY
        assert list(tmp_path.iterdir()) == []


def _hosts_in(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _rewrite(path: Path, text: str) -> None:
    """Rewrite a file with a modification time that is certainly different."""
    previous = path.stat().st_mtime_ns
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(previous + 2_000_000_000, previous + 2_000_000_000))


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
