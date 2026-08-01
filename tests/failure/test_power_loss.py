"""The power goes off. It goes off during each stage in turn.

A power cut is the failure this product was designed around, because on a device
plugged into a domestic socket it is not an incident - it is a Tuesday. There is
no signal, no unwinding and no chance to tidy up, so the only things that can
save the work are the ones already written down: the checkpoint, the manifest and
the lease.

The cut is simulated by raising a ``BaseException`` from inside a stage. That
gets past every safety net in the worker - which catch ``Exception`` - so the
attempt is abandoned without settling the job and without releasing its lease,
which is precisely the state the kernel leaves behind when the voltage drops.
Recovery then runs through the real code path.

What every test below asserts, in one sentence: **the job survives, it resumes
where it stopped, and it is never delivered twice.**
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.queue import JobStage
from mediahub.domain.download.enums import JobStatus
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN
from tests.support.pipeline_fakes import PipelineHarness
from tests.support.worker_fakes import OTHER_WORKER, WORKER

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.domain.download.value_objects import JobId

pytestmark = [pytest.mark.failure, pytest.mark.integration]

LEASE_SECONDS = 20.0
FAST = WorkerTimings(
    lease_seconds=LEASE_SECONDS,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.01,
)


class PowerCut(BaseException):
    """The voltage drops.

    A ``BaseException`` deliberately: the worker's own nets catch ``Exception``,
    and losing power gets past all of them.
    """


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    pipeline = PipelineHarness.build(tmp_path)
    pipeline.worker.timings = FAST
    return pipeline


async def cut_power_during(harness: PipelineHarness, stage: JobStage) -> JobId:
    """Run a job until ``stage``, then lose power. Returns the job."""
    job_id = await harness.worker.enqueue()
    harness.handler(stage).error = PowerCut()

    with pytest.raises(PowerCut):
        await harness.loop().run_once()

    harness.handler(stage).error = None
    return job_id


async def reboot_and_finish(harness: PipelineHarness) -> None:
    """Wait out the lease, sweep, and let a fresh worker carry on."""
    harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
    await harness.worker.services.recover_leases.execute()
    await harness.loop(worker=OTHER_WORKER).run_once()


@pytest.mark.parametrize("stage", DEFAULT_STAGE_PLAN, ids=lambda s: s.value)
class TestPowerLossDuringEveryStage:
    async def test_the_job_is_never_lost(self, harness: PipelineHarness, stage: JobStage) -> None:
        job_id = await cut_power_during(harness, stage)

        await reboot_and_finish(harness)

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED

    async def test_no_workspace_survives_the_reboot(
        self, harness: PipelineHarness, stage: JobStage
    ) -> None:
        await cut_power_during(harness, stage)

        await reboot_and_finish(harness)

        assert harness.worker.workspace_directories() == [], "a leaked lease is leaked disk"

    async def test_nothing_is_delivered_twice(
        self, harness: PipelineHarness, stage: JobStage
    ) -> None:
        await cut_power_during(harness, stage)

        await reboot_and_finish(harness)

        # The receipt is written in the same checkpoint as the stage that earned
        # it, so a cut anywhere either leaves both or neither.
        assert harness.destination.calls == 1, "the user is sent the file exactly once"

    async def test_completed_stages_are_not_repeated(
        self, harness: PipelineHarness, stage: JobStage
    ) -> None:
        job_id = await cut_power_during(harness, stage)
        finished_before = harness.completed_stages(job_id)

        await reboot_and_finish(harness)

        for done in finished_before:
            assert harness.handler(done).calls == 1, f"{done.value} ran again after the cut"


class TestPowerLossBetweenDeliveryAndCleanup:
    async def test_the_receipt_is_durable_before_anything_is_deleted(
        self, harness: PipelineHarness
    ) -> None:
        # The most dangerous moment in the pipeline: the bytes are at the
        # destination and the local copy is still on disk. Losing power here
        # must not be able to delete the local copy without the receipt, or a
        # failed delivery would have nothing left to retry from.
        job_id = await cut_power_during(harness, JobStage.CLEANUP)

        assert harness.state_of(job_id).receipt is not None
        assert JobStage.DELIVER in harness.completed_stages(job_id)

    async def test_the_resumed_job_does_not_upload_again(self, harness: PipelineHarness) -> None:
        await cut_power_during(harness, JobStage.CLEANUP)

        await reboot_and_finish(harness)

        assert harness.destination.calls == 1
        assert harness.handler(JobStage.DELIVER).calls == 1


class TestPowerLossBeforeAnythingIsCheckpointed:
    async def test_the_job_starts_over_rather_than_half_way(self, harness: PipelineHarness) -> None:
        job_id = await cut_power_during(harness, JobStage.PROBE)

        assert harness.completed_stages(job_id) == ()

        await reboot_and_finish(harness)

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.handler(JobStage.PROBE).calls == 2


class TestRepeatedPowerLoss:
    async def test_a_job_that_dies_every_time_is_eventually_abandoned(
        self, harness: PipelineHarness
    ) -> None:
        # A device that browns out under load would otherwise retry one job for
        # ever, and every retry costs a full download.
        job_id = await harness.worker.enqueue(max_attempts=2)
        harness.handler(JobStage.PROBE).error = PowerCut()

        for _ in range(2):
            with pytest.raises(PowerCut):
                await harness.loop().run_once()
            harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
            await harness.worker.services.recover_leases.execute()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.FAILED
        assert job.attempts == 2
        assert await harness.worker.services.claim.execute(worker=WORKER) is None

    async def test_repeated_cuts_leave_no_disk_behind(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue(max_attempts=4)
        harness.handler(JobStage.DOWNLOAD).error = PowerCut()

        for _ in range(3):
            with pytest.raises(PowerCut):
                await harness.loop().run_once()
            harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
            await harness.worker.services.recover_leases.execute()

        assert harness.worker.workspace_directories() == []
        assert (await harness.worker.job(job_id)).attempts == 3
