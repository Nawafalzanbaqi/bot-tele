"""A job requested through the API's use cases is executed by a real worker.

The runtime seam, tested end to end with the real composition root: a job is
catalogued and queued exactly as an HTTP request or a Telegram message would
queue it, and a worker built by ``build_container`` claims it, runs its stages
and settles it. The handlers here are scripted stand-ins, because what is under
test is the *runtime* - claiming, leasing, checkpointing, settling.

What the real handlers do with a job is
``tests/integration/test_acquisition_pipeline.py``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mediahub.application.delivery.errors import NoProviderForTargetError
from mediahub.application.download.dto import (
    GetDownloadJobQuery,
    RequestDownloadCommand,
)
from mediahub.application.download.errors import WorkerNotReadyError
from mediahub.application.download.queue import JobStage, WorkerId
from mediahub.application.media.dto import RegisterMediaCommand
from mediahub.domain.download.enums import JobStatus
from mediahub.domain.media.enums import MediaType
from mediahub.infrastructure.di.container import build_container
from mediahub.presentation.worker.__main__ import (
    build_services,
    build_stage_handlers,
    main,
    timings_from,
    worker_identity,
    worker_target,
)
from mediahub.presentation.worker.runtime import WorkerRuntime
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN, StageRegistry
from mediahub.shared.config.settings import (
    DatabaseSettings,
    DownloadSettings,
    Environment,
    LoggingSettings,
    LogLevel,
    PersistenceBackend,
    Settings,
    WorkerSettings,
    WorkspaceSettings,
)
from tests.support.delivery_fakes import FakeDeliveryProvider
from tests.support.worker_fakes import handlers_for

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from mediahub.infrastructure.di.container import Container

pytestmark = pytest.mark.integration


def build_settings(
    tmp_path: Path,
    *,
    worker_enabled: bool = True,
    download_enabled: bool = True,
) -> Settings:
    """Return a configuration for a worker process, writing under ``tmp_path``."""
    return Settings(
        _env_file=None,
        environment=Environment.TESTING,
        database=DatabaseSettings(backend=PersistenceBackend.MEMORY),
        download=DownloadSettings(enabled=download_enabled),
        workspace=WorkspaceSettings(root=tmp_path / "workspace", min_free_bytes=0),
        worker=WorkerSettings(
            enabled=worker_enabled,
            host="testhost",
            lease_seconds=20.0,
            heartbeat_seconds=5.0,
            idle_poll_seconds=0.05,
            max_idle_poll_seconds=0.1,
            progress_interval_seconds=0.0,
        ),
        logging=LoggingSettings(level=LogLevel.WARNING),
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture
def container(settings: Settings) -> Container:
    return build_container(settings)


async def queue_a_job(container: Container) -> UUID:
    """Catalogue an item and queue a download for it, as an interface would."""
    media = await container.register_media_use_case().execute(
        RegisterMediaCommand(
            source_url="https://example.com/clip.mp4",
            title="A clip",
            media_type=MediaType.VIDEO,
        )
    )
    job = await container.request_download_use_case().execute(
        RequestDownloadCommand(media_id=media.media_id)
    )
    return job.job_id


class TestTheWiredWorker:
    async def test_a_queued_job_is_executed_and_completed(
        self, container: Container, settings: Settings
    ) -> None:
        job_id = await queue_a_job(container)
        handlers = handlers_for(DEFAULT_STAGE_PLAN)
        runtime = WorkerRuntime(
            services=build_services(container),
            handlers=StageRegistry(handlers),
            worker=worker_identity(settings.worker),
            timings=timings_from(settings.worker),
        )
        await runtime.start()

        assert await _run_one_job(runtime) is True

        summary = await container.get_download_job_use_case().execute(
            GetDownloadJobQuery(job_id=job_id)
        )
        assert summary.status is JobStatus.SUCCEEDED
        assert summary.attempts == 1
        assert all(handler.calls == 1 for handler in handlers)

    async def test_progress_reported_by_a_stage_is_readable_from_the_job(
        self, container: Container, settings: Settings
    ) -> None:
        job_id = await queue_a_job(container)
        handlers = handlers_for(DEFAULT_STAGE_PLAN)
        download = next(handler for handler in handlers if handler.stage is JobStage.DOWNLOAD)
        download.on_execute = lambda context: context.observe(
            transferred_bytes=750, total_bytes=1000, speed_bps=250.0, eta_seconds=1.0
        )
        runtime = WorkerRuntime(
            services=build_services(container),
            handlers=StageRegistry(handlers),
            worker=worker_identity(settings.worker),
            timings=timings_from(settings.worker),
        )
        await runtime.start()
        await _run_one_job(runtime)

        summary = await container.get_download_job_use_case().execute(
            GetDownloadJobQuery(job_id=job_id)
        )
        assert summary.downloaded_bytes == 750
        assert summary.percentage == 75.0

    async def test_the_workspace_is_empty_when_the_job_is_done(
        self, container: Container, settings: Settings
    ) -> None:
        await queue_a_job(container)
        handlers = handlers_for(DEFAULT_STAGE_PLAN)
        next(h for h in handlers if h.stage is JobStage.DOWNLOAD).bytes_written = 4096
        runtime = WorkerRuntime(
            services=build_services(container),
            handlers=StageRegistry(handlers),
            worker=worker_identity(settings.worker),
            timings=timings_from(settings.worker),
        )
        await runtime.start()
        await _run_one_job(runtime)

        root = settings.workspace.root
        assert not root.exists() or [entry for entry in root.iterdir() if entry.is_dir()] == []


class TestComposition:
    def test_the_worker_identity_is_stable_and_readable(self, settings: Settings) -> None:
        assert str(worker_identity(settings.worker)) == "testhost:worker:0"

    def test_a_hostname_with_a_colon_is_still_a_valid_identity(self, tmp_path: Path) -> None:
        settings = build_settings(tmp_path)
        worker = worker_identity(settings.worker.model_copy(update={"host": "fe80::1"}))

        assert str(worker) == "fe80--1:worker:0"

    def test_the_identity_falls_back_to_the_machine_name(self, tmp_path: Path) -> None:
        settings = build_settings(tmp_path)

        worker = worker_identity(settings.worker.model_copy(update={"host": None}))

        assert isinstance(worker, WorkerId)
        assert worker.host

    def test_timings_come_from_configuration(self, settings: Settings) -> None:
        timings = timings_from(settings.worker)

        assert timings.lease_seconds == 20.0
        assert timings.heartbeat_seconds == 5.0

    def test_a_worker_without_a_workspace_refuses_to_be_built(self, tmp_path: Path) -> None:
        container = build_container(build_settings(tmp_path, download_enabled=False))

        with pytest.raises(RuntimeError, match="workspace"):
            build_services(container)

    def test_a_backend_with_no_queue_adapter_says_so(self, container: Container) -> None:
        # What a Postgres-backed container looks like today: repositories, but
        # no queue adapter until the SQLite queue lands.
        queueless = replace(container, job_queue=None)

        with pytest.raises(RuntimeError, match="queue"):
            queueless.claim_job_use_case()

    def test_the_pipeline_covers_every_stage_of_the_plan(self, container: Container) -> None:
        # A worker refuses to start with a gap in its plan, so the composition
        # root's job is to leave none.
        router = container.delivery_router(FakeDeliveryProvider(provider_name="telegram"))

        registry = StageRegistry(build_stage_handlers(container, router, principal="testhost"))

        assert registry.missing_for(DEFAULT_STAGE_PLAN) == ()
        assert registry.stages == set(DEFAULT_STAGE_PLAN)

    def test_the_worker_delivers_to_the_configured_default_destination(
        self, settings: Settings
    ) -> None:
        # A queued job names no destination, so the deployment's default is it.
        target = worker_target(settings.delivery)

        assert target.provider == settings.delivery.default_provider
        assert target.address.provider == settings.delivery.default_provider

    def test_a_worker_no_destination_claims_refuses_to_compose(self, container: Container) -> None:
        # Discovering that nothing can accept the file after a two-hour
        # download is the worst possible moment to discover it.
        with pytest.raises(NoProviderForTargetError):
            build_stage_handlers(container, container.delivery_router(), principal="testhost")

    def test_a_worker_with_nowhere_to_write_has_no_pipeline(self, tmp_path: Path) -> None:
        engineless = build_container(build_settings(tmp_path, download_enabled=False))
        router = engineless.delivery_router(FakeDeliveryProvider(provider_name="telegram"))

        with pytest.raises(RuntimeError, match="workspace"):
            build_stage_handlers(engineless, router, principal="testhost")


class TestRefusingToStart:
    def test_a_disabled_worker_does_not_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings = build_settings(tmp_path, worker_enabled=False)
        monkeypatch.setattr("mediahub.presentation.worker.__main__.get_settings", lambda: settings)

        with pytest.raises(SystemExit) as exit_code:
            main()

        assert exit_code.value.code == 1

    def test_a_worker_with_nowhere_to_write_does_not_start(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        settings = build_settings(tmp_path, download_enabled=False)
        monkeypatch.setattr("mediahub.presentation.worker.__main__.get_settings", lambda: settings)

        with pytest.raises(SystemExit):
            main()

    async def test_a_worker_missing_a_handler_refuses_to_run(
        self, container: Container, settings: Settings
    ) -> None:
        # Claiming a job costs it an attempt, so a worker that cannot finish one
        # must never claim it.
        runtime = WorkerRuntime(
            services=build_services(container),
            handlers=StageRegistry(handlers_for([JobStage.PROBE, JobStage.DOWNLOAD])),
            worker=worker_identity(settings.worker),
            timings=timings_from(settings.worker),
        )

        with pytest.raises(WorkerNotReadyError, match="verify"):
            await runtime.start()


async def _run_one_job(runtime: WorkerRuntime) -> bool:
    """Claim and execute a single job through a slot of ``runtime``."""
    loop = runtime._build_loop(0)
    return await loop.run_once()
