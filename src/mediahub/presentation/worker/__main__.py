"""Entry point for the worker process.

``python -m mediahub.presentation.worker``

A composition root - one of the few modules allowed to name concrete adapters -
and the only place where the container, the stage handlers and the runtime meet.
Everything below it receives ports and finished use cases.

It runs as its own process, and that separation is structural: acquisition work
is blocking and memory-hungry, and one transcode inside the API process freezes
every HTTP request on a small device (``docs/adr/0010-separate-worker-process.md``).
Same image, different command.

The worker refuses to start rather than starting badly. No queue, no workspace,
no destination, no handler for a stage in its plan - each is a configuration
mistake that would otherwise present as jobs being claimed and immediately
failed, quietly burning an attempt every time.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.delivery.ports import DeliveryTarget, TargetAddress
from mediahub.application.download.queue import WorkerId
from mediahub.infrastructure.delivery.telegram.client import PythonTelegramBotClient
from mediahub.infrastructure.delivery.telegram.provider import TelegramDeliveryProvider
from mediahub.infrastructure.di.container import build_container
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.runtime import WorkerRuntime
from mediahub.presentation.worker.services import WorkerServices
from mediahub.presentation.worker.stages import (
    AcquisitionPolicy,
    AcquisitionSteps,
    StageRegistry,
    acquisition_handlers,
)
from mediahub.shared.config.settings import get_settings
from mediahub.shared.logging.setup import configure_logging

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.delivery.ports import DeliveryRouter
    from mediahub.infrastructure.di.container import Container
    from mediahub.presentation.worker.stages import StageHandler
    from mediahub.shared.config.settings import DeliverySettings, Settings, WorkerSettings


def main() -> None:
    """Start the worker, or refuse to start with a reason."""
    settings = get_settings()
    configure_logging(settings)

    if not settings.worker.enabled:
        logger.error("worker.enabled is false; nothing to run")
        raise SystemExit(1)
    if not settings.download.enabled:
        logger.error("download.enabled is false; the worker would have nowhere to write")
        raise SystemExit(1)

    asyncio.run(_run(settings))


def build_stage_handlers(
    container: Container,
    delivery: DeliveryRouter,
    *,
    principal: str,
) -> Sequence[StageHandler]:
    """Return the handlers that execute the acquisition pipeline.

    The destination is resolved here, at composition, rather than at the first
    delivery: a worker whose target no destination claims can never finish a
    job, and discovering that after a two-hour download is the worst possible
    moment to discover it.

    Args:
        container: The wired application. Supplies the engine, the journal and
            the use case that answers "what is this job for?".
        delivery: The router over whichever destinations this process started.
        principal: Whose history acquisitions are recorded under - the worker's
            own identity, since a queued job names no requester.

    Raises:
        RuntimeError: If no workspace is configured, which means the engine is
            disabled and nothing could be written anyway.
        NoProviderForTargetError: If no enabled destination claims this
            deployment's target.
    """
    if container.workspace is None:
        message = "no workspace is configured; the worker has nowhere to write"
        raise RuntimeError(message)

    target = worker_target(container.settings.delivery)
    destination = delivery.capabilities_for(target)
    logger.bind(
        destination=destination.provider,
        ceiling_bytes=destination.maximum_file_size,
        custodian=destination.can_serve_back,
    ).info("Acquisition pipeline wired")
    return acquisition_handlers(
        AcquisitionSteps(
            downloader=container.downloader,
            delivery=delivery,
            journal=container.journal,
            jobs=container.get_download_job_use_case(),
            policy=AcquisitionPolicy(
                target=target,
                principal=principal,
                max_item_bytes=container.settings.download.max_item_bytes,
                probe_timeout_seconds=container.settings.download.probe_timeout_seconds,
                download_timeout_seconds=container.settings.download.download_timeout_seconds,
                socket_timeout_seconds=container.settings.download.socket_timeout_seconds,
            ),
        )
    )


def worker_target(settings: DeliverySettings) -> DeliveryTarget:
    """Return the destination a queued job's result is sent to.

    A ``DownloadJob`` carries no destination: nobody was choosing one when it
    was enqueued. The worker therefore delivers to this deployment's configured
    default, and the router decides which provider owns it. A deployment that
    needs per-request destinations drives acquisition through the interface that
    knows them - the gateway does exactly that - rather than through the queue.
    """
    provider = settings.default_provider
    return DeliveryTarget(
        provider=provider,
        address=TargetAddress(provider=provider),
        label="default destination",
    )


def worker_identity(settings: WorkerSettings) -> WorkerId:
    """Return this process's stable identity.

    Stable, never random: a restarted worker with the same identity releases its
    own stale leases at startup instead of waiting a full lease period to
    recover work it was already doing.
    """
    host = settings.host or socket.gethostname()
    return WorkerId(host=host.replace(":", "-"), role=settings.role, index=settings.index)


def build_services(container: Container) -> WorkerServices:
    """Assemble the use cases and ports the worker is allowed to call.

    Raises:
        RuntimeError: If no workspace is configured. An attempt writes inside a
            lease and is deleted with it; without one there is nowhere safe to
            put a byte.
    """
    if container.workspace is None:
        message = "no workspace is configured; the worker has nowhere to write"
        raise RuntimeError(message)
    return WorkerServices(
        claim=container.claim_job_use_case(),
        heartbeat=container.heartbeat_job_use_case(),
        checkpoint=container.checkpoint_job_use_case(),
        report_progress=container.report_job_progress_use_case(),
        complete=container.complete_job_use_case(),
        fail=container.fail_job_use_case(),
        release=container.release_job_use_case(),
        acknowledge_cancellation=container.acknowledge_cancellation_use_case(),
        recover_leases=container.recover_leases_use_case(),
        workspace=container.workspace,
        clock=container.clock,
    )


def timings_from(settings: WorkerSettings) -> WorkerTimings:
    """Translate configuration into the numbers the runtime works in."""
    return WorkerTimings(
        lease_seconds=settings.lease_seconds,
        heartbeat_seconds=settings.heartbeat_seconds,
        idle_poll_seconds=settings.idle_poll_seconds,
        max_idle_poll_seconds=settings.max_idle_poll_seconds,
        progress_interval_seconds=settings.progress_interval_seconds,
        progress_percent_step=settings.progress_percent_step,
        drain_grace_seconds=settings.drain_grace_seconds,
    )


async def _run(settings: Settings) -> None:  # pragma: no cover - process wiring
    """Wire everything together and execute jobs until asked to stop."""
    container = build_container(settings)
    client: PythonTelegramBotClient | None = None

    if settings.telegram.enabled:
        client = PythonTelegramBotClient(
            settings.telegram.bot_token.get_secret_value(),
            api_base_url=settings.telegram.api_base_url,
        )
        await client.start()
        router = container.delivery_router(
            TelegramDeliveryProvider(
                client,
                bot_principal=settings.telegram.bot_id,
                local_api_server=settings.telegram.uses_local_api_server,
            )
        )
    else:
        router = container.delivery_router()

    worker = worker_identity(settings.worker)
    runtime = WorkerRuntime(
        services=build_services(container),
        handlers=StageRegistry(build_stage_handlers(container, router, principal=str(worker))),
        worker=worker,
        timings=timings_from(settings.worker),
        slots=settings.worker.slots,
        recover_own_leases=settings.worker.recover_own_leases_on_start,
        reclaim_expired_leases=settings.worker.reclaim_expired_leases_on_start,
    )
    runtime.shutdown.install()

    logger.bind(worker=str(worker), destinations=list(router.provider_names)).info(
        "Worker composition complete"
    )

    try:
        await runtime.run()
    finally:
        if client is not None:
            await client.close()
        await container.shutdown()


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
