"""The process goes away, politely or otherwise.

Three ways a worker stops, and they are not the same event:

* ``SIGTERM`` - a deploy, a restart, an operator. The process is *asked*, so
  in-flight work stops at a stage boundary, is checkpointed and is handed back.
  The next worker continues from there within a poll interval.
* ``SIGKILL`` (``kill -9``, an OOM kill, a container that overran its stop
  timeout) - no notice at all. Identical, from the outside, to a power cut: the
  lease is left held and nothing was settled.
* **Restarting** - the same identity comes back. It should take its own work
  back immediately rather than waiting a full lease period for it to lapse,
  because that period is dead time on a device with one worker.

What matters most is that only the *first* of these is graceful, and the system
does not depend on it. Everything a clean shutdown achieves is an optimisation
over what the lease already guarantees, and these tests are what keep it that
way - a recovery path that only works when shutdown was polite is a recovery
path that does not work.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.queue import JobStage
from mediahub.application.workspace.use_cases.recover_workspaces import RecoverWorkspaces
from mediahub.domain.download.enums import JobStatus
from mediahub.domain.workspace.policies import RecoveryPolicy
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.stages.base import StageOutcome
from tests.support.pipeline_fakes import PipelineHarness
from tests.support.worker_fakes import OTHER_WORKER, WORKER

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.domain.download.value_objects import JobId
    from mediahub.presentation.worker.stages.base import StageContext

pytestmark = [pytest.mark.failure, pytest.mark.integration]

LEASE_SECONDS = 20.0
FAST = WorkerTimings(
    lease_seconds=LEASE_SECONDS,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.01,
    drain_grace_seconds=1.0,
)


class Sigkill(BaseException):
    """``kill -9``. There is no handler for this, which is the point."""


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    pipeline = PipelineHarness.build(tmp_path)
    pipeline.worker.timings = FAST
    return pipeline


async def killed_during(harness: PipelineHarness, stage: JobStage) -> JobId:
    """Run a job until ``stage``, then have the process disappear."""
    job_id = await harness.worker.enqueue()
    harness.handler(stage).error = Sigkill()
    with pytest.raises(Sigkill):
        await harness.loop().run_once()
    harness.handler(stage).error = None
    return job_id


class TestSigkill:
    async def test_the_lease_is_left_held_exactly_as_after_a_power_cut(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await killed_during(harness, JobStage.DOWNLOAD)

        state = await harness.worker.queue.state_of(job_id)

        assert state is not None
        assert state.lease.is_held_by(WORKER), "nothing was handed back; nothing could be"
        assert (await harness.worker.job(job_id)).status is JobStatus.RUNNING

    async def test_nobody_else_may_take_the_job_until_the_lease_lapses(
        self, harness: PipelineHarness
    ) -> None:
        await killed_during(harness, JobStage.DOWNLOAD)

        assert await harness.worker.services.claim.execute(worker=OTHER_WORKER) is None

    async def test_the_job_is_recovered_once_the_lease_lapses(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await killed_during(harness, JobStage.DOWNLOAD)

        harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
        await harness.worker.services.recover_leases.execute()
        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED

    async def test_recovery_takes_at_most_two_lease_periods(self, harness: PipelineHarness) -> None:
        # The bound an operator sizes `lease_seconds` against: one period for
        # the lease to lapse, one sweep to notice.
        job_id = await killed_during(harness, JobStage.DOWNLOAD)

        harness.worker.clock.advance(int(LEASE_SECONDS) - 1)
        assert (await harness.worker.services.recover_leases.execute()).total == 0

        harness.worker.clock.advance(2)
        assert (await harness.worker.services.recover_leases.execute()).reclaimed == (job_id.value,)

    async def test_no_workspace_survives_the_kill(self, harness: PipelineHarness) -> None:
        await killed_during(harness, JobStage.DOWNLOAD)

        # The lease context manager unwinds even for a BaseException, which is
        # the only reason a killed attempt does not leak its bytes.
        assert harness.worker.workspace_directories() == []


class TestGracefulShutdown:
    async def test_a_drained_job_is_handed_back_without_costing_an_attempt(
        self, harness: PipelineHarness
    ) -> None:
        # A deploy must not spend the retry budget of everything in flight.
        job_id = await harness.worker.enqueue()
        started = asyncio.Event()
        release = asyncio.Event()
        harness.handler(JobStage.DOWNLOAD).inner = _Blocking(
            JobStage.DOWNLOAD, release=release, started=started
        )
        loop = harness.loop()

        task = asyncio.create_task(loop.run())
        await asyncio.wait_for(started.wait(), timeout=5)
        loop.drain()
        release.set()
        await asyncio.wait_for(task, timeout=5)

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.QUEUED, "handed back, not failed"
        assert job.attempts == 0, "the attempt is refunded; a deploy is not the job's fault"

    async def test_a_drained_worker_leaves_no_workspace(self, harness: PipelineHarness) -> None:
        await harness.worker.enqueue()
        runtime = harness.runtime()

        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.05)
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        assert harness.worker.workspace_directories() == []

    async def test_a_second_signal_stops_waiting_for_the_drain(
        self, harness: PipelineHarness
    ) -> None:
        # An operator who sends SIGTERM twice is saying the polite path is not
        # working. Refusing to listen is how someone reaches for SIGKILL.
        release = asyncio.Event()
        started = asyncio.Event()
        await harness.worker.enqueue()
        harness.handler(JobStage.PROBE).inner = _Blocking(
            JobStage.PROBE, release=release, started=started
        )
        runtime = harness.runtime()

        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(started.wait(), timeout=5)
        runtime.shutdown.request()
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        release.set()
        assert runtime.shutdown.immediate

    async def test_stopping_an_idle_worker_is_immediate_and_clean(
        self, harness: PipelineHarness
    ) -> None:
        runtime = harness.runtime(slots=2)

        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.02)
        runtime.shutdown.request()
        await asyncio.wait_for(task, timeout=5)

        assert harness.worker.leases_in_use() == 0
        assert harness.worker.workspace_directories() == []


class _Blocking:
    """A stage handler that parks until a test lets it go.

    Stands in for the stage that will not wind down inside the grace period -
    a long transfer, a wedged socket - which is the case the second signal
    exists for.
    """

    def __init__(self, stage: JobStage, *, release: asyncio.Event, started: asyncio.Event) -> None:
        self.stage = stage
        self._release = release
        self._started = started

    async def execute(self, context: StageContext) -> StageOutcome:
        del context
        self._started.set()
        await self._release.wait()
        return StageOutcome()


class TestRestart:
    async def test_a_restarted_worker_takes_its_own_work_back_immediately(
        self, harness: PipelineHarness
    ) -> None:
        # Without this, a container restart stalls everything the previous
        # incarnation was holding for a full lease period.
        job_id = await killed_during(harness, JobStage.DOWNLOAD)

        recovery = await harness.runtime().start()

        assert recovery.reclaimed == (job_id.value,)
        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED

    async def test_the_restarted_worker_resumes_from_the_checkpoint(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await killed_during(harness, JobStage.DELIVER)
        done_before = harness.completed_stages(job_id)

        await harness.runtime().start()
        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        for stage in done_before:
            assert harness.handler(stage).calls == 1, f"{stage.value} was repeated"

    async def test_a_restart_purges_whatever_the_previous_process_left(
        self, harness: PipelineHarness
    ) -> None:
        root = harness.worker.workspace.root
        (root / "job-orphan").mkdir()
        (root / "job-orphan" / "leftover.bin").write_bytes(b"x" * 4096)

        report = RecoverWorkspaces(
            workspace=harness.worker.workspace,
            policy=RecoveryPolicy(delete_every_lease=True),
            owner=harness.worker.workspace.owner,
            clock=harness.worker.clock,
        ).execute()

        assert report.deleted
        assert harness.worker.workspace_directories() == []

    async def test_repeated_restarts_do_not_multiply_attempts(
        self, harness: PipelineHarness
    ) -> None:
        # A crash-looping container must not spend the retry budget of a job it
        # never actually got round to running.
        job_id = await harness.worker.enqueue(max_attempts=5)

        for _ in range(4):
            await harness.runtime().start()

        assert (await harness.worker.job(job_id)).attempts == 0
        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED

    async def test_repeated_restarts_converge_on_finishing_the_job(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue(max_attempts=5)

        for _ in range(3):
            runtime = harness.runtime()
            await runtime.start()
            task = asyncio.create_task(runtime.run())
            await asyncio.sleep(0.05)
            runtime.shutdown.request()
            await asyncio.wait_for(task, timeout=5)
            if (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED:
                break

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.worker.workspace_directories() == []


class TestSupervision:
    async def test_a_slot_that_dies_stops_the_process_instead_of_idling_on(
        self, harness: PipelineHarness
    ) -> None:
        # A worker whose only slot has died answers its liveness probe, holds
        # its identity and claims nothing. On an unattended device that is an
        # outage that lasts until somebody happens to look.
        runtime = harness.runtime()
        await runtime.start()
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.05)

        loops = runtime._loops
        assert loops
        loops[0].drain()

        await asyncio.wait_for(task, timeout=5)
        assert task.done(), "the supervisor noticed and wound the process down"
