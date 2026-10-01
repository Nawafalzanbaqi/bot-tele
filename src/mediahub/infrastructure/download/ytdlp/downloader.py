"""The yt-dlp download engine adapter.

Implements :class:`~mediahub.application.download.ports.DownloaderPort`.

Execution model: yt-dlp is a synchronous library, so every call runs in a worker
thread via :func:`asyncio.to_thread` and the event loop stays free. A thread
cannot be killed, so stopping work is **cooperative**: the caller sets a
cancellation token, the progress hook observes it and raises, and the thread
unwinds through yt-dlp's own cleanup. The same mechanism enforces the wall-clock
deadline and the byte ceiling
(:mod:`~mediahub.infrastructure.download.ytdlp.progress`).

Whatever happens, files created by a failed or cancelled attempt are removed
before the error is raised: the caller's lease is left exactly as it was found.

**Abandoned threads are counted, not ignored.** Cooperative cancellation has one
failure mode: a thread wedged somewhere that never reaches a hook - inside a
blocking DNS lookup, say - cannot be stopped at all. After the drain budget the
caller gives up and returns, but the thread does not, and it keeps a socket, a
descriptor and a slot of the thread pool for as long as the process lives. A
handful of those and no download can ever start again, silently, with the
process still looking healthy. :func:`engine_thread_stats` therefore reports how
many are running and how many were abandoned, so the condition is observable at
shutdown and in the failure tests rather than deduced from a hang months later.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from loguru import logger

from mediahub.application.common.cancellation import CancellationReason
from mediahub.application.download.errors import (
    ConnectionBlockedError,
    DownloadCancelledError,
    DownloadError,
    DownloadTimeoutError,
    GeoRestrictedError,
    LiveSourceNotAllowedError,
    MetadataUnavailableError,
    PlaylistNotAllowedError,
    ProviderError,
    SiteChallengeError,
    SizeLimitExceededError,
)
from mediahub.application.download.ports import (
    DownloadCapabilities,
    DownloadResult,
    DownloadStage,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.shared.retry import RetrySchedule, retry_async
from mediahub.infrastructure.download.ytdlp.errors import classify
from mediahub.infrastructure.download.ytdlp.format_selection import build_format_expression
from mediahub.infrastructure.download.ytdlp.inspection import verify_complete
from mediahub.infrastructure.download.ytdlp.mapping import to_metadata, to_selected_format
from mediahub.infrastructure.download.ytdlp.options import (
    build_download_options,
    build_probe_options,
)
from mediahub.infrastructure.download.ytdlp.progress import (
    EngineAbort,
    ProgressBridge,
    SizeCeilingExceeded,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.ports import (
        DownloadRequest,
        MediaMetadata,
        ProgressCallback,
    )
    from mediahub.application.workspace.ports import ArtifactRef, WorkspaceScope
    from mediahub.domain.sources.value_objects import ValidatedUrl
    from mediahub.infrastructure.download.ytdlp.inspection import StreamInspector
    from mediahub.infrastructure.security.address_guard import DnsAddressGuard
    from mediahub.shared.config.settings import DownloadSettings

    YoutubeDLFactory = Callable[[Mapping[str, Any]], "YoutubeDLLike"]

try:  # pragma: no cover - exercised by the presence or absence of the package
    import yt_dlp.version
    from yt_dlp import YoutubeDL as _YoutubeDL

    _ENGINE_VERSION = str(yt_dlp.version.__version__)
except ImportError:  # pragma: no cover - the engine is an optional install
    _YoutubeDL = None
    _ENGINE_VERSION = "unavailable"

try:  # pragma: no cover - depends on the yt-dlp build
    from yt_dlp.networking.impersonate import ImpersonateTarget as _ImpersonateTarget
except ImportError:  # pragma: no cover - older yt-dlp, or no impersonation support
    _ImpersonateTarget = None

ENGINE_NAME: Final[str] = "yt-dlp"
_THREAD_DRAIN_SECONDS: Final[float] = 30.0
_IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
)
_SUBTITLE_EXTENSIONS: Final[frozenset[str]] = frozenset({".vtt", ".srt", ".ass", ".ssa"})
_PARTIAL_SUFFIXES: Final[tuple[str, ...]] = (".part", ".ytdl", ".temp")


class ProxyPolicy:
    """Decides which hosts are fetched through the egress proxy.

    **Nothing goes through it by default.** A tunnel is slower than the direct
    path and usually metered, and almost every source is reachable without one;
    routing everything through it would make every download worse in order to
    fix a few. So the proxy is used for a host only when that host has given a
    reason.

    The reason is specific and self-evident: a connection that opened and was
    then reset before any data came back
    (:class:`~mediahub.application.download.errors.ConnectionBlockedError`).
    That is a path failure rather than a source failure, and it is the one thing
    a different egress can cure. When it happens the host is remembered, so the
    cost of learning is a single fast failure, paid once.

    What is learned is kept in a plain text file when one is configured, so a
    restart does not pay the failed direct attempt again for a host that was
    refused yesterday. The file is the operator's too: one host per line,
    comments allowed, re-read whenever it changes, so a line can be removed to
    try a host directly again without restarting anything. Without a file the
    memory is per-process, as before. ``proxy_hosts`` in the settings is the
    static half of the same list.
    """

    __slots__ = (
        "_file",
        "_file_mtime",
        "_impersonate",
        "_impersonate_hosts",
        "_learned",
        "_listed",
        "_lock",
        "_proton_countries",
        "_proxy",
    )

    def __init__(
        self,
        proxy: str | None,
        hosts: Sequence[str] = (),
        *,
        impersonate: str | None = None,
        impersonate_hosts: Sequence[str] = (),
        learned_file: Path | None = None,
        proton_countries: Sequence[str] = (),
    ) -> None:
        """Bind the policy to an egress, the hosts known to need it, and a browser fingerprint.

        Args:
            proxy: The egress, or ``None`` when there is none to fall back to.
            hosts: Hosts routed through the egress from the first attempt.
            impersonate: curl_cffi target presented on every proxied request
                and on direct requests to ``impersonate_hosts``; ``None`` to
                never impersonate.
            impersonate_hosts: Hosts that fingerprint the TLS handshake and so
                need the browser fingerprint even on the direct path.
            learned_file: Where hosts that turned out to need the egress are
                kept between processes, and where the operator edits them.
                ``None`` keeps the memory per-process.
            proton_countries: The third tier's country codes, when it exists.
                Only used to say what ``/vpn`` may ask for.
        """
        self._proxy = proxy or None
        self._listed = frozenset(host.lower().lstrip(".") for host in hosts if host)
        self._learned: dict[str, str] = {}
        self._proton_countries = tuple(code.lower() for code in proton_countries)
        self._impersonate = impersonate or None
        self._impersonate_hosts = frozenset(
            host.lower().lstrip(".") for host in impersonate_hosts if host
        )
        # Probes and fetches run on engine threads; the set is written from
        # whichever one first meets a reset.
        self._lock = threading.Lock()
        self._file = learned_file
        self._file_mtime: int | None = None
        self._load()

    @property
    def is_configured(self) -> bool:
        """Return whether an egress exists to fall back to."""
        return self._proxy is not None

    @property
    def proton_countries(self) -> tuple[str, ...]:
        """Return the third tier's country codes; empty when there is no third tier."""
        return self._proton_countries

    def for_host(self, host: str) -> str | None:
        """Return the second-tier egress this host should use, or ``None`` for direct."""
        if self._proxy is None:
            return None
        tier, _country = self.route_for(host)
        return self._proxy if tier == "warp" else None

    def route_for(self, host: str) -> tuple[str, str | None]:
        """Return ``(tier, country)`` for ``host``: direct, warp, or proton with a code.

        Configured hosts are second-tier. A learned host carries the tier it
        was learned on. Subdomains follow their parent.
        """
        self._reload_if_edited()
        lowered = host.lower()
        with self._lock:
            learned = dict(self._learned)
        tier = _tier_for(lowered, learned)
        if tier is None and _matches(lowered, self._listed):
            tier = "warp"
        if tier is None:
            return "direct", None
        if tier.startswith("proton:"):
            return "proton", tier.split(":", maxsplit=1)[1]
        return ("warp", None) if self._proxy is not None else ("direct", None)

    def impersonation_for(self, host: str, *, proxied: bool) -> str | None:
        """Return the browser fingerprint to present to ``host``, if any.

        Always on a proxied request - a shared exit address is where bot walls
        look hardest, and it is the difference between 403 and 200 on TikTok's
        short-link resolver - and on the direct path only for hosts known to
        fingerprint the handshake regardless of where it comes from.
        """
        if self._impersonate is None:
            return None
        if proxied or _matches(host.lower(), self._impersonate_hosts):
            return self._impersonate
        return None

    def escalate(self, host: str) -> str | None:
        """Remember that ``host`` needs the egress, and return it.

        Returns ``None`` when there is nothing to escalate to, or when this host
        was already being routed - in which case the proxy is not the answer and
        the original failure is the honest one.
        """
        if self._proxy is None or self.route_for(host)[0] != "direct":
            return None
        self._remember(host, "warp")
        logger.bind(host=host).info(
            "Direct connection to this host was reset; routing it through the egress proxy"
        )
        return self._proxy

    def learn_proton(self, host: str, country: str) -> None:
        """Remember that ``host`` worked through the third tier in ``country``."""
        self._remember(host, f"proton:{country.lower()}")
        logger.bind(host=host, country=country).info(
            "Host answered through the ProtonVPN exit; routing it there from now on"
        )

    def pin(self, host: str, tier: str = "warp") -> bool:
        """Route ``host`` through ``tier`` from now on, at the operator's request.

        Returns whether anything changed. A host already on that tier -
        configured, learned, or a parent domain of either - is left as it is.

        Raises:
            ValueError: If ``tier`` is not ``warp`` or ``proton:<cc>`` for a
                configured country.
        """
        tier = tier.lower()
        if tier != "warp" and not (
            tier.startswith("proton:") and tier[7:] in self._proton_countries
        ):
            message = f"'{tier}' is not an egress tier this deployment has"
            raise ValueError(message)
        if self._proxy is None:
            return False
        current, country = self.route_for(host)
        current_label = current if country is None else f"{current}:{country}"
        if current_label == tier:
            return False
        self._remember(host, tier)
        logger.bind(host=host, tier=tier).info("Host pinned to an egress tier")
        return True

    def routed(self) -> tuple[tuple[str, str], ...]:
        """Return every routed host with its tier, sorted by host."""
        self._reload_if_edited()
        with self._lock:
            entries = dict.fromkeys(self._listed, "warp")
            entries.update(self._learned)
        return tuple(sorted(entries.items()))

    @property
    def learned_hosts(self) -> tuple[str, ...]:
        """Return the hosts discovered to need an egress. Diagnostic helper."""
        self._reload_if_edited()
        with self._lock:
            return tuple(sorted(self._learned))

    # -- The file ------------------------------------------------------------

    def _remember(self, host: str, tier: str) -> None:
        """Record a host's tier and write the list out."""
        with self._lock:
            self._learned[host.lower().lstrip(".")] = tier
        self._save()

    def _load(self) -> None:
        """Read the learned hosts from the file, if there is one."""
        if self._file is None:
            return
        try:
            stat = self._file.stat()
        except FileNotFoundError:
            with self._lock:
                self._learned = {}
                self._file_mtime = None
            return
        except OSError:
            logger.opt(exception=True).warning("Could not read the egress host list")
            return
        try:
            text = self._file.read_text(encoding="utf-8")
        except OSError:
            logger.opt(exception=True).warning("Could not read the egress host list")
            return
        hosts = _parse_host_list(text)
        with self._lock:
            self._learned = hosts
            self._file_mtime = stat.st_mtime_ns
        logger.bind(count=len(hosts)).debug("Egress host list loaded")

    def _reload_if_edited(self) -> None:
        """Pick up a hand edit - or a deletion - without a restart.

        One ``stat`` per request. The file is the operator's control surface:
        removing a line must let that host be tried directly again, and
        removing the file must forget everything, both while the bot runs.
        """
        if self._file is None:
            return
        try:
            mtime: int | None = self._file.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        except OSError:
            return
        with self._lock:
            unchanged = mtime == self._file_mtime
        if unchanged:
            return
        self._load()

    def _save(self) -> None:
        """Write the learned hosts out atomically. A failure is logged, never raised."""
        if self._file is None:
            return
        with self._lock:
            entries = sorted(self._learned.items())
        body = _HOST_LIST_HEADER + "".join(
            f"{host}\n" if tier == "warp" else f"{host} {tier}\n" for host, tier in entries
        )
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._file.with_name(self._file.name + ".tmp")
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(self._file)
            with self._lock:
                self._file_mtime = self._file.stat().st_mtime_ns
        except OSError:
            logger.opt(exception=True).warning(
                "Could not write the egress host list; the lesson is kept for this process only"
            )


def _matches(host: str, names: frozenset[str]) -> bool:
    """Return whether ``host`` is one of ``names`` or a subdomain of one."""
    return any(host == name or host.endswith(f".{name}") for name in names)


_HOST_LIST_HEADER: Final[str] = (
    "# Hosts the download engine sends through an egress tunnel on the first attempt.\n"
    "# One host per line; subdomains are covered; '#' starts a comment.\n"
    "#   host              -> through WARP\n"
    "#   host proton:nl    -> through the ProtonVPN exit in that country (nl, pl, ro)\n"
    "# A line is added when a direct connection is refused in a way that names the address\n"
    "# (reset, 403, geo-fence, bot wall), when a site answers through WARP with removed /\n"
    "# geo-blocked / unavailable and a ProtonVPN exit then works, or by /vpn [nl|pl|ro] <link>.\n"
    "# The running bot re-reads this file whenever it changes: delete a line to try that host\n"
    "# directly again, delete the file to forget all of them. MEDIAHUB_DOWNLOAD__PROXY_HOSTS in\n"
    "# .env is the static (WARP) half of the list.\n"
)


def _parse_host_list(text: str) -> dict[str, str]:
    """Return ``host -> tier`` from a host-list file, ignoring comments and blanks.

    A bare host is the second tier (``warp``); ``host proton:nl`` is the third.
    Anything else after the host is ignored rather than fatal - a typo in a
    hand-edited file must not take the whole list with it.
    """
    hosts: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip().lower()
        if not line:
            continue
        host, _, tier = line.partition(" ")
        host = host.lstrip(".")
        tier = tier.strip()
        if not host:
            continue
        code = tier.removeprefix("proton:")
        hosts[host] = tier if tier.startswith("proton:") and code else "warp"
    return hosts


def _tier_for(host: str, learned: Mapping[str, str]) -> str | None:
    """Return the learned tier that covers ``host``, the closest parent winning."""
    best: tuple[int, str] | None = None
    for name, tier in learned.items():
        covers = host == name or host.endswith(f".{name}")
        if covers and (best is None or len(name) > best[0]):
            best = (len(name), tier)
    return None if best is None else best[1]


@dataclass(frozen=True, slots=True)
class EgressUsed:
    """Which exit carried an engine call."""

    tier: str = "direct"
    country: str | None = None

    @property
    def label(self) -> str:
        """Return ``direct``, ``warp`` or ``proton:<cc>``."""
        return self.tier if self.country is None else f"{self.tier}:{self.country}"

    @property
    def via_proxy(self) -> bool:
        """Return whether any tunnel was involved."""
        return self.tier != "direct"


class ThirdTier(Protocol):
    """What the adapter needs from the ProtonVPN tier: its countries, and a held exit."""

    @property
    def countries(self) -> tuple[str, ...]:
        """Return the country codes in the order to try them."""
        ...

    def hold(self, code: str) -> AbstractAsyncContextManager[str]:
        """Take the tunnel in ``code`` and yield the proxy URL to use."""
        ...


_THIRD_TIER_CODES: Final[frozenset[str]] = frozenset({"content_removed", "geo_restricted"})
_THIRD_TIER_SIGNATURES: Final[tuple[str, ...]] = ("unavailable", "not available")


def _wants_third_tier(error: DownloadError) -> bool:
    """Return whether the site named the *content* as unavailable from this exit.

    Removed, geo-blocked and "unavailable" are the answers a different country
    has been seen to change. A private video or a login wall is not: it fails
    identically everywhere and gets no third attempt.
    """
    if error.code in _THIRD_TIER_CODES:
        return True
    text = str(error).lower()
    return any(needle in text for needle in _THIRD_TIER_SIGNATURES)


_ESCALATION_SIGNATURES: Final[tuple[str, ...]] = (
    "http error 403",
    "forbidden",
    "access denied",
    "your ip address is blocked",
    "unable to extract",
    "not available in your",
    "blocked in your",
)
"""Failure text that names the *address*, not the content, as the problem.

Each of these has been seen to succeed from a different exit: a 403 on a
resolver that fingerprints or rate-limits by IP, an explicit "your IP is
blocked", TikTok's "unable to extract" bot wall, and geo-fences. A private video
or a deleted post is deliberately not here - they fail identically everywhere.
"""


def _should_escalate(error: DownloadError) -> bool:
    """Return whether a failure is one a different exit address might cure."""
    if isinstance(error, (ConnectionBlockedError, GeoRestrictedError, SiteChallengeError)):
        # A browser challenge is aimed at the client, and the proxied attempt
        # presents a browser fingerprint (ProxyPolicy.impersonation_for), which
        # is what gets PornHub's JS gate out of the way on this deployment.
        return True
    text = str(error).lower()
    return any(signature in text for signature in _ESCALATION_SIGNATURES)


@dataclass(frozen=True, slots=True)
class EngineThreadStats:
    """How many engine threads this process is carrying.

    Attributes:
        running: Threads currently inside a probe or a fetch. Bounded by the
            worker's slot count in a healthy process.
        abandoned: Threads the caller gave up waiting for. **Never decreases**,
            because the whole point is that nobody knows whether they finished.
            Anything above zero is a leak that will eventually exhaust the
            thread pool, and the number is the evidence.
    """

    running: int
    abandoned: int

    @property
    def is_leaking(self) -> bool:
        """Return whether any engine thread has ever been abandoned."""
        return self.abandoned > 0


class _ThreadLedger:
    """Counts engine threads. Trivially small, deliberately global.

    Global because the leak it detects is a property of the *process* - a thread
    that outlives the downloader that started it is exactly the case that needs
    counting, and an instance attribute would be collected along with the
    downloader and take the evidence with it.
    """

    __slots__ = ("_abandoned", "_lock", "_running")

    def __init__(self) -> None:
        """Start with nothing running and nothing abandoned."""
        self._lock = threading.Lock()
        self._running = 0
        self._abandoned = 0

    def started(self) -> None:
        """Record that an engine thread has begun work."""
        with self._lock:
            self._running += 1

    def finished(self) -> None:
        """Record that an engine thread has unwound, however it ended."""
        with self._lock:
            self._running = max(0, self._running - 1)

    def abandoned(self) -> int:
        """Record that a caller stopped waiting, and return the running total."""
        with self._lock:
            self._abandoned += 1
            return self._abandoned

    def stats(self) -> EngineThreadStats:
        """Return the current picture."""
        with self._lock:
            return EngineThreadStats(running=self._running, abandoned=self._abandoned)

    def reset(self) -> None:
        """Forget everything. Tests only."""
        with self._lock:
            self._running = 0
            self._abandoned = 0


_THREADS: Final[_ThreadLedger] = _ThreadLedger()


def engine_thread_stats() -> EngineThreadStats:
    """Return how many engine threads are running and how many were abandoned.

    Read at shutdown and by the failure suite. A non-zero ``abandoned`` count
    means this process has permanently lost thread-pool capacity and should be
    restarted before it runs out.
    """
    return _THREADS.stats()


def reset_engine_thread_stats() -> None:
    """Clear the ledger so one test's leak is not another's. Tests only."""
    _THREADS.reset()


class YoutubeDLLike(Protocol):
    """Structural description of the slice of ``YoutubeDL`` this adapter uses.

    Deliberately tiny: the smaller this surface, the less of yt-dlp's API
    MediaHub is coupled to, and the simpler a test double is. Structural typing
    means neither the real engine nor a fake has to inherit anything.
    """

    def extract_info(self, url: str, *, download: bool = True) -> Mapping[str, Any] | None:
        """Extract metadata, optionally downloading the media."""
        ...

    def close(self) -> None:
        """Release engine resources."""
        ...


def default_youtube_dl_factory(options: Mapping[str, Any]) -> YoutubeDLLike:
    """Build a real ``YoutubeDL`` instance.

    Raises:
        MetadataUnavailableError: If yt-dlp is not installed, which is a
            configuration problem rather than a source problem, but must still
            reach the caller as a typed error.
    """
    if _YoutubeDL is None:  # pragma: no cover - depends on the environment
        message = "yt-dlp is not installed; the download engine cannot run"
        raise MetadataUnavailableError(message)
    prepared = dict(options)
    target = prepared.get("impersonate")
    if isinstance(target, str):  # pragma: no cover - depends on the environment
        resolved = _impersonate_target(target)
        if resolved is None:
            prepared.pop("impersonate")
        else:
            prepared["impersonate"] = resolved
    engine: YoutubeDLLike = _YoutubeDL(prepared)
    return engine


def _impersonate_target(name: str) -> Any:  # pragma: no cover - depends on the environment
    """Turn a target name into yt-dlp's own type, or ``None`` if it cannot be honoured.

    The options module keeps the name as a plain string so it stays free of
    yt-dlp imports; the conversion belongs here, next to the only place that
    constructs the real engine. A missing curl_cffi or an unknown target name
    degrades to yt-dlp's own client with a warning rather than a failed download.
    """
    if _ImpersonateTarget is None:
        logger.bind(target=name).warning(
            "Browser impersonation is unavailable in this build; continuing without it"
        )
        return None
    try:
        return _ImpersonateTarget.from_str(name)
    except Exception:
        logger.bind(target=name).warning(
            "Unknown impersonation target; continuing without a browser fingerprint"
        )
        return None


class YtDlpDownloader:
    """Downloads media with yt-dlp, behind the engine-neutral port."""

    __slots__ = (
        "_address_guard",
        "_factory",
        "_inspector",
        "_proton",
        "_proxy_policy",
        "_settings",
        "_url_policy",
    )

    def __init__(
        self,
        settings: DownloadSettings,
        *,
        url_policy: UrlPolicy | None = None,
        address_guard: DnsAddressGuard | None = None,
        youtube_dl_factory: YoutubeDLFactory | None = None,
        proxy_policy: ProxyPolicy | None = None,
        inspector: StreamInspector | None = None,
        proton: ThirdTier | None = None,
    ) -> None:
        """Wire the engine to its configuration and its guards.

        Args:
            settings: Engine configuration.
            url_policy: URL rules. A default policy is used when omitted.
            address_guard: Resolves hosts and rejects forbidden addresses.
                Omitting it disables the DNS half of the SSRF gate; the
                syntactic half always runs.
            youtube_dl_factory: Builds the engine object. Tests inject a fake,
                which is what keeps the unit suite free of network access.
            proxy_policy: Decides which hosts are fetched through the egress.
                Built from ``settings`` when omitted.
            inspector: Looks inside the finished file and refuses one that is
                not the media that was asked for. Omitting it skips the check,
                which is right for a device without ``ffprobe`` and for tests
                whose "downloads" are a few zero bytes.
            proton: The third egress tier, asked only when a site answers
                through the second one with "removed", "geo-blocked" or
                "unavailable". ``None`` means there are two tiers.
        """
        self._settings = settings
        self._url_policy = url_policy or UrlPolicy()
        self._address_guard = address_guard
        self._factory = youtube_dl_factory or default_youtube_dl_factory
        self._inspector = inspector
        self._proton = proton
        self._proxy_policy = proxy_policy or ProxyPolicy(
            settings.proxy,
            settings.proxy_hosts,
            impersonate=settings.impersonate,
            impersonate_hosts=settings.impersonate_hosts,
            learned_file=settings.egress_hosts_file,
            proton_countries=proton.countries if proton is not None else (),
        )

    @property
    def proxy_policy(self) -> ProxyPolicy:
        """Return the egress policy, so the operator's surface can pin and list hosts."""
        return self._proxy_policy

    # -- Port surface --------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the engine's name."""
        return ENGINE_NAME

    def capabilities(self) -> DownloadCapabilities:
        """Return what this engine can currently do."""
        return DownloadCapabilities(
            engine=ENGINE_NAME,
            version=_ENGINE_VERSION,
            supports_probe=True,
            supports_format_selection=True,
            supports_audio_only=True,
            supports_resume=True,
            supports_playlists=True,
            supports_live=False,
            supports_thumbnails=True,
            requires_external_merger=True,
            max_concurrent_fragments=self._settings.concurrent_fragments,
        )

    def supports(self, url: str) -> bool:
        """Return whether this engine will attempt ``url``.

        Deliberately a cheap syntactic check. yt-dlp ships a generic extractor
        that accepts any HTTP(S) URL, so enumerating its ~1800 site-specific
        extractors here would cost milliseconds per call to answer a question
        that only a probe can settle. Which platform actually claimed the URL is
        reported as :attr:`MediaMetadata.provider` after probing - that is the
        platform detection, and it needs no hard-coded provider list.
        """
        try:
            self._url_policy.validate(url)
        except Exception:
            # Any rejection - domain error or a malformed value the parser
            # choked on - means the same thing here: not supported.
            return False
        return True

    async def probe(self, url: str, *, timeout_seconds: float | None = None) -> MediaMetadata:
        """Read metadata without downloading the payload."""
        validated = self._validate(url)
        budget = timeout_seconds or self._settings.probe_timeout_seconds
        schedule = RetrySchedule(
            attempts=self._settings.probe_attempts,
            base_delay_seconds=self._settings.probe_backoff_seconds,
        )

        async def attempt() -> MediaMetadata:
            return await self._probe_once(validated, timeout_seconds=budget)

        return await retry_async(attempt, schedule=schedule, description=f"probe {validated.host}")

    async def fetch(
        self,
        request: DownloadRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        """Download the requested rendition into ``workspace``."""
        validated = self._validate(request.url)
        started_at = datetime.now(UTC)
        max_bytes = request.max_bytes or self._settings.max_item_bytes
        budget = request.timeout_seconds or self._settings.download_timeout_seconds

        bridge = ProgressBridge(
            callback=on_progress,
            cancellation=cancellation,
            max_bytes=max_bytes,
            deadline=time.monotonic() + budget if budget else None,
            min_interval_seconds=self._settings.progress_interval_seconds,
        )
        bridge.stage(DownloadStage.VALIDATING)

        existing = set(workspace.names())
        bound = logger.bind(url=validated.host, lease_id=workspace.lease_id)

        try:
            bridge.stage(DownloadStage.SELECTING)

            async def run(proxy: str | None, impersonate: str | None) -> Mapping[str, Any]:
                return await self._run_guarded(
                    lambda: self._blocking_fetch(
                        request, validated, workspace, bridge, proxy, impersonate
                    ),
                    timeout_seconds=budget,
                    cancellation=cancellation,
                    url=validated.value,
                )

            # Retried for the same reason a probe is, and it was missing here:
            # extraction runs again at the start of every download, and some
            # extractors fail a noticeable share of the time for no visible
            # reason. Without this a flaky quarter of attempts reached the user
            # as a flat failure - "some links work and some do not", with
            # nothing to tell them apart.
            info, used = await retry_async(
                lambda: self._with_egress_fallback(run, host=validated.host),
                schedule=RetrySchedule(
                    attempts=self._settings.download_attempts,
                    base_delay_seconds=self._settings.probe_backoff_seconds,
                ),
                description=f"download {validated.host}",
            )
            bridge.stage(DownloadStage.VERIFYING)
            result = self._build_result(
                request=request,
                validated=validated,
                workspace=workspace,
                info=info,
                existing=existing,
                started_at=started_at,
                max_bytes=max_bytes,
                egress=used,
            )
            await self._inspect(result, workspace, url=validated.value)
        except BaseException:
            self._discard_new_files(workspace, existing)
            raise
        else:
            bridge.stage(DownloadStage.COMPLETED)
            bound.bind(
                provider=result.provider,
                bytes=result.total_bytes,
                seconds=round(result.duration_seconds, 2),
                format_id=result.selected_format.format_id,
            ).info("Download complete")
            return result

    # -- Validation ----------------------------------------------------------

    def _validate(self, url: str) -> ValidatedUrl:
        """Apply the URL policy, then the address guard.

        Raises the domain's URL errors unchanged: "which URLs may this system
        fetch" is a rule shared by every interface, so it must not be restated
        as an engine-specific error.
        """
        validated = self._url_policy.validate(url)
        if self._address_guard is not None:
            self._address_guard.check(validated)
        return validated

    async def _with_egress_fallback(
        self,
        run: Callable[[str | None, str | None], Awaitable[Mapping[str, Any]]],
        *,
        host: str,
    ) -> tuple[Mapping[str, Any], EgressUsed]:
        """Run an engine call, retrying through the egress if the *address* was the problem.

        Returns the engine's answer and whether it came through the proxy, so
        the caller can say which path worked.

        The retry is deliberately narrow. It fires for a reset connection, a
        geo-fence, a 403, an explicit "your IP is blocked" and a bot wall - the
        failures that name the exit address rather than the content, and the
        ones a different exit has actually been seen to cure. A 404, a private
        video or an expired session would fail identically through a tunnel, and
        retrying them there would double the time to report a failure that was
        already final.

        At most one escalation to the second tier: if the host was already
        being routed, that proxy is not the answer. Proxied requests present a
        browser fingerprint (see :meth:`ProxyPolicy.impersonation_for`); direct
        ones do so only for hosts known to need it.

        The third tier comes after that, and only when the answer named the
        *content* as unavailable from here - removed, geo-blocked, unavailable -
        because that is what a different country can change. Countries are
        tried in their configured order; the one that works is remembered for
        the host. A host already learned or pinned to the third tier goes there
        first and never touches the direct path.
        """
        tier, country = self._proxy_policy.route_for(host)
        if tier == "proton" and self._proton is not None:
            return await self._through_proton(run, host, first=country, after=None)

        proxy = self._proxy_policy.for_host(host)
        proxied = proxy is not None
        try:
            info = await run(proxy, self._proxy_policy.impersonation_for(host, proxied=proxied))
        except DownloadError as error:
            last: DownloadError = error
        else:
            return info, EgressUsed("warp" if proxied else "direct")

        escalated = self._proxy_policy.escalate(host) if _should_escalate(last) else None
        if escalated is not None:
            logger.bind(host=host, code=last.code).info(
                "Refused on the direct path; retrying through the egress proxy"
            )
            try:
                info = await run(
                    escalated, self._proxy_policy.impersonation_for(host, proxied=True)
                )
            except DownloadError as second:
                last = second
            else:
                return info, EgressUsed("warp")
        if self._proton is not None and _wants_third_tier(last):
            return await self._through_proton(run, host, first=None, after=last)
        raise last

    async def _through_proton(
        self,
        run: Callable[[str | None, str | None], Awaitable[Mapping[str, Any]]],
        host: str,
        *,
        first: str | None,
        after: DownloadError | None,
    ) -> tuple[Mapping[str, Any], EgressUsed]:
        """Try the third tier's countries in order, ``first`` ahead of the rest.

        Stops at the first country that answers, and moves on only when the
        failure is one another country could cure (content unavailable from
        here) or the tunnel itself could not be reached. Anything else - a
        private video, a login wall - is final and is raised as it is. Never
        falls back to the direct path: a host that reached this tier was
        refused there already, and the kill switch is a promise.
        """
        assert self._proton is not None  # noqa: S101 - the caller checked
        countries = list(self._proton.countries)
        if first is not None and first in countries:
            countries.remove(first)
            countries.insert(0, first)
        last = after
        for code in countries:
            try:
                async with self._proton.hold(code) as proxy:
                    logger.bind(host=host, country=code).info("Trying the ProtonVPN exit")
                    info = await run(
                        proxy, self._proxy_policy.impersonation_for(host, proxied=True)
                    )
            except DownloadError as error:
                last = error
                if _wants_third_tier(error) or isinstance(error, ProviderError):
                    continue
                raise
            self._proxy_policy.learn_proton(host, code)
            return info, EgressUsed("proton", code)
        if last is None:  # pragma: no cover - a tier with no countries is refused at construction
            message = f"'{host}' could not be fetched through any ProtonVPN exit"
            raise ProviderError(message, provider="protonvpn")
        raise last

    # -- Probing -------------------------------------------------------------

    async def _probe_once(
        self, validated: ValidatedUrl, *, timeout_seconds: float
    ) -> MediaMetadata:
        """Perform one probe attempt, escalating to the egress if cut off."""

        async def run(proxy: str | None, impersonate: str | None) -> Mapping[str, Any]:
            return await self._run_guarded(
                lambda: self._blocking_probe(
                    validated,
                    timeout_seconds=timeout_seconds,
                    proxy=proxy,
                    impersonate=impersonate,
                ),
                timeout_seconds=timeout_seconds,
                cancellation=None,
                url=validated.value,
            )

        info, used = await self._with_egress_fallback(run, host=validated.host)
        metadata = to_metadata(info, url=validated.value, probed_at=datetime.now(UTC))
        logger.bind(
            provider=metadata.provider,
            kind=metadata.kind.value,
            live=metadata.is_live,
            playlist=metadata.is_playlist,
            expected_bytes=metadata.expected_bytes,
            egress=used.label,
        ).debug("Probed source")
        if metadata.is_playlist:
            return await self._first_of(metadata, info, timeout_seconds=timeout_seconds)
        return metadata

    async def _first_of(
        self, collection: MediaMetadata, info: Mapping[str, Any], *, timeout_seconds: float
    ) -> MediaMetadata:
        """Resolve a collection to its first entry, when it has one that can be fetched.

        A person who pastes a playlist link almost always wants *a* video, and
        the first is the only defensible guess; refusing the whole link taught
        them nothing except to open the list and copy an entry by hand. The
        entry's URL goes through the same validation as anything typed in - it
        came from a third party's page - and if it cannot be fetched, or the
        collection is empty, the collection is described as before and the
        caller decides what to say.
        """
        first = _first_entry_url(info)
        if first is None:
            return collection
        try:
            validated = self._validate(first)
        except Exception as error:
            logger.bind(url=collection.url, reason=type(error).__name__).info(
                "A collection's first entry could not be fetched; describing the collection"
            )
            return collection
        entry = await self._probe_once(validated, timeout_seconds=timeout_seconds)
        if entry.is_playlist:
            # A collection of collections. One level is what was promised.
            return collection
        size = collection.entry_count
        logger.bind(collection=collection.url, entry=entry.url, size=size).info(
            "Resolved a collection to its first entry"
        )
        return replace(entry, from_playlist=True, entry_count=size)

    def _blocking_probe(
        self,
        validated: ValidatedUrl,
        *,
        timeout_seconds: float,
        proxy: str | None = None,
        impersonate: str | None = None,
    ) -> Mapping[str, Any]:
        """Extract metadata synchronously. Runs in a worker thread."""
        options = build_probe_options(
            self._settings,
            socket_timeout_seconds=min(timeout_seconds, self._settings.socket_timeout_seconds),
            proxy=proxy,
            impersonate=impersonate,
        )
        engine = self._factory(options)
        try:
            info = engine.extract_info(validated.value, download=False)
        except Exception as exc:
            raise classify(exc, url=validated.value) from exc
        finally:
            with contextlib.suppress(Exception):
                engine.close()

        if not info:
            message = f"'{validated.value}' returned no metadata"
            raise MetadataUnavailableError(message)
        return info

    # -- Fetching ------------------------------------------------------------

    def _blocking_fetch(
        self,
        request: DownloadRequest,
        validated: ValidatedUrl,
        workspace: WorkspaceScope,
        bridge: ProgressBridge,
        proxy: str | None = None,
        impersonate: str | None = None,
    ) -> Mapping[str, Any]:
        """Download synchronously. Runs in a worker thread."""
        options = build_download_options(
            self._settings,
            request,
            directory=workspace.directory(),
            format_expression=build_format_expression(request.selection),
            progress_hook=bridge.on_download_hook,
            postprocessor_hook=bridge.on_postprocessor_hook,
            proxy=proxy,
            impersonate=impersonate,
        )
        engine = self._factory(options)
        try:
            bridge.check_guards()
            info = engine.extract_info(validated.value, download=True)
        except (EngineAbort, SizeCeilingExceeded):
            raise
        except Exception as exc:
            signal = _find_control_signal(exc)
            if signal is not None:
                raise signal from exc
            raise classify(exc, url=validated.value) from exc
        finally:
            with contextlib.suppress(Exception):
                engine.close()

        if not info:
            message = f"'{validated.value}' produced no download"
            raise MetadataUnavailableError(message)
        return info

    # -- Thread bridging -----------------------------------------------------

    async def _run_guarded(
        self,
        work: Callable[[], Mapping[str, Any]],
        *,
        timeout_seconds: float | None,
        cancellation: CancellationToken | None,
        url: str,
    ) -> Mapping[str, Any]:
        """Run blocking engine work in a thread, honouring deadline and signals.

        The thread cannot be killed, so a timeout does not abandon it: the
        deadline is already known to the progress bridge, which raises inside
        the thread and lets yt-dlp clean up. This outer guard is the backstop
        for work that never reaches a hook at all - an extractor wedged on a
        socket, for instance.
        """
        _THREADS.started()
        task = asyncio.create_task(asyncio.to_thread(work))
        task.add_done_callback(_thread_finished)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
        except TimeoutError as exc:
            await self._drain(task, url=url, reason="timeout")
            message = f"'{url}' exceeded its {timeout_seconds:.0f}s budget"
            raise DownloadTimeoutError(message) from exc
        except asyncio.CancelledError:
            await self._drain(task, url=url, reason="cancelled")
            raise
        except EngineAbort as exc:
            raise self._from_abort(exc, url) from exc
        except SizeCeilingExceeded as exc:
            raise SizeLimitExceededError(exc.limit_bytes, exc.observed_bytes) from exc
        except DownloadError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise classify(exc, url=url) from exc
        finally:
            if cancellation is not None and cancellation.cancelled and not task.done():
                await self._drain(task, url=url, reason="cancelled")

    @staticmethod
    async def _drain(task: asyncio.Task[Mapping[str, Any]], *, url: str, reason: str) -> None:
        """Wait for an abandoned engine thread to unwind, and report if it does not.

        The drain is a budget, not a guarantee - nothing can force a Python
        thread to stop. What this adds is that giving up is *recorded*: the
        thread is still holding a descriptor and a pool slot, and a process that
        accumulates them stops being able to download without any single event
        looking like a failure.
        """
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), _THREAD_DRAIN_SECONDS)
        if task.done():
            return
        total = _THREADS.abandoned()
        logger.bind(url=url, reason=reason, abandoned_threads=total).error(
            "An engine thread did not unwind within its drain budget; it still holds "
            "a pool slot and its descriptors. Restart this worker before the pool "
            "is exhausted."
        )

    @staticmethod
    def _from_abort(abort: EngineAbort, url: str) -> DownloadError:
        """Convert an internal abort signal into the caller's typed error."""
        if abort.reason is CancellationReason.TIMEOUT:
            message = f"'{url}' exceeded its time budget"
            return DownloadTimeoutError(message)
        message = f"'{url}' was cancelled ({abort.reason.value})"
        return DownloadCancelledError(message)

    # -- Result assembly -----------------------------------------------------

    def _build_result(
        self,
        *,
        request: DownloadRequest,
        validated: ValidatedUrl,
        workspace: WorkspaceScope,
        info: Mapping[str, Any],
        existing: set[str],
        started_at: datetime,
        max_bytes: int | None,
        egress: EgressUsed | None = None,
    ) -> DownloadResult:
        """Verify what landed on disk and describe it."""
        used = egress or EgressUsed()
        metadata = to_metadata(info, url=validated.value, probed_at=started_at)
        self._enforce_source_policy(request, metadata)

        produced = [name for name in workspace.names() if name not in existing]
        self._reject_partials(produced, validated.value)
        produced = self._drop_leftover_partials(produced, workspace)
        if not produced:
            message = f"'{validated.value}' completed without producing a file"
            raise MetadataUnavailableError(message)

        primary_name = self._primary_name(info, produced)
        artifacts = tuple(
            workspace.artifact(name, role=self._role_for(name, primary_name)) for name in produced
        )
        total_bytes = sum(artifact.size_bytes for artifact in artifacts)
        self._verify(artifacts, total_bytes=total_bytes, max_bytes=max_bytes, url=validated.value)

        return DownloadResult(
            url=validated.value,
            provider=metadata.provider,
            artifacts=artifacts,
            metadata=metadata,
            selected_format=to_selected_format(info),
            total_bytes=total_bytes,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            resumed=request.resume and bool(existing),
            via_proxy=used.via_proxy,
            egress=used.label,
        )

    @staticmethod
    def _enforce_source_policy(request: DownloadRequest, metadata: MediaMetadata) -> None:
        """Refuse live sources and collections unless they were asked for."""
        if metadata.is_live and not request.allow_live:
            message = f"'{metadata.url}' is a live stream and live capture was not requested"
            raise LiveSourceNotAllowedError(message, provider=metadata.provider)
        if metadata.is_playlist and not request.allow_playlist:
            message = (
                f"'{metadata.url}' is a collection and collection downloads were not requested"
            )
            raise PlaylistNotAllowedError(
                message, entry_count=metadata.entry_count, provider=metadata.provider
            )

    @staticmethod
    def _reject_partials(produced: Sequence[str], url: str) -> None:
        """Fail when the engine left only partial files behind."""
        if produced and all(name.endswith(_PARTIAL_SUFFIXES) for name in produced):
            message = f"'{url}' produced only partial files"
            raise MetadataUnavailableError(message)

    @staticmethod
    def _drop_leftover_partials(produced: Sequence[str], workspace: WorkspaceScope) -> list[str]:
        """Remove part-files an earlier attempt left beside a finished download.

        A retried extraction resumes from, or abandons, the fragments of the
        attempt before it. Once a complete file exists those fragments are
        dead weight - and they were being counted: as sidecar artifacts, in
        ``total_bytes``, and against the size ceiling. They are deleted here
        so the result describes what will be delivered and nothing else.
        """
        leftovers = [name for name in produced if name.endswith(_PARTIAL_SUFFIXES)]
        if not leftovers:
            return list(produced)
        for name in leftovers:
            with contextlib.suppress(Exception):
                workspace.remove(name)
        logger.bind(count=len(leftovers)).debug(
            "Removed partial files left beside the finished download"
        )
        return [name for name in produced if name not in leftovers]

    @staticmethod
    def _primary_name(info: Mapping[str, Any], produced: Sequence[str]) -> str:
        """Return the name of the media file among everything produced.

        yt-dlp reports the file it wrote under ``requested_downloads``; when it
        does not, the largest produced file is the media and the small ones are
        thumbnails.
        """
        requested = info.get("requested_downloads")
        if isinstance(requested, list) and requested and isinstance(requested[0], dict):
            reported = requested[0].get("filepath") or requested[0].get("filename")
            if isinstance(reported, str):
                candidate = reported.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
                if candidate in produced:
                    return candidate
        media = [name for name in produced if not _looks_like_sidecar(name)]
        return media[0] if media else produced[0]

    @staticmethod
    def _role_for(name: str, primary_name: str) -> ArtifactRole:
        """Classify one produced file."""
        if name == primary_name:
            return ArtifactRole.PRIMARY
        lowered = name.lower()
        if any(lowered.endswith(extension) for extension in _IMAGE_EXTENSIONS):
            return ArtifactRole.THUMBNAIL
        if any(lowered.endswith(extension) for extension in _SUBTITLE_EXTENSIONS):
            return ArtifactRole.SUBTITLE
        return ArtifactRole.SIDECAR

    @staticmethod
    def _verify(
        artifacts: Sequence[ArtifactRef],
        *,
        total_bytes: int,
        max_bytes: int | None,
        url: str,
    ) -> None:
        """Check that what landed is usable and within budget.

        Containment is not re-checked here: every artifact reference was taken
        through the workspace, which refuses anything outside the lease.
        """
        primary = next(
            (artifact for artifact in artifacts if artifact.role is ArtifactRole.PRIMARY), None
        )
        if primary is None:  # pragma: no cover - _primary_name always assigns one
            message = f"'{url}' produced no primary artifact"
            raise MetadataUnavailableError(message)
        if primary.size_bytes <= 0:
            message = f"'{url}' produced an empty file"
            raise MetadataUnavailableError(message)
        if max_bytes is not None and total_bytes > max_bytes:
            raise SizeLimitExceededError(max_bytes, total_bytes)

    async def _inspect(
        self, result: DownloadResult, workspace: WorkspaceScope, *, url: str
    ) -> None:
        """Look inside the primary file and refuse it if it is not what was asked for.

        Size was checked already; this is the part the size cannot tell -
        whether the bytes decode, and whether there are as many seconds of them
        as the source promised. A skipped inspection (no tool) is logged, not
        failed: it is a fact about the device.
        """
        if self._inspector is None:
            return
        primary = result.primary
        report = await self._inspector.inspect(workspace.path_for(primary.name))
        if report is None:
            logger.bind(name=primary.name).debug("Completeness check skipped")
            return
        verify_complete(
            report,
            expected_seconds=result.metadata.duration_seconds,
            expect_video=not result.selected_format.is_audio_only,
            url=url,
        )
        logger.bind(
            name=primary.name,
            seconds=report.duration_seconds,
            declared=result.metadata.duration_seconds,
            video=list(report.video_codecs),
            audio=list(report.audio_codecs),
            size=f"{report.width}x{report.height}" if report.width else None,
        ).info("Delivered file verified complete")

    @staticmethod
    def _discard_new_files(workspace: WorkspaceScope, existing: set[str]) -> None:
        """Remove everything this attempt created, leaving the lease as found."""
        for name in workspace.names():
            if name not in existing:
                with contextlib.suppress(Exception):
                    workspace.remove(name)


def _first_entry_url(info: Mapping[str, Any]) -> str | None:
    """Return the page URL of a flat collection's first entry, if it names one."""
    entries = info.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("webpage_url", "url"):
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
        break
    return None


def _thread_finished(task: asyncio.Task[Mapping[str, Any]]) -> None:
    """Retire an engine thread from the ledger, whatever it ended as.

    Also *retrieves* the exception of a task nobody is waiting for any more. An
    abandoned task that later raises otherwise prints "exception was never
    retrieved" at collection time - noise that arrives hours after the event it
    describes, attached to nothing.
    """
    _THREADS.finished()
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.exception()


def _looks_like_sidecar(name: str) -> bool:
    """Return whether a produced file is auxiliary rather than the media."""
    lowered = name.lower()
    return any(
        lowered.endswith(extension)
        for extension in (*_IMAGE_EXTENSIONS, *_SUBTITLE_EXTENSIONS, ".json", ".description")
    )


def _find_control_signal(exc: BaseException) -> EngineAbort | SizeCeilingExceeded | None:
    """Return an internal control signal from an exception chain, if present.

    yt-dlp wraps exceptions raised inside progress hooks, so an abort can arrive
    as the ``__cause__`` of a ``DownloadError`` rather than on its own.
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (EngineAbort, SizeCeilingExceeded)):
            return current
        current = current.__cause__ or current.__context__
    return None
