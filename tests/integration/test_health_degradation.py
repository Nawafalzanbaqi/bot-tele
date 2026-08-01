"""Readiness answers the question an orchestrator is actually asking.

"Should traffic be routed here?" - not "is anything wrong anywhere?". The
difference decides whether a device with a full SD card keeps accepting work
(it should: the queue is the right place for that work to wait) or is pulled
out of service for a condition one deletion fixes.

Two dependencies are checked and both have to hold:

* the persistence backend answers;
* the workspace root is still a writable directory.

The second is the one added by production hardening, and it catches the failure
that used to be invisible: an unmounted volume, or a filesystem the kernel
remounted read-only after an I/O error. An instance in that state answers every
request and completes none of them, which is precisely what readiness exists to
detect. A workspace that is merely *full* stays ready.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from mediahub.infrastructure.download.ytdlp.downloader import (
    _THREADS,
    engine_thread_stats,
    reset_engine_thread_stats,
)
from mediahub.infrastructure.workspace.disk import DiskUsage, WorkspaceLimits
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.presentation.api.app import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mediahub.infrastructure.di.container import Container
    from mediahub.shared.config.settings import Settings

pytestmark = pytest.mark.integration


async def client_for(settings: Settings, container: Container) -> AsyncIterator[AsyncClient]:
    """Yield a client over an app wired to ``container``, lifespan running."""
    app = create_app(settings, container)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


class TestWorkspaceReadiness:
    async def test_a_healthy_workspace_answers_ready(
        self, settings: Settings, container: Container, tmp_path: Path
    ) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        wired = replace(container, workspace=workspace)

        async for http in client_for(settings, wired):
            response = await http.get("/health/ready")

            assert response.status_code == 200
            assert response.json() == {"status": "ready", "database": True}

    async def test_a_workspace_root_that_vanished_answers_degraded(
        self, settings: Settings, container: Container, tmp_path: Path
    ) -> None:
        # An unmounted volume, or a card the kernel gave up on. Every request
        # this instance accepts will fail, so it should stop being sent any.
        root = tmp_path / "workspace"
        workspace = FilesystemWorkspace(root)
        root.rmdir()
        wired = replace(container, workspace=workspace)

        async for http in client_for(settings, wired):
            response = await http.get("/health/ready")

            assert response.status_code == 503
            assert response.json()["status"] == "degraded"

    async def test_the_payload_shape_is_unchanged_when_degraded(
        self, settings: Settings, container: Container, tmp_path: Path
    ) -> None:
        # The response schema is a contract with whatever scrapes it. A new
        # failure reason must not become a new field.
        root = tmp_path / "workspace"
        workspace = FilesystemWorkspace(root)
        root.rmdir()
        wired = replace(container, workspace=workspace)

        async for http in client_for(settings, wired):
            body = (await http.get("/health/ready")).json()

            assert set(body) == {"status", "database"}
            assert body["database"] is True, "the database is fine; the disk is not"

    async def test_a_full_workspace_stays_ready(
        self, settings: Settings, container: Container, tmp_path: Path
    ) -> None:
        # Deliberately *not* a readiness failure. Requests still queue correctly
        # and the worker's own backpressure holds them until space returns;
        # pulling the instance would take a working API out of service.
        class FullDevice:
            def usage(self, path: Path) -> DiskUsage:
                del path
                return DiskUsage(capacity_bytes=32 * 1024**3, free_bytes=0)

        workspace = FilesystemWorkspace(
            tmp_path / "workspace",
            limits=WorkspaceLimits(min_free_bytes=1024**3),
            probe=FullDevice(),
        )
        wired = replace(container, workspace=workspace)

        async for http in client_for(settings, wired):
            response = await http.get("/health/ready")

            assert response.status_code == 200
            assert workspace.free_bytes() == 0, "the device really is full"

    async def test_liveness_ignores_the_workspace_entirely(
        self, settings: Settings, container: Container, tmp_path: Path
    ) -> None:
        # A failing dependency must never make the orchestrator kill an
        # otherwise healthy process into a restart loop.
        root = tmp_path / "workspace"
        workspace = FilesystemWorkspace(root)
        root.rmdir()
        wired = replace(container, workspace=workspace)

        async for http in client_for(settings, wired):
            assert (await http.get("/health/live")).status_code == 200


class TestContainerLifecycle:
    async def test_shutdown_is_safe_to_call_twice(self, container: Container) -> None:
        # A supervisor that races its own teardown must not turn a clean stop
        # into a stack trace.
        await container.shutdown()
        await container.shutdown()

    async def test_shutdown_reports_abandoned_engine_threads(self, container: Container) -> None:
        # Shutdown is the last moment anything can say that this process
        # permanently lost thread-pool capacity.
        reset_engine_thread_stats()
        try:
            _THREADS.abandoned()
            assert engine_thread_stats().is_leaking

            await container.shutdown()
        finally:
            reset_engine_thread_stats()

    async def test_a_container_with_no_workspace_is_ready(self, container: Container) -> None:
        # The engine is off, so there is nothing to write and nothing to be
        # unready about.
        assert container.workspace is None
        assert container.check_workspace() is True
