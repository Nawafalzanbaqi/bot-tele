"""A worker dies mid-job. The job finishes anyway, exactly once.

The crash is simulated the only honest way available in-process: a stage raises
something no handler catches, so the attempt is abandoned **without settling and
without releasing its lease** - precisely the state a power cut leaves behind.
Recovery then runs through the real code: the lease lapses, the sweep takes it
back, another worker claims it and resumes from the last checkpoint.

What each test is really asserting, in the words of
``docs/architecture/10-worker-architecture.md`` §10.6:

* unfinished jobs become claimable again;
* no stage runs twice;
* no workspace is left behind;
* an attempt spent on a crash stays spent, so a job that reliably kills its
  worker eventually stops instead of taking the system down with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.queue import JobStage
from mediahub.domain.download.enums import JobStatus
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN
from tests.support.worker_fakes import OTHER_WORKER, WORKER, WorkerHarness

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.domain.download.value_objects import JobId

pytestmark = [pytest.mark.chaos, pytest.mark.integration]

LEASE_SECONDS = 20.0
FAST = WorkerTimings(
    lease_seconds=LEASE_SECONDS,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.01,
)


class SuddenDeath(BaseException):
    """Stands in for the process disappearing.

    A ``BaseException`` on purpose: the worker's own safety nets catch
    ``Exception``, and this must get past all of them, exactly as losing power
    does.
    """


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness.build(tmp_path, timings=FAST)


async def crash_during(harness: WorkerHarness, stage: JobStage) -> JobId:
    """Run a job until ``stage`` kills the worker, and return the job."""
    job_id = await harness.enqueue()
    harness.handler(stage).error = SuddenDeath()

    with pytest.raises(SuddenDeath):
        await harness.loop().run_once()

    harness.handler(stage).error = None
    return job_id


class TestCrashDuringAJob:
    async def test_the_job_is_still_leased_and_running(self, harness: WorkerHarness) -> None:
        job_id = await crash_during(harness, JobStage.VERIFY)

        job = await harness.job(job_id)
        state = await harness.queue.state_of(job_id)
        assert job.status is JobStatus.RUNNING
        assert state is not None
        assert state.lease.is_held_by(WORKER)

    async def test_nobody_else_may_take_it_while_the_lease_is_alive(
        self, harness: WorkerHarness
    ) -> None:
        await crash_during(harness, JobStage.VERIFY)

        assert await harness.services.claim.execute(worker=OTHER_WORKER) is None

    async def test_completed_stages_survive_the_crash(self, harness: WorkerHarness) -> None:
        job_id = await crash_during(harness, JobStage.VERIFY)

        checkpoint = harness.queue.checkpoint_of(job_id)
        assert checkpoint.completed_stages == (JobStage.PROBE, JobStage.DOWNLOAD)

    async def test_the_workspace_does_not_survive_the_crash(self, harness: WorkerHarness) -> None:
        harness.handler(JobStage.DOWNLOAD).bytes_written = 2048

        await crash_during(harness, JobStage.VERIFY)

        assert harness.workspace_directories() == []


class TestRecovery:
    async def test_the_job_becomes_claimable_again_once_the_lease_lapses(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await crash_during(harness, JobStage.VERIFY)

        harness.clock.advance(int(LEASE_SECONDS) + 1)
        recovery = await harness.services.recover_leases.execute()

        job = await harness.job(job_id)
        assert recovery.reclaimed == (job_id.value,)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 1, "the crash still cost the attempt it consumed"

    async def test_the_next_worker_resumes_and_does_not_repeat_work(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await crash_during(harness, JobStage.VERIFY)
        harness.clock.advance(int(LEASE_SECONDS) + 1)
        await harness.services.recover_leases.execute()

        await harness.loop(worker=OTHER_WORKER).run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.SUCCEEDED
        assert job.attempts == 2
        assert harness.handler(JobStage.PROBE).calls == 1, "checkpointed; never repeated"
        assert harness.handler(JobStage.DOWNLOAD).calls == 1
        assert harness.handler(JobStage.VERIFY).calls == 2, "the stage that died is re-run"
        assert harness.handler(JobStage.DELIVER).calls == 1
        assert harness.workspace_directories() == []

    async def test_a_restarted_worker_recovers_its_own_job_without_waiting(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await crash_during(harness, JobStage.VERIFY)

        # No clock advance: the same identity comes back and takes its own work
        # rather than waiting a full lease period for it to lapse.
        recovery = await harness.runtime().start()

        assert recovery.reclaimed == (job_id.value,)
        assert (await harness.job(job_id)).status is JobStatus.QUEUED

        await harness.loop().run_once()
        assert (await harness.job(job_id)).status is JobStatus.SUCCEEDED

    async def test_a_crash_before_any_stage_finishes_starts_over(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await crash_during(harness, JobStage.PROBE)
        harness.clock.advance(int(LEASE_SECONDS) + 1)
        await harness.services.recover_leases.execute()

        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.handler(JobStage.PROBE).calls == 2

    async def test_a_crash_after_the_last_stage_closes_the_job_without_redoing_it(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        # Everything ran and was checkpointed; the process died before the job
        # could be marked complete.
        claimed = await harness.services.claim.execute(worker=WORKER)
        assert claimed is not None
        current = claimed
        for stage in DEFAULT_STAGE_PLAN:
            current = current.with_checkpoint(
                await harness.services.checkpoint.execute(current, stage)
            )

        harness.clock.advance(int(LEASE_SECONDS) + 1)
        await harness.services.recover_leases.execute()
        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.job(job_id)).status is JobStatus.SUCCEEDED
        assert all(handler.calls == 0 for handler in harness.handlers), "nothing was redone"

    async def test_a_job_that_kills_every_worker_eventually_stops(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue(max_attempts=2)
        harness.handler(JobStage.PROBE).error = SuddenDeath()

        for _ in range(2):
            with pytest.raises(SuddenDeath):
                await harness.loop().run_once()
            harness.clock.advance(int(LEASE_SECONDS) + 1)
            recovery = await harness.services.recover_leases.execute()

        job = await harness.job(job_id)
        assert recovery.abandoned == (job_id.value,)
        assert job.status is JobStatus.FAILED
        assert job.attempts == 2
        assert await harness.services.claim.execute(worker=OTHER_WORKER) is None


class TestNoDuplicateExecution:
    async def test_two_workers_cannot_resume_the_same_job(self, harness: WorkerHarness) -> None:
        await crash_during(harness, JobStage.VERIFY)
        harness.clock.advance(int(LEASE_SECONDS) + 1)
        await harness.services.recover_leases.execute()

        first = await harness.services.claim.execute(worker=WORKER)
        second = await harness.services.claim.execute(worker=OTHER_WORKER)

        assert first is not None
        assert second is None

    async def test_a_repeated_sweep_does_not_requeue_a_job_twice(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await crash_during(harness, JobStage.VERIFY)
        harness.clock.advance(int(LEASE_SECONDS) + 1)

        first = await harness.services.recover_leases.execute()
        second = await harness.services.recover_leases.execute()

        assert first.reclaimed == (job_id.value,)
        assert second.total == 0
        assert (await harness.job(job_id)).attempts == 1
