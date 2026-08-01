"""The claim loop settles every attempt exactly once, and survives anything.

Two rules are tested harder than the rest, because both are the difference
between a bad job and a bad day:

* the loop never dies from a job's exception - one poison job must not stop a
  worker claiming anything ever again;
* a job whose lease was taken is left completely alone - writing to it would be
  the second writer that turns one download into two.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import ProviderError, UnsupportedProviderError
from mediahub.application.download.failures import DISK_RETRY_SECONDS
from mediahub.application.download.queue import JobStage
from mediahub.domain.download.enums import JobStatus
from mediahub.domain.workspace.errors import InsufficientDiskSpaceError
from mediahub.infrastructure.persistence.memory.queue import InMemoryJobQueue
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.loop import ClaimLoop
from tests.support.worker_fakes import OTHER_WORKER, WORKER, WorkerHarness

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from datetime import datetime
    from pathlib import Path

    from mediahub.application.download.queue import ClaimedJob, WorkerId
    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.presentation.worker.stages.base import StageContext

pytestmark = pytest.mark.unit

FAST = WorkerTimings(
    lease_seconds=20.0,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.01,
    drain_grace_seconds=1.0,
)


class BrokenQueue(InMemoryJobQueue):
    """A queue that fails the way an unreachable database does."""

    async def claim(
        self, *, worker: WorkerId, lease_seconds: float, now: datetime
    ) -> ClaimedJob | None:
        message = "database is locked"
        raise TimeoutError(message)


class AlreadyCancelledQueue(InMemoryJobQueue):
    """A queue that hands out a job somebody has already asked to stop.

    The in-memory claim filters those out, as the SQL claim does. A worker must
    not depend on that: honouring the flag it was handed costs one line and
    covers any adapter that does not filter.
    """

    async def claim(
        self, *, worker: WorkerId, lease_seconds: float, now: datetime
    ) -> ClaimedJob | None:
        claimed = await super().claim(worker=worker, lease_seconds=lease_seconds, now=now)
        return None if claimed is None else replace(claimed, cancel_requested=True)


class FullDiskWorkspace:
    """A workspace on a device with nothing left to give."""

    def lease(
        self, *, label: str, reserve_bytes: int | None = None
    ) -> AbstractContextManager[WorkspaceScope]:
        del label
        raise InsufficientDiskSpaceError(reserve_bytes or 0, 0)

    def free_bytes(self) -> int:
        return 0


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness.build(tmp_path, timings=FAST)


def _loop_on_a_full_disk(harness: WorkerHarness) -> ClaimLoop:
    """Return a loop whose workspace refuses every lease."""
    return ClaimLoop(
        services=replace(harness.services, workspace=FullDiskWorkspace()),
        executor=harness.executor(),
        worker=WORKER,
        timings=harness.timings,
    )


class TestOneCycle:
    async def test_an_empty_queue_is_not_work(self, harness: WorkerHarness) -> None:
        assert await harness.loop().run_once() is False

    async def test_a_job_runs_every_stage_and_completes(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()

        assert await harness.loop().run_once() is True

        job = await harness.job(job_id)
        assert job.status is JobStatus.SUCCEEDED
        assert all(handler.calls == 1 for handler in harness.handlers)
        assert harness.leases_in_use() == 0

    async def test_the_workspace_is_gone_afterwards(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        harness.handler(JobStage.DOWNLOAD).bytes_written = 4096

        await harness.loop().run_once()

        assert harness.workspace_directories() == []

    async def test_progress_reaches_the_job(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        harness.handler(JobStage.DOWNLOAD).on_execute = lambda context: context.observe(
            transferred_bytes=900, total_bytes=1000, speed_bps=100.0, eta_seconds=1.0
        )

        await harness.loop().run_once()

        observed = harness.queue.progress_of(job_id)
        assert observed is not None
        assert observed.percentage == 90.0
        assert observed.eta_seconds == 1.0
        assert (await harness.job(job_id)).progress.downloaded_bytes == 900


class TestFailureSettlement:
    async def test_a_transient_failure_goes_back_to_the_queue(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue(backoff_seconds=30)
        harness.handler(JobStage.DOWNLOAD).error = ProviderError("upstream 503")

        await harness.loop().run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 1
        assert harness.leases_in_use() == 0
        assert harness.workspace_directories() == []

    async def test_a_permanent_failure_stops_the_job(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        harness.handler(JobStage.PROBE).error = UnsupportedProviderError("no extractor")

        await harness.loop().run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.FAILED
        assert job.last_error is not None
        assert "unsupported_provider" in job.last_error

    async def test_a_retried_job_resumes_from_its_checkpoint(self, harness: WorkerHarness) -> None:
        await harness.enqueue(backoff_seconds=0)
        harness.handler(JobStage.VERIFY).error = ProviderError("upstream 503")
        harness.handler(JobStage.VERIFY).error_times = 1

        loop = harness.loop()
        await loop.run_once()
        await loop.run_once()

        assert harness.handler(JobStage.PROBE).calls == 1, "already done; not repeated"
        assert harness.handler(JobStage.DOWNLOAD).calls == 1
        assert harness.handler(JobStage.VERIFY).calls == 2
        assert harness.handler(JobStage.DELIVER).calls == 1

    async def test_a_queue_that_keeps_failing_does_not_kill_the_loop(
        self, harness: WorkerHarness
    ) -> None:
        harness.queue = BrokenQueue(harness.unit_of_work.database)
        loop = harness.loop()

        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.05)
        still_running = not task.done()
        loop.drain()
        await asyncio.wait_for(task, timeout=5)

        assert still_running, "an unreachable queue is absorbed, not fatal"

    async def test_a_stage_that_explodes_costs_only_that_job(self, harness: WorkerHarness) -> None:
        poison = await harness.enqueue(max_attempts=1)
        harness.handler(JobStage.PROBE).error = ZeroDivisionError("surprise")
        loop = harness.loop()
        await loop.run_once()

        harness.handler(JobStage.PROBE).error = None
        healthy = await harness.enqueue()
        await loop.run_once()

        assert (await harness.job(poison)).status is JobStatus.FAILED
        assert (await harness.job(healthy)).status is JobStatus.SUCCEEDED


class TestWorkspaceFailures:
    async def test_a_full_disk_settles_the_job_instead_of_stranding_it(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()

        await _loop_on_a_full_disk(harness).run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.QUEUED, "transient: the space may come back"
        assert job.last_error is not None
        assert "insufficient_disk_space" in job.last_error
        assert harness.leases_in_use() == 0, "the job did not stay leased until it lapsed"
        assert all(handler.calls == 0 for handler in harness.handlers)

    async def test_a_disk_failure_waits_longer_than_the_usual_backoff(
        self, harness: WorkerHarness
    ) -> None:
        await harness.enqueue(backoff_seconds=30)

        await _loop_on_a_full_disk(harness).run_once()

        # A full disk does not free itself in thirty seconds; retrying on the
        # normal curve would just waste wakeups.
        healthy = harness.loop()
        assert await healthy.run_once() is False
        harness.clock.advance(int(DISK_RETRY_SECONDS))
        assert await healthy.run_once() is True


class TestCancellation:
    async def test_a_cancellation_during_a_stage_stops_the_job(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()

        async def request_cancel(context: StageContext) -> None:
            await harness.queue.request_cancellation(context.job_id, now=harness.clock.now())
            harness.clock.advance(int(FAST.heartbeat_seconds))
            for _ in range(200):
                if context.cancellation.cancelled:
                    return
                await asyncio.sleep(0.01)

        harness.handler(JobStage.PROBE).on_execute = request_cancel

        await asyncio.wait_for(harness.loop().run_once(), timeout=5)

        job = await harness.job(job_id)
        assert job.status is JobStatus.CANCELLED
        assert harness.handler(JobStage.DOWNLOAD).calls == 0
        assert harness.leases_in_use() == 0
        assert harness.workspace_directories() == []

    async def test_a_job_handed_over_already_cancelled_runs_nothing(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        harness.queue = AlreadyCancelledQueue(harness.unit_of_work.database)

        await harness.loop().run_once()

        assert (await harness.job(job_id)).status is JobStatus.CANCELLED
        assert all(handler.calls == 0 for handler in harness.handlers)

    async def test_draining_hands_the_job_back_unharmed(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        loop = harness.loop()
        harness.handler(JobStage.PROBE).on_execute = lambda _: loop.drain()

        await loop.run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 0, "a deploy is not the job's fault"
        assert harness.handler(JobStage.DOWNLOAD).calls == 0
        assert loop.draining
        assert harness.workspace_directories() == []

    async def test_a_drained_job_resumes_where_it_stopped(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        first = harness.loop()
        harness.handler(JobStage.PROBE).on_execute = lambda _: first.drain()

        await first.run_once()
        harness.handler(JobStage.PROBE).on_execute = None
        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.handler(JobStage.PROBE).calls == 1, "resumed, not restarted"


class TestLostLease:
    async def test_a_job_whose_lease_was_taken_is_left_alone(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()

        async def reclaimed_underneath(context: StageContext) -> None:
            del context
            await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        harness.handler(JobStage.PROBE).on_execute = reclaimed_underneath

        await harness.loop().run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.RUNNING, "whoever owns it now decides how it ends"
        assert job.attempts == 1
        assert job.last_error is None, "nothing was reported about a job we no longer hold"
        assert harness.handler(JobStage.DOWNLOAD).calls == 0
        assert harness.workspace_directories() == []

    async def test_a_lease_lost_while_settling_is_absorbed(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue(backoff_seconds=0)

        async def fail_after_losing_the_lease(context: StageContext) -> None:
            del context
            await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        # The stage fails *and* the lease is gone, so the settlement itself is
        # the write that discovers it. It must not escape the loop.
        harness.handler(JobStage.DOWNLOAD).on_execute = fail_after_losing_the_lease
        harness.handler(JobStage.DOWNLOAD).error = ProviderError("upstream 503")

        await harness.loop().run_once()

        assert (await harness.job(job_id)).status is JobStatus.QUEUED


class TestTheLoop:
    async def test_the_loop_works_through_the_queue_then_idles(
        self, harness: WorkerHarness
    ) -> None:
        first = await harness.enqueue()
        second = await harness.enqueue()
        loop = harness.loop()

        task = asyncio.create_task(loop.run())
        for _ in range(200):
            if (await harness.job(second)).status is JobStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        loop.drain()
        await asyncio.wait_for(task, timeout=5)

        assert (await harness.job(first)).status is JobStatus.SUCCEEDED
        assert (await harness.job(second)).status is JobStatus.SUCCEEDED

    async def test_draining_mid_job_stops_the_loop_after_it(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        second = await harness.enqueue()
        loop = harness.loop()
        harness.handler(JobStage.DELIVER).on_execute = lambda _: loop.drain()

        task = asyncio.create_task(loop.run())
        await asyncio.wait_for(task, timeout=5)

        assert (await harness.job(second)).status is JobStatus.QUEUED, "claiming stopped at once"

    async def test_draining_an_idle_loop_stops_it(self, harness: WorkerHarness) -> None:
        loop = harness.loop()

        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.02)
        loop.drain()
        await asyncio.wait_for(task, timeout=5)

        assert loop.draining

    async def test_a_job_claimed_while_draining_is_handed_straight_back(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        loop = harness.loop()
        loop.drain()

        await loop.run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 0
        assert all(handler.calls == 0 for handler in harness.handlers)


class TestBackoff:
    def test_the_idle_wait_doubles_up_to_its_ceiling(self) -> None:
        timings = WorkerTimings(idle_poll_seconds=1.0, max_idle_poll_seconds=5.0)

        assert timings.backoff_from(1.0) == 2.0
        assert timings.backoff_from(2.0) == 4.0
        assert timings.backoff_from(4.0) == 5.0
        assert timings.backoff_from(5.0) == 5.0
