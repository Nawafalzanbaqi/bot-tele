"""Which path a request takes, and what it tells the caller about it.

Direct first, the egress proxy only for failures that name the exit address as
the problem, a browser fingerprint wherever a bot wall is likely - and the result
says which path actually worked.
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    ContentRemovedError,
    GeoRestrictedError,
    MetadataUnavailableError,
    ProviderError,
    SiteChallengeError,
)
from mediahub.application.download.ports import DownloadRequest, FormatSelection
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import ProxyPolicy, YtDlpDownloader
from mediahub.infrastructure.download.ytdlp.options import base_options
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, video_info, writes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
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
            "PhantomJS not found, please install it",
        ],
        ids=["403", "403-webpage", "ip-blocked", "bot-wall", "geo", "reset", "js-challenge"],
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


def _host_lines(hosts_file: Path) -> list[str]:
    """Return the file's host lines, without the explanatory header."""
    return [
        line.strip()
        for line in hosts_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


class TestALessonExpires:
    """A host learned from a refused attempt is tried directly again after the TTL."""

    def test_a_learned_host_is_stamped_and_expires(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        clock = {"now": datetime(2026, 10, 1, 8, 0, tzinfo=UTC)}
        policy = ProxyPolicy(
            PROXY, learned_file=hosts_file, learned_ttl_days=7, now=lambda: clock["now"]
        )

        policy.escalate("example.com")

        assert policy.route_for("example.com") == ("warp", None)
        assert "example.com learned=2026-10-01T08:00:00Z" in hosts_file.read_text(encoding="utf-8")
        clock["now"] += timedelta(days=8)
        assert policy.route_for("example.com") == ("direct", None), "tried directly again"
        assert policy.routed() == ()
        assert policy.escalate("example.com") == PROXY, "and can be learned afresh"

    def test_a_pin_and_a_hand_written_line_never_expire(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("by.hand\n", encoding="utf-8")
        clock = {"now": datetime(2026, 10, 1, 8, 0, tzinfo=UTC)}
        policy = ProxyPolicy(
            PROXY, learned_file=hosts_file, learned_ttl_days=1, now=lambda: clock["now"]
        )

        policy.pin("pinned.example")
        clock["now"] += timedelta(days=400)

        assert policy.route_for("by.hand") == ("warp", None)
        assert policy.route_for("pinned.example") == ("warp", None)
        assert not any("learned=" in line for line in _host_lines(hosts_file))

    def test_an_expired_line_is_ignored_and_dropped_on_the_next_write(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text(
            "old.example learned=2020-01-01T00:00:00Z\n"
            "keep.example proton:nl learned=2026-10-01T00:00:00Z\n",
            encoding="utf-8",
        )
        policy = ProxyPolicy(
            PROXY,
            learned_file=hosts_file,
            learned_ttl_days=7,
            proton_countries=("nl",),
            now=lambda: datetime(2026, 10, 2, tzinfo=UTC),
        )

        assert policy.route_for("old.example") == ("direct", None)
        assert policy.route_for("keep.example") == ("proton", "nl")

        policy.escalate("new.example")
        text = hosts_file.read_text(encoding="utf-8")
        assert "old.example" not in text
        assert "keep.example proton:nl learned=2026-10-01T00:00:00Z" in text
        assert "new.example learned=2026-10-02T00:00:00Z" in text

    def test_a_pin_replaces_the_stamp(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        policy = ProxyPolicy(PROXY, learned_file=hosts_file, learned_ttl_days=7)

        policy.escalate("example.com")
        assert any("learned=" in line for line in _host_lines(hosts_file))
        policy.pin("example.com", "warp")

        assert not any("learned=" in line for line in _host_lines(hosts_file)), "pinned for good"

    def test_without_a_ttl_nothing_expires(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("old.example learned=2000-01-01T00:00:00Z\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=hosts_file)

        assert policy.route_for("old.example") == ("warp", None)

    def test_a_garbled_stamp_is_a_permanent_line(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("odd.example learned=yesterday\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=hosts_file, learned_ttl_days=1)

        assert policy.route_for("odd.example") == ("warp", None)


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

        assert policy.routed() == (("configured.example", "warp"), ("learned.example", "warp"))

    def test_pin_routes_a_host_and_says_whether_it_was_new(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        policy = ProxyPolicy(PROXY, ["configured.example"], learned_file=hosts_file)

        assert policy.pin("New.Example") is True
        assert policy.pin("new.example") is False, "already routed"
        assert policy.pin("cdn.configured.example") is False, "covered by a configured parent"
        assert policy.for_host("new.example") == PROXY
        assert "new.example" in _hosts_in(hosts_file)


class TestTiersInTheFile:
    """A line names a host, and optionally the tier it goes through."""

    def test_a_proton_line_routes_to_that_country(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("a.example proton:nl\nb.example\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=hosts_file, proton_countries=("nl", "pl"))

        assert policy.route_for("a.example") == ("proton", "nl")
        assert policy.route_for("cdn.a.example") == ("proton", "nl")
        assert policy.route_for("b.example") == ("warp", None)
        assert policy.route_for("c.example") == ("direct", None)
        assert policy.for_host("a.example") is None, "the second-tier proxy is not for it"
        assert policy.routed() == (("a.example", "proton:nl"), ("b.example", "warp"))

    def test_a_garbled_tier_falls_back_to_warp(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("a.example protn:xx\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=hosts_file)

        assert policy.route_for("a.example") == ("warp", None)

    def test_learning_a_proton_country_is_written_with_its_tier(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        policy = ProxyPolicy(PROXY, learned_file=hosts_file, proton_countries=("nl", "pl", "ro"))

        policy.learn_proton("Blocked.Example", "pl")

        assert "blocked.example proton:pl" in hosts_file.read_text(encoding="utf-8")
        assert ProxyPolicy(PROXY, learned_file=hosts_file, proton_countries=("pl",)).route_for(
            "blocked.example"
        ) == ("proton", "pl")

    def test_pin_accepts_a_tier_and_moves_a_host_between_tiers(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        policy = ProxyPolicy(PROXY, learned_file=hosts_file, proton_countries=("nl", "pl"))

        assert policy.pin("a.example", "proton:nl") is True
        assert policy.pin("a.example", "proton:nl") is False
        assert policy.pin("a.example", "warp") is True, "moved back to the second tier"
        assert policy.route_for("a.example") == ("warp", None)
        with pytest.raises(ValueError, match="not an egress tier"):
            policy.pin("a.example", "proton:xx")

    def test_the_closest_parent_wins(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "egress-hosts.txt"
        hosts_file.write_text("example proton:ro\nvideo.example\n", encoding="utf-8")
        policy = ProxyPolicy(PROXY, learned_file=hosts_file, proton_countries=("ro",))

        assert policy.route_for("cdn.video.example") == ("warp", None)
        assert policy.route_for("other.example") == ("proton", "ro")


class FakeProton:
    """A third tier whose exit is a distinct proxy URL per country, and which records holds."""

    def __init__(
        self,
        countries: tuple[str, ...] = ("nl", "pl", "ro"),
        *,
        broken: frozenset[str] = frozenset(),
    ) -> None:
        self.countries = countries
        self.holds: list[str] = []
        self.broken = set(broken)

    @contextlib.asynccontextmanager
    async def hold(self, code: str) -> AsyncIterator[str]:
        self.holds.append(code)
        if code in self.broken:
            message = f"the ProtonVPN tunnel did not reach {code}"
            raise ProviderError(message, provider="protonvpn")
        yield f"http://proton-{code}:8888"


def scripted(
    outcomes: Mapping[str | None, BaseException | None],
) -> tuple[Callable[[Mapping[str, Any]], FakeYoutubeDL], list[str | None]]:
    """Return a factory that fails or succeeds per proxy, and the proxies it saw."""
    seen: list[str | None] = []

    def factory(options: Mapping[str, Any]) -> FakeYoutubeDL:
        proxy = options.get("proxy")
        seen.append(proxy)
        outcome = outcomes.get(proxy, RuntimeError("unexpected proxy " + str(proxy)))
        return FakeYoutubeDL(options, info=None if outcome else video_info(), error=outcome)

    return factory, seen


def third_tier(
    config: DownloadSettings, factory: Callable[..., FakeYoutubeDL], proton: FakeProton
) -> YtDlpDownloader:
    return YtDlpDownloader(
        config,
        url_policy=UrlPolicy(),
        address_guard=None,
        youtube_dl_factory=factory,
        proton=proton,
    )


REMOVED = "This video has been removed"
GEO = "The uploader has not made this video available in your country"
PRIVATE = "This video is private"
RESET = "[Errno 104] Connection reset by peer"
CHALLENGE = "PhantomJS not found, please install it"


class TestTheThirdTier:
    """Direct, then WARP, then ProtonVPN by country - and only for what a country can change."""

    async def test_removed_through_warp_is_retried_from_the_netherlands(
        self, tmp_path: Path
    ) -> None:
        factory, seen = scripted(
            {None: RuntimeError(RESET), PROXY: RuntimeError(REMOVED), "http://proton-nl:8888": None}
        )
        proton = FakeProton()
        hosts_file = tmp_path / "hosts.txt"

        metadata = await third_tier(settings(egress_hosts_file=hosts_file), factory, proton).probe(
            URL
        )

        assert metadata.title == "A Test Video"
        assert seen == [None, PROXY, "http://proton-nl:8888"]
        assert proton.holds == ["nl"]
        assert "example.com proton:nl" in hosts_file.read_text(encoding="utf-8")

    async def test_countries_are_tried_in_order_until_one_answers(self) -> None:
        factory, seen = scripted(
            {
                None: RuntimeError(RESET),
                PROXY: RuntimeError(GEO),
                "http://proton-nl:8888": RuntimeError(REMOVED),
                "http://proton-pl:8888": RuntimeError(GEO),
                "http://proton-ro:8888": None,
            }
        )
        proton = FakeProton()

        await third_tier(settings(), factory, proton).probe(URL)

        assert proton.holds == ["nl", "pl", "ro"]
        assert seen[-1] == "http://proton-ro:8888"

    async def test_when_no_country_answers_the_last_answer_is_reported(self) -> None:
        factory, seen = scripted(
            {
                None: RuntimeError(RESET),
                PROXY: RuntimeError(REMOVED),
                "http://proton-nl:8888": RuntimeError(REMOVED),
                "http://proton-pl:8888": RuntimeError(REMOVED),
                "http://proton-ro:8888": RuntimeError(GEO),
            }
        )
        proton = FakeProton()

        with pytest.raises(GeoRestrictedError):
            await third_tier(settings(), factory, proton).probe(URL)

        assert seen.count(None) == 1, "the direct path was tried once and never again"

    async def test_a_private_video_gets_no_third_attempt(self) -> None:
        factory, seen = scripted({None: RuntimeError(RESET), PROXY: RuntimeError(PRIVATE)})
        proton = FakeProton()

        with pytest.raises(MetadataUnavailableError):
            await third_tier(settings(), factory, proton).probe(URL)

        assert proton.holds == []
        assert seen == [None, PROXY]

    async def test_a_browser_challenge_through_warp_gets_no_third_attempt(self) -> None:
        """A JS gate is aimed at the client; no country changes it, and it is not "removed"."""
        factory, seen = scripted({None: RuntimeError(CHALLENGE), PROXY: RuntimeError(CHALLENGE)})
        proton = FakeProton()

        with pytest.raises(SiteChallengeError):
            await third_tier(settings(), factory, proton).probe(URL)

        assert proton.holds == []
        assert seen == [None, PROXY], "direct, then WARP with a browser fingerprint, then stop"

    async def test_a_private_answer_from_a_country_stops_the_search(self) -> None:
        factory, _seen = scripted(
            {
                None: RuntimeError(RESET),
                PROXY: RuntimeError(REMOVED),
                "http://proton-nl:8888": RuntimeError(PRIVATE),
            }
        )
        proton = FakeProton()

        with pytest.raises(MetadataUnavailableError):
            await third_tier(settings(), factory, proton).probe(URL)

        assert proton.holds == ["nl"], "Poland and Romania were not asked about a private video"

    async def test_removed_on_the_direct_path_goes_straight_to_the_third_tier(self) -> None:
        """No reset, so no WARP; but 'removed' is a third-tier answer."""
        factory, seen = scripted({None: RuntimeError(REMOVED), "http://proton-nl:8888": None})
        proton = FakeProton()

        await third_tier(settings(), factory, proton).probe(URL)

        assert seen == [None, "http://proton-nl:8888"]

    async def test_a_learned_host_goes_to_its_country_first(self, tmp_path: Path) -> None:
        hosts_file = tmp_path / "hosts.txt"
        hosts_file.write_text("example.com proton:pl\n", encoding="utf-8")
        factory, seen = scripted({"http://proton-pl:8888": None})
        proton = FakeProton()

        await third_tier(settings(egress_hosts_file=hosts_file), factory, proton).probe(URL)

        assert seen == ["http://proton-pl:8888"], "neither direct nor WARP was tried"
        assert proton.holds == ["pl"]

    async def test_a_learned_host_whose_country_stopped_working_tries_the_others(
        self, tmp_path: Path
    ) -> None:
        hosts_file = tmp_path / "hosts.txt"
        hosts_file.write_text("example.com proton:pl\n", encoding="utf-8")
        factory, seen = scripted(
            {"http://proton-pl:8888": RuntimeError(REMOVED), "http://proton-nl:8888": None}
        )
        proton = FakeProton()

        await third_tier(settings(egress_hosts_file=hosts_file), factory, proton).probe(URL)

        assert proton.holds == ["pl", "nl"]
        assert seen == ["http://proton-pl:8888", "http://proton-nl:8888"]
        assert "example.com proton:nl" in hosts_file.read_text(encoding="utf-8")

    async def test_a_tunnel_that_cannot_reach_a_country_moves_on_and_never_goes_direct(
        self,
    ) -> None:
        """The kill switch: a broken exit is skipped, and the direct path is not a fallback."""
        factory, seen = scripted(
            {None: RuntimeError(RESET), PROXY: RuntimeError(REMOVED), "http://proton-pl:8888": None}
        )
        proton = FakeProton(broken=frozenset({"nl"}))

        await third_tier(settings(), factory, proton).probe(URL)

        assert proton.holds == ["nl", "pl"]
        assert seen == [None, PROXY, "http://proton-pl:8888"]

    async def test_all_exits_broken_is_reported_as_the_tunnel_not_the_video(self) -> None:
        factory, seen = scripted({None: RuntimeError(RESET), PROXY: RuntimeError(REMOVED)})
        proton = FakeProton(broken=frozenset({"nl", "pl", "ro"}))

        with pytest.raises(ProviderError, match="did not reach"):
            await third_tier(settings(), factory, proton).probe(URL)

        assert seen.count(None) == 1

    async def test_without_a_third_tier_removed_is_final(self) -> None:
        factory, seen = scripted({None: RuntimeError(RESET), PROXY: RuntimeError(REMOVED)})

        with pytest.raises(ContentRemovedError):
            await downloader(settings(), factory).probe(URL)

        assert seen == [None, PROXY]

    async def test_a_fetch_through_proton_says_so(self, scope: WorkspaceScope) -> None:
        def factory(options: Mapping[str, Any]) -> FakeYoutubeDL:
            proxy = options.get("proxy")
            if proxy == "http://proton-nl:8888":
                return FakeYoutubeDL(options, info=video_info(), script=writes("abc123.mp4", 2048))
            return FakeYoutubeDL(options, error=RuntimeError(RESET if proxy is None else REMOVED))

        result = await third_tier(settings(), factory, FakeProton()).fetch(
            DownloadRequest(url=URL, selection=FormatSelection.best()), scope
        )

        assert result.via_proxy is True
        assert result.egress == "proton:nl"

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
    """Return the hosts named in the file: the first token of every host line."""
    return {
        line.split()[0]
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
