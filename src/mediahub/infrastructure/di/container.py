"""Dependency wiring for the whole application.

The container is built once per process, from :class:`Settings`, and holds one
instance of every adapter. Use cases are cheap objects, so they are constructed
on demand by the ``*_use_case`` factory methods rather than cached - that keeps
them stateless and free of accidental sharing between requests.

Two things this module deliberately does not do:

* **No magic.** No decorators, no auto-discovery, no service locator. Wiring is
  a readable function; if you want to know what satisfies a port, you read it.
* **No global.** Nothing here is a module-level singleton. The API stores its
  container on ``app.state``; tests build their own. Anything that reaches for
  a global would make the two impossible to run side by side.

The type annotations do real work here: each field is annotated with its
*port*, so mypy verifies that the adapter assigned to it actually satisfies
the protocol. A mismatch is a build failure, not a 3 a.m. ``AttributeError``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import text

from mediahub.application.access.use_cases.authorize_principal import AuthorizePrincipal
from mediahub.application.credentials.use_cases.manage_cookies import (
    DescribeCookies,
    DiscardCookies,
    InstallCookies,
)
from mediahub.application.download.use_cases.acknowledge_cancellation import (
    AcknowledgeCancellation,
)
from mediahub.application.download.use_cases.acquire_media import AcquireMedia
from mediahub.application.download.use_cases.cancel_download_job import CancelDownloadJob
from mediahub.application.download.use_cases.checkpoint_job import CheckpointJob
from mediahub.application.download.use_cases.claim_job import ClaimJob
from mediahub.application.download.use_cases.complete_job import CompleteJob
from mediahub.application.download.use_cases.describe_capabilities import (
    DescribeCapabilities,
)
from mediahub.application.download.use_cases.fail_job import FailJob
from mediahub.application.download.use_cases.get_download_job import GetDownloadJob
from mediahub.application.download.use_cases.get_history import GetHistory
from mediahub.application.download.use_cases.heartbeat_job import HeartbeatJob
from mediahub.application.download.use_cases.list_download_jobs import ListDownloadJobs
from mediahub.application.download.use_cases.probe_source import ProbeSource
from mediahub.application.download.use_cases.recover_leases import RecoverLeases
from mediahub.application.download.use_cases.release_job import ReleaseJob
from mediahub.application.download.use_cases.report_job_progress import ReportJobProgress
from mediahub.application.download.use_cases.request_download import RequestDownload
from mediahub.application.media.use_cases.archive_media import ArchiveMedia
from mediahub.application.media.use_cases.get_media import GetMedia
from mediahub.application.media.use_cases.list_media import ListMedia
from mediahub.application.media.use_cases.register_media import RegisterMedia
from mediahub.application.workspace.use_cases.recover_workspaces import RecoverWorkspaces
from mediahub.domain.access.policies import AllowListPolicy, AuthorizationPolicy
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.domain.workspace.policies import RecoveryPolicy
from mediahub.infrastructure.credentials.filesystem_store import FilesystemCookieStore
from mediahub.infrastructure.delivery.dummy.provider import DummyDeliveryProvider
from mediahub.infrastructure.delivery.registry import (
    DEFAULT_PRIORITY,
    DeliveryProviderRegistry,
    ProviderRegistration,
)
from mediahub.infrastructure.download.composite import CompositeDownloader
from mediahub.infrastructure.download.gallerydl.downloader import GalleryDlDownloader
from mediahub.infrastructure.download.ytdlp.downloader import (
    YtDlpDownloader,
    engine_thread_stats,
)
from mediahub.infrastructure.download.ytdlp.inspection import FfprobeInspector
from mediahub.infrastructure.downloader.null_downloader import NullDownloader
from mediahub.infrastructure.messaging.logging_event_publisher import LoggingEventPublisher
from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
from mediahub.infrastructure.persistence.memory.journal import InMemoryAcquisitionJournal
from mediahub.infrastructure.persistence.memory.queue import InMemoryJobQueue
from mediahub.infrastructure.persistence.sqlalchemy.engine import Database
from mediahub.infrastructure.persistence.sqlalchemy.unit_of_work import (
    SqlAlchemyUnitOfWorkFactory,
)
from mediahub.infrastructure.persistence.sqlite.engine import SqliteDatabase
from mediahub.infrastructure.persistence.sqlite.journal import SqliteAcquisitionJournal
from mediahub.infrastructure.persistence.sqlite.queue import SqliteJobQueue
from mediahub.infrastructure.security.address_guard import DnsAddressGuard
from mediahub.infrastructure.security.audit_sink import LoggingAuditSink
from mediahub.infrastructure.system.clock import SystemClock
from mediahub.infrastructure.system.id_generator import Uuid4Generator
from mediahub.infrastructure.workspace.disk import WorkspaceLimits
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import PersistenceBackend

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.access.ports import AuditSink
    from mediahub.application.common.ports import Clock, EventPublisher, UuidGenerator
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.credentials.ports import CookieStore
    from mediahub.application.delivery.ports import DeliveryProvider, DeliveryRouter
    from mediahub.application.download.journal import AcquisitionJournal
    from mediahub.application.download.ports import DownloaderPort, EgressRoutes
    from mediahub.application.download.queue import JobQueue
    from mediahub.application.workspace.ports import WorkspacePort
    from mediahub.shared.config.settings import Settings


@dataclass(frozen=True, slots=True)
class Container:
    """Every adapter the application needs, wired and ready.

    Attributes:
        settings: The configuration this container was built from.
        clock: Source of the current time.
        uuid_generator: Source of new identifiers.
        event_publisher: Sink for domain events.
        unit_of_work: Factory producing one transaction per use case.
        downloader: The download engine, or a null object when disabled.
        workspace: Ephemeral scratch space for in-flight work, or ``None`` when
            no engine is wired and nothing can write anything.
        database: The SQLAlchemy engine holder, or ``None`` for the in-memory
            backend.
        job_queue: The lease-bearing work queue a worker claims from, or
            ``None`` when the configured backend has no queue adapter yet.
    """

    settings: Settings
    clock: Clock
    uuid_generator: UuidGenerator
    event_publisher: EventPublisher
    unit_of_work: UnitOfWorkFactory
    downloader: DownloaderPort
    workspace: WorkspacePort | None = None
    database: Database | SqliteDatabase | None = None
    job_queue: JobQueue | None = None
    journal: AcquisitionJournal = field(default_factory=InMemoryAcquisitionJournal)
    audit: AuditSink = field(default_factory=LoggingAuditSink)
    cookies: CookieStore = field(default_factory=lambda: FilesystemCookieStore(None))
    allow_list: AllowListPolicy = field(default_factory=AllowListPolicy)
    authorization: AuthorizationPolicy = field(default_factory=AuthorizationPolicy)
    egress: EgressRoutes | None = None
    """The engine's egress routes, for the operator's /vpn; ``None`` without an engine."""

    # -- Media use cases -----------------------------------------------------

    def register_media_use_case(self) -> RegisterMedia:
        """Build the "catalogue a new item" use case."""
        return RegisterMedia(
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            uuid_generator=self.uuid_generator,
            event_publisher=self.event_publisher,
        )

    def get_media_use_case(self) -> GetMedia:
        """Build the "read one item" use case."""
        return GetMedia(unit_of_work=self.unit_of_work)

    def list_media_use_case(self) -> ListMedia:
        """Build the "browse the catalogue" use case."""
        return ListMedia(unit_of_work=self.unit_of_work)

    def archive_media_use_case(self) -> ArchiveMedia:
        """Build the "retire an item" use case."""
        return ArchiveMedia(
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
        )

    # -- Download use cases --------------------------------------------------

    def request_download_use_case(self) -> RequestDownload:
        """Build the "queue an acquisition" use case."""
        return RequestDownload(
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            uuid_generator=self.uuid_generator,
            event_publisher=self.event_publisher,
        )

    def get_download_job_use_case(self) -> GetDownloadJob:
        """Build the "read one job" use case."""
        return GetDownloadJob(unit_of_work=self.unit_of_work)

    def list_download_jobs_use_case(self) -> ListDownloadJobs:
        """Build the "browse the queue" use case."""
        return ListDownloadJobs(unit_of_work=self.unit_of_work)

    def cancel_download_job_use_case(self) -> CancelDownloadJob:
        """Build the "stop a job" use case."""
        return CancelDownloadJob(
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
        )

    # -- Worker use cases ----------------------------------------------------
    #
    # Everything a worker process is allowed to call. They are built here, one
    # per intention, so that the worker itself receives finished use cases and
    # has no way to reach a repository, an engine or a rule.

    def claim_job_use_case(self) -> ClaimJob:
        """Build the "take the next due job" use case."""
        return ClaimJob(
            queue=self._require_queue(),
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
            lease_seconds=self.settings.worker.lease_seconds,
        )

    def heartbeat_job_use_case(self) -> HeartbeatJob:
        """Build the "keep the lease, read the cancellation flag" use case."""
        return HeartbeatJob(
            queue=self._require_queue(),
            clock=self.clock,
            lease_seconds=self.settings.worker.lease_seconds,
        )

    def checkpoint_job_use_case(self) -> CheckpointJob:
        """Build the "record that a stage finished" use case."""
        return CheckpointJob(queue=self._require_queue(), clock=self.clock)

    def report_job_progress_use_case(self) -> ReportJobProgress:
        """Build the "publish an observation" use case."""
        return ReportJobProgress(
            queue=self._require_queue(),
            unit_of_work=self.unit_of_work,
            clock=self.clock,
        )

    def complete_job_use_case(self) -> CompleteJob:
        """Build the "this attempt succeeded" use case."""
        return CompleteJob(
            queue=self._require_queue(),
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
        )

    def fail_job_use_case(self) -> FailJob:
        """Build the "this attempt failed; decide what happens" use case."""
        return FailJob(
            queue=self._require_queue(),
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
        )

    def release_job_use_case(self) -> ReleaseJob:
        """Build the "we are stopping; take this back" use case."""
        return ReleaseJob(
            queue=self._require_queue(),
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
        )

    def acknowledge_cancellation_use_case(self) -> AcknowledgeCancellation:
        """Build the "it stopped because it was asked to" use case."""
        return AcknowledgeCancellation(
            queue=self._require_queue(),
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
        )

    def recover_leases_use_case(self) -> RecoverLeases:
        """Build the "take back work whose owner is gone" use case."""
        return RecoverLeases(
            queue=self._require_queue(),
            unit_of_work=self.unit_of_work,
            clock=self.clock,
            event_publisher=self.event_publisher,
        )

    def _require_queue(self) -> JobQueue:
        """Return the queue adapter, or explain why there is not one.

        Raises:
            RuntimeError: If the configured persistence backend has no queue
                adapter. A worker cannot claim from a queue that does not exist,
                and failing here names the reason instead of producing an
                ``AttributeError`` three frames away.
        """
        if self.job_queue is None:
            message = (
                "no job queue is configured for this persistence backend; "
                "the worker has nothing to claim from"
            )
            raise RuntimeError(message)
        return self.job_queue

    # -- Acquisition use cases -----------------------------------------------

    def authorize_principal_use_case(self) -> AuthorizePrincipal:
        """Build the "may this caller do this?" use case."""
        return AuthorizePrincipal(
            allow_list=self.allow_list,
            authorization=self.authorization,
            audit=self.audit,
            clock=self.clock,
        )

    def probe_source_use_case(self) -> ProbeSource:
        """Build the "what is at this link?" use case."""
        return ProbeSource(
            downloader=self.downloader,
            max_bytes=self.settings.download.max_item_bytes,
            allow_merge=self.settings.download.allow_merge,
            prefer_compatible=self.settings.download.prefer_compatible_codecs,
        )

    def acquire_media_use_case(self, delivery: DeliveryRouter) -> AcquireMedia:
        """Build the "fetch it and send it" use case.

        The router is supplied by the caller rather than held on the container:
        a delivery provider often owns a client with an async lifecycle, and
        that belongs to whichever entry point started it.

        Raises:
            RuntimeError: If no workspace is configured, which means the engine
                is disabled and nothing could be written anyway.
        """
        if self.workspace is None:
            message = "no workspace is configured; enable the download engine first"
            raise RuntimeError(message)
        return AcquireMedia(
            downloader=self.downloader,
            delivery=delivery,
            workspace=self.workspace,
            journal=self.journal,
            clock=self.clock,
            max_item_bytes=self.settings.download.max_item_bytes,
            allow_merge=self.settings.download.allow_merge,
            prefer_compatible=self.settings.download.prefer_compatible_codecs,
        )

    def delivery_router(self, *providers: DeliveryProvider) -> DeliveryProviderRegistry:
        """Build the router over whichever providers the entry point started.

        Priorities and the default destination come from configuration, so a
        deployment can install a destination, disable one, or change which is
        preferred without a code change.
        """
        priorities = {
            "telegram": self.settings.delivery.telegram_priority,
            "dummy": self.settings.delivery.dummy_priority,
        }
        registrations = [
            ProviderRegistration(
                provider=provider,
                priority=priorities.get(provider.name, DEFAULT_PRIORITY),
            )
            for provider in providers
        ]
        if self.settings.delivery.enable_dummy and not any(
            provider.name == "dummy" for provider in providers
        ):
            registrations.append(
                ProviderRegistration(
                    provider=DummyDeliveryProvider(),
                    priority=self.settings.delivery.dummy_priority,
                )
            )
        return DeliveryProviderRegistry(
            registrations=registrations,
            default_provider=self.settings.delivery.default_provider,
            failure_threshold=self.settings.delivery.failure_threshold,
            cooldown_seconds=self.settings.delivery.cooldown_seconds,
        )

    def install_cookies_use_case(self) -> InstallCookies:
        """Build the "replace the jar the engine presents" use case."""
        return InstallCookies(store=self.cookies)

    def describe_cookies_use_case(self) -> DescribeCookies:
        """Build the "what is installed?" use case."""
        return DescribeCookies(store=self.cookies)

    def discard_cookies_use_case(self) -> DiscardCookies:
        """Build the "browse anonymously again" use case."""
        return DiscardCookies(store=self.cookies)

    def get_history_use_case(self) -> GetHistory:
        """Build the "what have I fetched?" use case."""
        return GetHistory(journal=self.journal)

    def describe_capabilities_use_case(self, delivery: DeliveryRouter) -> DescribeCapabilities:
        """Build the "what can this instance do?" use case."""
        return DescribeCapabilities(
            downloader=self.downloader,
            delivery=delivery,
            max_item_bytes=self.settings.download.max_item_bytes,
            allow_live=False,
            allow_playlist=False,
        )

    # -- Lifecycle -----------------------------------------------------------

    async def prepare(self) -> None:
        """Make the store ready to be used. Safe to call twice.

        Only SQLite does anything here, and it does two things: create any
        missing tables, so a fresh device boots into a working system without a
        separate migrate step; and *verify* that the pragmas took effect, since
        a foreign-key pragma that silently failed to apply is precisely the
        class of defect the SQLite adapter exists to prevent.
        """
        if isinstance(self.database, SqliteDatabase):
            await self.database.create_schema()
            await self.database.verify_pragmas()
        await self._prune_history()

    async def _prune_history(self) -> None:
        """Forget delivered-file history older than the configured retention.

        Done at start-up rather than on a timer because the process restarts
        weekly with the engine refresh anyway, and a bound that is applied
        every few days is a bound. A failure here is logged, not fatal: stale
        history is not a reason to refuse to start.
        """
        cutoff = self.clock.now() - timedelta(days=self.settings.database.journal_retention_days)
        try:
            removed = await self.journal.prune(before=cutoff)
        except Exception:
            logger.exception("Could not prune delivered-file history")
            return
        if removed:
            logger.bind(removed=removed, older_than=cutoff.date().isoformat()).info(
                "Pruned delivered-file history"
            )

    async def check_database(self) -> bool:
        """Return whether the persistence backend answers.

        Used by the readiness probe. The in-memory backend is always ready;
        PostgreSQL is asked for a trivial round trip so a broken pool shows up
        as "not ready" rather than as failing requests.
        """
        if self.database is None:
            return True
        try:
            async with self.database.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Database readiness check failed")
            return False
        return True

    def check_workspace(self) -> bool:
        """Return whether the workspace can still be written to.

        Distinguishes the two conditions that look identical from outside and
        need opposite responses. A workspace root that has **gone away or gone
        read-only** - an unmounted volume, an SD card the kernel remounted after
        an I/O error, a permission that changed under a restart - makes every
        acquisition this instance accepts fail, so it should stop being sent
        traffic. A workspace that is merely **full** is not a readiness problem
        at all: requests still queue correctly and the worker's own backpressure
        holds them until space returns. Conflating the two would take a healthy
        instance out of service for a condition one deletion fixes.

        Deliberately a permission check rather than a probe write: a readiness
        endpoint that writes a file is a readiness endpoint that wears out the
        card it is reporting on.
        """
        if self.workspace is None:
            return True
        root = getattr(self.workspace, "root", None)
        if root is None:  # pragma: no cover - only a workspace without a root
            return True
        try:
            usable = root.is_dir() and os.access(root, os.W_OK)
        except OSError:
            logger.exception("Workspace readiness check failed")
            return False
        if not usable:
            logger.bind(workspace=str(root)).error(
                "The workspace root is missing or not writable; this instance cannot "
                "acquire anything until it is restored"
            )
        return usable

    async def shutdown(self) -> None:
        """Release every resource the container owns. Safe to call twice.

        The engine-thread count is reported here because shutdown is the last
        moment anything can say it. A process that abandoned threads has
        permanently lost pool capacity, and the line below is the difference
        between an operator seeing that and inferring it from a worker that
        gradually stopped downloading.
        """
        threads = engine_thread_stats()
        if threads.is_leaking:
            logger.bind(running=threads.running, abandoned=threads.abandoned).error(
                "Engine threads were abandoned during this process's life; they were "
                "still holding descriptors when it exited"
            )
        if self.database is not None:
            await self.database.dispose()


def build_container(settings: Settings) -> Container:
    """Wire the application for the given configuration.

    Args:
        settings: The validated configuration for this process.

    Returns:
        A fully wired container. The caller owns it and must call
        :meth:`Container.shutdown` when done.
    """
    database: Database | SqliteDatabase | None = None
    unit_of_work: UnitOfWorkFactory
    job_queue: JobQueue | None = None
    journal: AcquisitionJournal = InMemoryAcquisitionJournal()

    if settings.database.backend is PersistenceBackend.SQLITE:
        sqlite = SqliteDatabase(settings.database)
        database = sqlite
        unit_of_work = SqlAlchemyUnitOfWorkFactory(sqlite.session_factory)
        # History outlives the process here, which on a device that loses power
        # is the normal case rather than the exceptional one.
        journal = SqliteAcquisitionJournal(sqlite.session_factory)
        # So does the queue. A job claimed when the power went is reclaimed when
        # the lease lapses and resumed from its last checkpoint, instead of
        # being lost with the process that was running it.
        job_queue = SqliteJobQueue(sqlite.session_factory)
    elif settings.database.backend is PersistenceBackend.POSTGRES:
        database = Database(settings.database)
        unit_of_work = SqlAlchemyUnitOfWorkFactory(database.session_factory)
    else:
        memory = InMemoryUnitOfWorkFactory()
        unit_of_work = memory
        # The queue shares the store the repositories commit into, so a job
        # becomes claimable the moment the transaction that created it commits -
        # the transactional enqueue that a separate broker would need an outbox
        # to achieve (``docs/architecture/09-queue-architecture.md`` §9.1).
        job_queue = InMemoryJobQueue(memory.database)

    clock = SystemClock()
    workspace = _build_workspace(settings, clock)
    downloader, egress = _build_downloader(settings)
    container = Container(
        settings=settings,
        clock=clock,
        uuid_generator=Uuid4Generator(),
        event_publisher=LoggingEventPublisher(),
        unit_of_work=unit_of_work,
        downloader=downloader,
        egress=egress,
        workspace=workspace,
        database=database,
        job_queue=job_queue,
        journal=journal,
        cookies=FilesystemCookieStore(settings.download.cookies_file),
        audit=LoggingAuditSink(),
        allow_list=_build_allow_list(settings),
        authorization=AuthorizationPolicy(),
    )

    logger.bind(
        environment=settings.environment.value,
        backend=settings.database.backend.value,
        downloader=type(container.downloader).__name__,
        workspace=str(workspace.root) if workspace is not None else None,
    ).info("Application container built")
    return container


def _build_workspace(settings: Settings, clock: Clock) -> FilesystemWorkspace | None:
    """Prepare the scratch space, or ``None`` when nothing will write.

    Startup is the one moment ownership can be established, so the sweep runs
    here, before anything claims work. What it does with what it finds is the
    operator's decision, expressed as a policy: wipe the root (right for a
    single-process deployment), or keep what this identity left behind and
    leave anything else alone until it expires
    (``docs/architecture/11-storage-strategy.md`` §11.6).
    """
    if not settings.download.enabled:
        return None
    workspace = FilesystemWorkspace(
        settings.workspace.root,
        limits=WorkspaceLimits(
            min_free_bytes=settings.workspace.min_free_bytes,
            max_lease_bytes=settings.workspace.max_lease_bytes,
            max_total_bytes=settings.workspace.max_total_bytes,
        ),
        clock=clock,
    )
    RecoverWorkspaces(
        workspace=workspace,
        policy=RecoveryPolicy(
            lease_expiry_seconds=settings.workspace.lease_expiry_seconds,
            adopt_own_leases=settings.workspace.adopt_own_leases_on_start,
            delete_every_lease=settings.workspace.purge_on_start,
        ),
        owner=workspace.owner,
        clock=clock,
    ).execute()
    return workspace


def _build_allow_list(settings: Settings) -> AllowListPolicy:
    """Turn the configured Telegram id lists into an access policy.

    Deny by default: an instance with no ids configured allows nobody, which is
    the correct posture for a bot whose username is guessable.
    """
    telegram = settings.telegram
    return AllowListPolicy.from_ids(
        scheme="telegram",
        owner_ids=tuple(telegram.owner_ids),
        member_ids=tuple(telegram.member_ids),
        readonly_ids=tuple(telegram.readonly_ids),
    )


def _build_downloader(settings: Settings) -> tuple[DownloaderPort, EgressRoutes | None]:
    """Return the real engine when enabled, and the null adapter otherwise.

    The engine is off by default so that a deployment which has not been
    configured for downloading reports the capability as unavailable rather than
    failing at the first job. The second item is the engine's egress policy,
    handed to the operator's surface; there is none without an engine.
    """
    if not settings.download.enabled:
        return NullDownloader(), None

    policy = UrlPolicy(block_private_networks=settings.security.block_private_networks)
    guard = DnsAddressGuard(policy)
    video = YtDlpDownloader(
        settings.download,
        url_policy=policy,
        address_guard=guard,
        inspector=(
            FfprobeInspector(settings.download.ffprobe_path)
            if settings.download.verify_streams
            else None
        ),
    )
    egress = video.proxy_policy
    if not settings.download.images_enabled:
        return video, egress

    images = GalleryDlDownloader(settings.download, url_policy=policy, address_guard=guard)
    if not images.is_available:
        # Configured for images and unable to fetch them. Worth a line, because
        # the symptom is otherwise a photo post refused exactly as before and
        # nothing to say the engine meant to handle it is missing.
        logger.warning(
            "Image downloads are enabled but gallery-dl is not installed; "
            "photo posts will keep being refused"
        )
        return video, egress
    return CompositeDownloader(video, images), egress
