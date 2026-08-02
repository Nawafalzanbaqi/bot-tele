"""The application configuration schema.

Every setting is a typed field, grouped into sections that mirror the parts of
the system they configure. Environment variables are prefixed with
``MEDIAHUB_`` and nested with a double underscore::

    MEDIAHUB_ENVIRONMENT=production
    MEDIAHUB_DATABASE__HOST=postgres
    MEDIAHUB_LOGGING__LEVEL=INFO

Three properties make this safe to rely on for years:

* **Immutable.** Every model is frozen, so no code path can mutate config at
  runtime and produce behaviour that depends on call order.
* **Strict.** ``extra="forbid"`` turns a typo in an environment variable into a
  startup failure instead of a silently ignored value.
* **Environment-aware.** :meth:`Settings.enforce_production_hardening` refuses
  to boot production with development defaults still in place.

Access settings through :func:`get_settings`, or - inside the layers - through
the container that received them, never by constructing ``Settings()`` ad hoc.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final
from urllib.parse import quote_plus

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULTS: Final[frozenset[str]] = frozenset(
    {"change-me", "change-me-in-production", "secret", "mediahub", "postgres"}
)
"""Placeholder secrets that must never reach a production deployment."""


class Environment(StrEnum):
    """Deployment environment, which drives defaults and hardening checks."""

    LOCAL = "local"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production(self) -> bool:
        """Return whether production-grade hardening rules apply."""
        return self is Environment.PRODUCTION


class LogLevel(StrEnum):
    """Severity threshold for log sinks, matching Loguru's built-in levels."""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class PersistenceBackend(StrEnum):
    """Which repository adapters the container wires up.

    Attributes:
        SQLITE: A single file on the device. **The production deployment**
            (ADR-0006): no second process, no administration, one file to back
            up, and a transactional enqueue for free because the queue shares
            the connection the repositories commit through.
        POSTGRES: SQLAlchemy adapters against PostgreSQL. Supported for a
            deployment that already runs one; unnecessary on an appliance, where
            it costs a container, a pool and 200 MB to store a few thousand rows.
        MEMORY: In-process adapters. Tests and throwaway demos only - all data
            is lost when the process exits.
    """

    SQLITE = "sqlite"
    POSTGRES = "postgres"
    MEMORY = "memory"


class _ConfigSection(BaseModel):
    """Base for configuration sections: frozen and intolerant of typos."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ApiSettings(_ConfigSection):
    """HTTP surface configuration.

    Attributes:
        host: Interface uvicorn binds to.
        port: Port uvicorn binds to.
        workers: Worker processes; keep at 1 behind an external supervisor.
        root_path: Mount prefix when served behind a reverse proxy.
        title: Name shown in the generated OpenAPI document.
        docs_enabled: Whether ``/docs`` and ``/openapi.json`` are served.
        cors_origins: Exact origins allowed to call the API from a browser.
    """

    host: str = "0.0.0.0"  # noqa: S104 - binding all interfaces is intended in a container
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1, le=32)
    root_path: str = ""
    title: str = "MediaHub API"
    docs_enabled: bool = True
    cors_origins: list[str] = Field(default_factory=list)


class DatabaseSettings(_ConfigSection):
    """Persistence configuration.

    Attributes:
        backend: Which repository adapters to wire up.
        sqlite_path: File holding the SQLite database, used when ``backend`` is
            ``sqlite``. Put it on durable storage, not on a tmpfs, and back up
            the file rather than copying it while it is live - a WAL database
            copied with ``cp`` is a corrupt database.
        host: Database host name.
        port: Database port.
        user: Role used to connect.
        password: Password for ``user``; never logged or serialised.
        name: Database name.
        pool_size: Connections kept open per process.
        max_overflow: Extra connections allowed under burst load.
        pool_timeout_seconds: How long to wait for a free connection.
        echo: Log every emitted statement. Debugging only - very noisy.
    """

    backend: PersistenceBackend = PersistenceBackend.SQLITE
    sqlite_path: Path = Path("/data/mediahub.db")
    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = "mediahub"
    password: SecretStr = SecretStr("mediahub")
    name: str = "mediahub"
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    echo: bool = False

    @property
    def dsn(self) -> str:
        """Return the async SQLAlchemy URL, with credentials URL-encoded."""
        user = quote_plus(self.user)
        password = quote_plus(self.password.get_secret_value())
        return f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Return the connection URL with the password masked, for logs."""
        return f"postgresql+asyncpg://{self.user}:***@{self.host}:{self.port}/{self.name}"

    @property
    def sqlite_url(self) -> str:
        """Return the async SQLAlchemy URL for :attr:`sqlite_path`."""
        return f"sqlite+aiosqlite:///{self.sqlite_path}"

    @property
    def migration_url(self) -> str:
        """Return the URL Alembic should connect to, for the chosen backend.

        Migrations must reach the database the application is about to use.
        Reading :attr:`dsn` unconditionally would point every ``alembic
        upgrade`` at PostgreSQL - including on the appliance, where the system
        of record is a file and the upgrade would silently apply somewhere else
        or refuse to connect at all.

        Raises:
            ValueError: If the backend keeps nothing to migrate.
        """
        if self.backend is PersistenceBackend.SQLITE:
            return self.sqlite_url
        if self.backend is PersistenceBackend.POSTGRES:
            return self.dsn
        message = f"the '{self.backend.value}' backend has no schema to migrate"
        raise ValueError(message)


class WorkspaceSettings(_ConfigSection):
    """Ephemeral scratch space used while a job is in flight.

    This is **not** a library. Everything written here is deleted when the work
    that created it reaches a terminal state, and whatever survives a crash is
    swept at startup.

    The two ceilings are separate on purpose. ``max_lease_bytes`` bounds the
    damage one runaway source can do; ``max_total_bytes`` bounds the damage
    several well-behaved ones can do at once
    (``docs/architecture/11-storage-strategy.md`` §11.7).

    Attributes:
        root: Directory containing one subdirectory per lease.
        min_free_bytes: Space that must remain free after any reservation. It is
            the floor that keeps the database able to commit the transaction
            recording whatever went wrong.
        max_lease_bytes: Largest a single lease may become, or ``None`` for no
            limit beyond the device. Enforced while writing, not by trusting a
            declared size.
        max_total_bytes: Largest the whole workspace may become, or ``None``
            when the workspace has a volume of its own and the device is the
            only limit.
        purge_on_start: Delete **every** lease during startup, on the grounds
            that anything present belongs to a process that no longer exists.
            Correct for a single-process deployment and exactly wrong for a
            root shared with another process, which is why it is a decision
            rather than an assumption.
        lease_expiry_seconds: How long a lease belonging to another process may
            sit untouched before a sweep treats it as abandoned. Long, because
            deleting a directory a healthy job is writing into is far worse
            than carrying its bytes for another sweep.
        adopt_own_leases_on_start: Keep leases left by a previous incarnation of
            this process instead of deleting them, so a resumed job can
            continue into the same directory.
    """

    root: Path = Path("/data/workspace")
    min_free_bytes: int = Field(default=1 * 1024**3, ge=0)
    max_lease_bytes: int | None = Field(default=8 * 1024**3, ge=1)
    max_total_bytes: int | None = Field(default=None, ge=1)
    purge_on_start: bool = True
    lease_expiry_seconds: float = Field(default=3600.0, ge=60.0)
    adopt_own_leases_on_start: bool = False

    @model_validator(mode="after")
    def check_ceilings_agree(self) -> WorkspaceSettings:
        """Refuse ceilings that can never both be satisfied.

        Returns:
            The validated settings.

        Raises:
            ValueError: If no lease could ever fit inside the workspace, which
                presents as every job failing on admission for no visible
                reason.
        """
        lease_ceiling = self.max_lease_bytes or 0
        total_ceiling = self.max_total_bytes or 0
        if total_ceiling and lease_ceiling > total_ceiling:
            message = (
                "workspace.max_lease_bytes must not exceed workspace.max_total_bytes; "
                "no lease would ever be admitted"
            )
            raise ValueError(message)
        return self


class DownloadSettings(_ConfigSection):
    """Download engine configuration.

    Defaults are chosen for a Raspberry Pi on domestic broadband: one fragment
    at a time, conservative timeouts, and a ceiling well below the device's
    disk.

    Attributes:
        enabled: Wire the real engine. When false the container installs the
            null adapter and the API reports the feature as unavailable.
        allow_merge: Permit fetching video and audio as separate streams and
            combining them. **Requires FFmpeg on the device** - the runtime
            image ships it. Turning this off does not disable the higher
            qualities, it makes them quietly resolve to the best already-muxed
            rendition instead, which on most platforms is 720p. The cost is CPU:
            on a Pi a long 1080p merge is minutes of it.
        prefer_compatible_codecs: Prefer H.264 video and AAC audio over the
            newest codecs a platform offers. **Leave this on for any destination
            people watch things in.** The best streams are increasingly AV1 or
            VP9 with Opus, which are smaller for the same resolution and which
            phone players and chat clients largely cannot decode - so the file
            arrives, is the right resolution, and does not play. It also keeps
            the merge a remux rather than a re-encode, which on a Pi is seconds
            instead of minutes.
        max_item_bytes: Hard ceiling per download, enforced while streaming.
        probe_timeout_seconds: Budget for a metadata probe.
        download_timeout_seconds: Wall-clock budget for one download.
        socket_timeout_seconds: Per-connection read timeout, which is what turns
            a stalled source into a failure instead of a hung worker.
        probe_attempts: Probe attempts, including the first. Probes are cheap and
            idempotent, so retrying them inside the engine is safe; downloads are
            retried by the queue instead, where the budget is visible.
        probe_backoff_seconds: Base delay between probe attempts.
        download_attempts: Download attempts, including the first, for failures
            classified transient.

            **A probe succeeding does not mean the download will.** Extraction
            runs again at the start of a download, and some extractors fail a
            noticeable fraction of the time for no reason the caller can see -
            TikTok's web path measured 6 successes in 8 from this device. With
            no retry here, a quarter of otherwise-valid links failed outright,
            which is exactly the "some work and some do not" that is impossible
            to diagnose from the outside.

            Cheap where it matters: an extraction failure has transferred
            nothing, and a failure later resumes rather than restarting, because
            partial files are left in the lease between attempts.
        retries: Engine-level retries for a whole download.
        fragment_retries: Retries for one fragment of a fragmented format.
        extractor_retries: Retries while extracting metadata, **inside a single
            engine call**. This is the one that covers a download as well as a
            probe, and it is why the default is not low.

            Extraction is where an intermittent refusal lands: a site answers
            403 to one request and serves the next, which happens routinely
            when the request leaves through a shared egress address. The outer
            ``probe_attempts`` only ever protected probing, so a refusal met
            while *downloading* reached the user as a flat failure for
            something that would have worked seconds later.
        concurrent_fragments: Fragments fetched in parallel. More than one
            rarely helps on a single-core-bound device.
        progress_interval_seconds: Shortest gap between progress callbacks.
        rate_limit_bytes_per_second: Optional bandwidth cap, so a download does
            not saturate a household connection.
        user_agent: Optional override for outbound requests.
        cookies_file: Netscape-format cookie jar presented to sources, or
            ``None`` to browse anonymously. Several platforms - X most visibly -
            now return "no video in this post" to an anonymous session for
            content a logged-in one can see, so without this they are simply
            unavailable rather than broken.

            **This file is a set of live session credentials**, equivalent to
            being logged in as whoever exported it. Mount it read-only, keep it
            at mode 600, and export from an account whose loss would be an
            inconvenience rather than a catastrophe.
        proxy: Optional proxy for every outbound request, e.g.
            ``socks5://127.0.0.1:1080`` or ``http://gateway:3128``.

            This is the only setting that answers a **network-level block**,
            which is a distinct failure from anything else in this module and
            looks nothing like it: DNS resolves, the TCP connection opens, and
            the TLS handshake is then reset by something in the path that read
            the hostname. No cookie, retry or engine update changes that,
            because the traffic never reaches the site. Sending it through a
            proxy on the far side of the filter is what changes it.
        proxy_hosts: Hosts to send through ``proxy`` from the first attempt.
            Comma-separated, and matching covers subdomains.

            **Empty is a working configuration, not an unfinished one.** With no
            list, everything is fetched directly and the proxy is used only for
            a host that has actually proved it needs one - a connection that
            opened and was reset. That host is then remembered for the life of
            the process. Listing a host here only skips the one fast failure
            that teaches the same lesson.

            The default of routing nothing is deliberate. A tunnel is slower
            than the direct path and is usually metered, and most sources do not
            need it; sending everything through one would make every download
            worse to fix a few.
    """

    enabled: bool = False
    allow_merge: bool = True
    prefer_compatible_codecs: bool = True
    max_item_bytes: int = Field(default=2 * 1024**3, ge=1)
    probe_timeout_seconds: float = Field(default=30.0, gt=0)
    download_timeout_seconds: float = Field(default=3600.0, gt=0)
    socket_timeout_seconds: float = Field(default=30.0, gt=0)
    probe_attempts: int = Field(default=4, ge=1, le=10)
    probe_backoff_seconds: float = Field(default=3.0, ge=0)
    download_attempts: int = Field(default=3, ge=1, le=6)
    retries: int = Field(default=3, ge=0, le=20)
    fragment_retries: int = Field(default=5, ge=0, le=50)
    extractor_retries: int = Field(default=5, ge=0, le=10)
    concurrent_fragments: int = Field(default=1, ge=1, le=8)
    progress_interval_seconds: float = Field(default=0.5, ge=0)
    rate_limit_bytes_per_second: int | None = Field(default=None, ge=1)
    user_agent: str | None = None
    cookies_file: Path | None = None
    proxy: str | None = None
    proxy_hosts: tuple[str, ...] = ()

    @field_validator("proxy_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        """Accept a comma-separated list, not only JSON.

        This one is edited by hand in a ``.env`` file more often than any other
        setting here, and ``["a.com","b.com"]`` is an unkind thing to ask
        someone to type correctly at a shell prompt.
        """
        if isinstance(value, str):
            return [part.strip().lower().lstrip(".") for part in value.split(",") if part.strip()]
        return value


class WorkerSettings(_ConfigSection):
    """The worker process: how much it runs at once, and how it is noticed.

    Defaults are sized for a Raspberry Pi and for the failure everyone actually
    hits: a lease that expires while the job is still healthy. The validator
    below refuses that configuration rather than letting it present as jobs
    mysteriously running twice.

    Attributes:
        enabled: Run the worker. Off by default, so an instance that has not
            been configured for execution simply does not claim work.
        role: The role half of the worker's identity, e.g. ``worker`` or
            ``delivery``. Part of ``host:role:index``.
        index: Which slot of that role on this host. Two workers on one machine
            must differ here, or they share an identity and release each other's
            leases at startup.
        host: Identity override. Defaults to the machine's hostname, which is
            what makes the identity stable across restarts.
        slots: Jobs executed concurrently by this process. One is right for a
            bandwidth-bound device; a second slot mostly halves both.
        lease_seconds: How long a claim is owned before it may be reclaimed. The
            recovery time after a hard crash is at most two of these.
        heartbeat_seconds: Gap between lease renewals. Each renewal also reads
            the cancellation flag, so this is the worst-case cancellation
            latency.
        idle_poll_seconds: First wait after finding nothing to do.
        max_idle_poll_seconds: Ceiling that wait backs off to. On a small device
            a fast poll loop is a measurable, pointless power draw.
        drain_grace_seconds: How long a stage is allowed to finish after a stop
            is requested. Must be **less** than the orchestrator's kill timeout,
            or graceful shutdown never actually runs.
        progress_interval_seconds: Shortest gap between durable progress writes.
            Writing every callback would destroy an SD card.
        progress_percent_step: Progress movement that forces a write regardless
            of the interval, so a fast download still reports smoothly.
        recover_own_leases_on_start: Release leases held by this identity at
            startup. A restarted worker would otherwise wait a full lease period
            to recover work it was already doing.
        reclaim_expired_leases_on_start: Also take back leases whose owner
            stopped reporting. Belongs to the scheduler in the long run; running
            it at startup means a single-worker deployment still recovers.
    """

    enabled: bool = False
    role: str = "worker"
    index: int = Field(default=0, ge=0, le=64)
    host: str | None = None
    slots: int = Field(default=1, ge=1, le=16)
    lease_seconds: float = Field(default=120.0, ge=5.0, le=3600.0)
    heartbeat_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    idle_poll_seconds: float = Field(default=1.0, ge=0.05, le=60.0)
    max_idle_poll_seconds: float = Field(default=5.0, ge=0.05, le=300.0)
    drain_grace_seconds: float = Field(default=30.0, ge=0.0, le=600.0)
    progress_interval_seconds: float = Field(default=5.0, ge=0.0, le=300.0)
    progress_percent_step: float = Field(default=5.0, ge=0.0, le=100.0)
    recover_own_leases_on_start: bool = True
    reclaim_expired_leases_on_start: bool = True

    @model_validator(mode="after")
    def check_timings_are_survivable(self) -> WorkerSettings:
        """Refuse timings that would lose leases on healthy jobs.

        A heartbeat slower than the lease means every job is reclaimed while it
        is still running - the worst possible failure, because it looks like
        random duplicate execution rather than like a misconfiguration.

        Returns:
            The validated settings.

        Raises:
            ValueError: If the heartbeat cannot keep a lease alive, or if the
                idle backoff cannot grow.
        """
        problems: list[str] = []
        if self.heartbeat_seconds * 2 > self.lease_seconds:
            problems.append(
                "worker.heartbeat_seconds must be at most half of worker.lease_seconds, "
                "so a single missed renewal does not lose the lease"
            )
        if self.max_idle_poll_seconds < self.idle_poll_seconds:
            problems.append(
                "worker.max_idle_poll_seconds must be at least worker.idle_poll_seconds"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class DeliverySettings(_ConfigSection):
    """Which destinations are installed, and how they are chosen.

    Attributes:
        default_provider: Destination used when a caller needs a ceiling before
            it has a target - to decide what qualities to offer, and to cap a
            download at something that can actually be sent.
        enable_dummy: Register the discard destination. Useful for a dry run,
            and for an instance with nothing else configured.
        dummy_priority: Its selection priority. Below Telegram by default, so
            installing it never quietly diverts real deliveries.
        telegram_priority: Telegram's selection priority.
        failure_threshold: Consecutive transient failures after which a
            provider is deprioritised in favour of an alternative.
        cooldown_seconds: How long it stays deprioritised.
    """

    default_provider: str = "telegram"
    enable_dummy: bool = False
    dummy_priority: int = Field(default=10, ge=0, le=1000)
    telegram_priority: int = Field(default=100, ge=0, le=1000)
    failure_threshold: int = Field(default=3, ge=1, le=20)
    cooldown_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)


class TelegramSettings(_ConfigSection):
    """The Telegram gateway and delivery destination.

    Attributes:
        enabled: Run the gateway. Off by default, so an instance with no bot
            configured simply has no Telegram surface.
        bot_token: The bot's credential. Never logged, never persisted, never
            placed in a subprocess environment.
        api_base_url: Base URL of a self-hosted Bot API server. Running one
            raises the upload ceiling from 50 MB to about 2 GB.
        owner_ids: Telegram user ids with full access.
        member_ids: Telegram user ids that may fetch and read their history.
        readonly_ids: Telegram user ids that may only look.
        max_concurrent_acquisitions: How many downloads may run at once.
            One is right for a bandwidth-bound device that shares its CPU
            with other things; excess requests wait rather than failing.
        auto_best_quality: Start fetching immediately at the best quality
            the destination will accept, instead of posting a keyboard and
            waiting. The menu is a tap that delays every download to answer
            a question whose answer is nearly always the same one; turn this
            off where the choice is genuinely wanted.
        poll_timeout_seconds: Long-poll duration.
        progress_interval_seconds: Shortest gap between progress edits. Telegram
            tolerates roughly one edit per second per chat; three is polite.
        history_limit: Entries shown by ``/history``.
        session_ttl_seconds: How long a posted set of quality buttons stays
            valid. A button tapped after this fails cleanly instead of acting
            on a stale request.
    """

    enabled: bool = False
    bot_token: SecretStr = SecretStr("")
    api_base_url: str | None = None
    owner_ids: list[str] = Field(default_factory=list)
    member_ids: list[str] = Field(default_factory=list)
    readonly_ids: list[str] = Field(default_factory=list)
    auto_best_quality: bool = True
    max_concurrent_acquisitions: int = Field(default=1, ge=1, le=8)
    poll_timeout_seconds: int = Field(default=30, ge=1, le=120)
    progress_interval_seconds: float = Field(default=3.0, ge=0.5, le=60.0)
    history_limit: int = Field(default=25, ge=1, le=50)
    session_ttl_seconds: float = Field(default=900.0, ge=30.0)

    @property
    def uses_local_api_server(self) -> bool:
        """Return whether a self-hosted Bot API server is configured."""
        return bool(self.api_base_url)

    @property
    def bot_id(self) -> str:
        """Return the bot's numeric id, taken from the token's prefix.

        Recorded on every delivery receipt because a Telegram file reference is
        usable only by the bot that created it. Deriving it from the token
        rather than calling the API keeps startup offline, and the prefix is
        not the secret half.
        """
        raw = self.bot_token.get_secret_value()
        return raw.split(":", maxsplit=1)[0] if ":" in raw else "unknown"

    @model_validator(mode="after")
    def check_gateway_is_usable(self) -> TelegramSettings:
        """Refuse a gateway that is switched on but cannot work.

        Both failures below are configuration typos that would otherwise
        present as "the bot ignores me", which is a miserable thing to debug.

        Returns:
            The validated settings.

        Raises:
            ValueError: If enabled without a token, or without anyone allowed.
        """
        if not self.enabled:
            return self
        if not self.bot_token.get_secret_value().strip():
            message = "telegram.enabled is true but no bot token is configured"
            raise ValueError(message)
        if not (self.owner_ids or self.member_ids or self.readonly_ids):
            message = (
                "telegram.enabled is true but the allow list is empty; "
                "nobody would be able to use the bot"
            )
            raise ValueError(message)
        return self


class StorageSettings(_ConfigSection):
    """Filesystem layout of the media library.

    Deprecated. ``library_path`` describes a permanent library, which the
    product does not have - media is deleted once delivery is proven. Use
    :class:`WorkspaceSettings` for scratch space. This section is retained so
    existing deployments keep booting and is removed by the custody rework.

    Attributes:
        library_path: Root of the permanent library. Storage keys are relative
            to this directory.
        staging_path: Scratch area for in-progress artefacts, moved into the
            library only once complete.
        max_item_size_bytes: Refuse to store anything larger. Defaults to 20 GiB.
    """

    library_path: Path = Path("/data/library")
    staging_path: Path = Path("/data/staging")
    max_item_size_bytes: int = Field(default=20 * 1024**3, ge=1)


class LoggingSettings(_ConfigSection):
    """Log sink configuration.

    Attributes:
        level: Minimum severity that reaches the sink.
        json_format: Emit newline-delimited JSON instead of coloured text.
            Enable wherever logs are shipped to a collector.
        backtrace: Include the full call stack for exceptions.
        diagnose: Include variable values in tracebacks. **Never enable in
            production** - it prints secrets held in local variables.
    """

    level: LogLevel = LogLevel.INFO
    json_format: bool = False
    backtrace: bool = False
    diagnose: bool = False


class SecuritySettings(_ConfigSection):
    """Secrets and transport-level guards.

    Attributes:
        secret_key: Key for signing anything the app issues. Rotating it
            invalidates everything signed with the previous value.
        allowed_hosts: Host header allow-list; ``["*"]`` disables the check.
        block_private_networks: Refuse to fetch URLs that resolve to loopback,
            private, link-local, multicast or reserved addresses. **Leave this
            on.** Turning it off lets a submitted link reach anything on the
            household network, including the router and cloud metadata
            endpoints.
    """

    secret_key: SecretStr = SecretStr("change-me-in-production")
    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])
    block_private_networks: bool = True


class Settings(BaseSettings):
    """The complete, validated configuration of a MediaHub process.

    Attributes:
        environment: Which deployment this process represents.
        debug: Verbose error output. Forced off in production.
        api: HTTP surface configuration.
        database: Persistence configuration.
        storage: Deprecated media library layout.
        workspace: Ephemeral scratch space.
        download: Download engine configuration.
        worker: The job-executing process.
        delivery: Destination registry and selection.
        telegram: Telegram gateway and destination.
        logging: Log sink configuration.
        security: Secrets and transport guards.
    """

    model_config = SettingsConfigDict(
        env_prefix="MEDIAHUB_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.LOCAL
    debug: bool = False

    api: Annotated[ApiSettings, Field(default_factory=ApiSettings)]
    database: Annotated[DatabaseSettings, Field(default_factory=DatabaseSettings)]
    storage: Annotated[StorageSettings, Field(default_factory=StorageSettings)]
    workspace: Annotated[WorkspaceSettings, Field(default_factory=WorkspaceSettings)]
    download: Annotated[DownloadSettings, Field(default_factory=DownloadSettings)]
    worker: Annotated[WorkerSettings, Field(default_factory=WorkerSettings)]
    delivery: Annotated[DeliverySettings, Field(default_factory=DeliverySettings)]
    telegram: Annotated[TelegramSettings, Field(default_factory=TelegramSettings)]
    logging: Annotated[LoggingSettings, Field(default_factory=LoggingSettings)]
    security: Annotated[SecuritySettings, Field(default_factory=SecuritySettings)]

    @model_validator(mode="after")
    def enforce_production_hardening(self) -> Settings:
        """Refuse to boot production with development defaults in place.

        Failing at startup is deliberate: a misconfigured production process
        that *runs* is far more dangerous than one that never starts.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If a placeholder secret, a debug flag or the in-memory
                backend is still configured in production.
        """
        if not self.environment.is_production:
            return self

        problems: list[str] = []
        if self.security.secret_key.get_secret_value() in INSECURE_DEFAULTS:
            problems.append("security.secret_key is still a placeholder")
        if (
            self.database.backend is PersistenceBackend.POSTGRES
            and self.database.password.get_secret_value() in INSECURE_DEFAULTS
        ):
            problems.append("database.password is still a placeholder")
        if self.database.backend is PersistenceBackend.MEMORY:
            problems.append("database.backend=memory loses all data on restart")
        if self.debug:
            problems.append("debug must be disabled")
        if self.logging.diagnose:
            problems.append("logging.diagnose leaks variable values into logs")

        if problems:
            joined = "; ".join(problems)
            msg = f"Refusing to start in production: {joined}."
            raise ValueError(msg)
        return self

    @property
    def is_debug(self) -> bool:
        """Return whether verbose diagnostics may be exposed."""
        return self.debug and not self.environment.is_production


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, reading the environment once.

    The result is cached for the lifetime of the process. Tests that need a
    different configuration should build a :class:`Settings` instance directly
    and inject it, or call ``get_settings.cache_clear()`` first.
    """
    return Settings()
