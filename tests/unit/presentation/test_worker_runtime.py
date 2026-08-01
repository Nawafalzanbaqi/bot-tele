"""The supervisor refuses to start badly, and recovers what it left behind.

Both halves matter on a device nobody is watching: a worker that claims jobs it
cannot finish burns their attempts until they dead-letter, and a restarted
worker that does not take back its own leases stalls every job it was holding
for a full lease period.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import WorkerNotReadyError
from mediahub.application.download.queue import JobStage
from mediahub.domain.download.enums import JobStatus
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.runtime import WorkerRuntime
from mediahub.presentation.worker.shutdown import ShutdownController
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN, StageRegistry
from tests.support.worker_fakes import OTHER_WORKER, WORKER, WorkerHarness

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit

FAST = WorkerTimings(
    lease_seconds=20.0,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.01,
    drain_grace_seconds=1.0,
)


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness.build(tmp_path, timings=FAST)


class TestTimings:
    def test_a_heartbeat_that_cannot_hold_a_lease_is_refused(self) -> None:
        with pytest.raises(WorkerNotReadyError):
            WorkerTimings(lease_seconds=30.0, heartbeat_seconds=25.0)

    def test_a_backoff_that_cannot_grow_is_refused(self) -> None:
        with pytest.raises(WorkerNotReadyError):
            WorkerTimings(idle_poll_seconds=5.0, max_idle_poll_seconds=1.0)

    def test_the_defaults_are_survivable(self) -> None:
        timings = WorkerTimings()

        assert timings.heartbeat_seconds * 2 <= timings.lease_seconds


class TestReadiness:
    async def test_a_worker_with_no_handler_for_a_stage_refuses_to_start(
        self, harness: WorkerHarness
    ) -> None:
        runtime = WorkerRuntime(
            services=harness.services,
            handlers=StageRegistry([harness.handler(JobStage.PROBE)]),
            worker=WORKER,
            timings=FAST,
        )

        with pytest.raises(WorkerNotReadyError) as failure:
            await runtime.start()

        assert "download" in str(failure.value)

    async def test_a_worker_with_no_slots_is_refused(self, harness: WorkerHarness) -> None:
        with pytest.raises(WorkerNotReadyError):
            WorkerRuntime(
                services=harness.services,
                handlers=harness.registry(),
                worker=WORKER,
                slots=0,
            )

    async def test_a_ready_worker_reports_its_identity_and_plan(
        self, harness: WorkerHarness
    ) -> None:
        runtime = harness.runtime()

        recovery = await runtime.start()

        assert runtime.worker_id == WORKER
        assert recovery.total == 0
        assert harness.registry().missing_for(DEFAULT_STAGE_PLAN) == ()


class TestStartupRecovery:
    async def test_a_restarted_worker_takes_back_its_own_jobs(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        claimed = await harness.services.claim.execute(worker=WORKER)
        assert claimed is not None

        recovery = await harness.runtime().start()

        assert recovery.reclaimed == (job_id.value,)
        assert (await harness.job(job_id)).status is JobStatus.QUEUED

    async def test_a_lapsed_lease_from_another_worker_is_swept_too(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        assert await harness.services.claim.execute(worker=OTHER_WORKER) is not None
        harness.clock.advance(int(FAST.lease_seconds) + 1)

        recovery = await harness.runtime().start()

        assert recovery.reclaimed == (job_id.value,)

    async def test_sweeping_other_workers_can_be_switched_off(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        assert await harness.services.claim.execute(worker=OTHER_WORKER) is not None
        harness.clock.advance(int(FAST.lease_seconds) + 1)

        runtime = WorkerRuntime(
            services=harness.services,
            handlers=harness.registry(),
            worker=WORKER,
            timings=FAST,
            reclaim_expired_leases=False,
        )
        recovery = await runtime.start()

        assert recovery.total == 0

    async def test_recovering_this_worker_can_be_switched_off(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        assert await harness.services.claim.execute(worker=WORKER) is not None

        runtime = WorkerRuntime(
            services=harness.services,
            handlers=harness.registry(),
            worker=WORKER,
            timings=FAST,
            recover_own_leases=False,
            reclaim_expired_leases=False,
        )
        recovery = await runtime.start()

        assert recovery.total == 0


class TestRunning:
    async def test_the_worker_drains_every_slot_on_a_stop_request(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        runtime = harness.runtime(slots=2)

        task = asyncio.create_task(runtime.run())
        for _ in range(200):
            if (await harness.job(job_id)).status is JobStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        assert (await harness.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.leases_in_use() == 0

    async def test_a_worker_started_explicitly_then_run_recovers_and_continues(
        self, harness: WorkerHarness
    ) -> None:
        # The startup sequence a supervisor would drive: check readiness first,
        # decide whether to carry on, then run.
        job_id = await harness.enqueue()
        assert await harness.services.claim.execute(worker=WORKER) is not None
        runtime = harness.runtime()
        recovery = await runtime.start()

        task = asyncio.create_task(runtime.run())
        for _ in range(200):
            if (await harness.job(job_id)).status is JobStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        job = await harness.job(job_id)
        assert recovery.reclaimed == (job_id.value,)
        assert job.status is JobStatus.SUCCEEDED
        assert job.attempts == 2, "the interrupted attempt, then the one that finished"

    async def test_stopping_before_anything_is_claimed_is_clean(
        self, harness: WorkerHarness
    ) -> None:
        runtime = harness.runtime()

        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.02)
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        assert harness.workspace_directories() == []

    async def test_two_slots_do_not_run_the_same_job_twice(self, harness: WorkerHarness) -> None:
        first = await harness.enqueue()
        second = await harness.enqueue()
        runtime = harness.runtime(slots=2)

        task = asyncio.create_task(runtime.run())
        for _ in range(200):
            statuses = [(await harness.job(first)).status, (await harness.job(second)).status]
            if all(status is JobStatus.SUCCEEDED for status in statuses):
                break
            await asyncio.sleep(0.01)
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        assert harness.handler(JobStage.DOWNLOAD).calls == 2, "two jobs, two runs"


class TestShutdownController:
    def test_a_stop_request_fires_the_callback_once(self) -> None:
        fired: list[int] = []
        controller = ShutdownController(on_stop=lambda: fired.append(1))

        controller.request()
        controller.request()

        assert fired == [1]
        assert controller.requested

    def test_asking_twice_means_now(self) -> None:
        controller = ShutdownController()

        controller.request()
        assert not controller.immediate

        controller.request()
        assert controller.immediate

    async def test_waiting_ends_when_the_stop_arrives(self) -> None:
        controller = ShutdownController()

        waiting = asyncio.create_task(controller.wait())
        await asyncio.sleep(0)
        controller.request()

        await asyncio.wait_for(waiting, timeout=1)

    async def test_an_impatient_stop_skips_the_drain_grace(self, harness: WorkerHarness) -> None:
        stuck = asyncio.Event()
        harness.handler(JobStage.PROBE).on_execute = lambda _: stuck.wait()
        await harness.enqueue()
        runtime = harness.runtime()

        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.05)
        runtime.shutdown.request()
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        stuck.set()
        assert runtime.shutdown.immediate
