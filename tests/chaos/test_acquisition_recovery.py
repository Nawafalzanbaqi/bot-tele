"""The pipeline is interrupted in every way it can be, and finishes anyway.

The crash is simulated the only honest way available in-process: a stage raises
something no handler catches, so the attempt is abandoned **without settling and
without releasing its lease** - precisely the state a power cut leaves behind.
Recovery then runs through real code: the lease lapses, the sweep takes it back,
a worker claims it and resumes from the last checkpoint.

The properties being defended, in one list:

* **No work is repeated needlessly.** A probe that was checkpointed is not paid
  for twice.
* **No work is skipped wrongly.** A lease belongs to one attempt, so a job that
  resumes into an empty one downloads again rather than delivering nothing.
* **Nothing is delivered twice.** The receipt is the record, and it is durable.
* **Nothing is deleted before it is safe.** The local copy goes only after a
  receipt exists.
* **Nothing is left on disk.** Not after a success, a failure, a cancellation or
  a crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import ProviderError
from mediahub.application.download.queue import JobStage
from mediahub.domain.download.enums import JobStatus
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN
from tests.support.download_fakes import FakeDownloader
from tests.support.pipeline_fakes import PRINCIPAL, PipelineHarness, request_cancellation
from tests.support.worker_fakes import OTHER_WORKER, WORKER

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
    progress_interval_seconds=0.0,
)


class PowerLoss(BaseException):
    """Stands in for the process disappearing.

    A ``BaseException`` on purpose: the worker's own safety nets catch
    ``Exception``, and this must get past all of them, exactly as losing power
    does.
    """


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    built = PipelineHarness.build(tmp_path)
    built.worker.timings = FAST
    return built


async def cut_power_during(harness: PipelineHarness, stage: JobStage) -> JobId:
    """Run a job until ``stage`` kills the worker, and return the job."""
    job_id = await harness.worker.enqueue()
    harness.handler(stage).error = PowerLoss()

    with pytest.raises(PowerLoss):
        await harness.loop().run_once()

    harness.handler(stage).error = None
    return job_id


async def recover(harness: PipelineHarness) -> None:
    """Let the lease lapse and take it back, as a scheduler sweep would."""
    harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
    await harness.worker.services.recover_leases.execute()


class TestPowerLossDuringDownload:
    async def test_the_bytes_and_the_lease_go_together(self, harness: PipelineHarness) -> None:
        await cut_power_during(harness, JobStage.DOWNLOAD)

        assert harness.worker.workspace_directories() == []

    async def test_only_the_probe_is_remembered(self, harness: PipelineHarness) -> None:
        job_id = await cut_power_during(harness, JobStage.DOWNLOAD)

        assert harness.completed_stages(job_id) == (JobStage.PROBE,)
        assert harness.state_of(job_id).is_probed is True

    async def test_the_resumed_job_downloads_but_does_not_probe_again(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await cut_power_during(harness, JobStage.DOWNLOAD)
        await recover(harness)

        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.downloader.probe_calls == 1, "checkpointed; never paid for twice"
        assert harness.downloader.fetch_calls == 1
        assert harness.destination.calls == 1


class TestPowerLossAfterDownload:
    async def test_the_checkpoint_survives_but_the_bytes_do_not(
        self, harness: PipelineHarness
    ) -> None:
        # The exact state that makes a naive pipeline deliver nothing: the
        # checkpoint says "downloaded" and the lease it referred to is gone.
        job_id = await cut_power_during(harness, JobStage.VERIFY)

        assert harness.completed_stages(job_id) == (JobStage.PROBE, JobStage.DOWNLOAD)
        assert harness.state_of(job_id).artifact is not None
        assert harness.worker.workspace_directories() == []

    async def test_the_resumed_job_fetches_the_bytes_again_and_finishes(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await cut_power_during(harness, JobStage.VERIFY)
        await recover(harness)

        await harness.loop(worker=OTHER_WORKER).run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.SUCCEEDED
        assert job.attempts == 2, "the crash still cost the attempt it consumed"
        assert harness.downloader.fetch_calls == 2, "a lease belongs to one attempt"
        assert harness.destination.calls == 1
        assert harness.worker.workspace_directories() == []

    async def test_the_artifact_is_recorded_against_the_lease_that_holds_it(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await cut_power_during(harness, JobStage.VERIFY)
        first_lease = harness.state_of(job_id).lease_id
        await recover(harness)

        await harness.loop(worker=OTHER_WORKER).run_once()

        assert harness.state_of(job_id).lease_id != first_lease


class TestPowerLossAfterDelivery:
    async def test_the_receipt_is_durable(self, harness: PipelineHarness) -> None:
        job_id = await cut_power_during(harness, JobStage.CLEANUP)

        receipt = harness.state_of(job_id).receipt
        assert receipt is not None
        assert receipt.remote_id == "fake-ref-1"

    async def test_the_resumed_job_does_not_deliver_a_second_time(
        self, harness: PipelineHarness
    ) -> None:
        # The failure a user would actually notice: the same file arriving
        # twice because a worker died between the upload and the checkpoint.
        job_id = await cut_power_during(harness, JobStage.CLEANUP)
        await recover(harness)

        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 1
        assert harness.downloader.fetch_calls == 1, "nothing was re-downloaded to re-send"
        assert len(await harness.journal.recent(PRINCIPAL)) == 1

    async def test_a_crash_after_every_stage_only_closes_the_job(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        claimed = await harness.worker.services.claim.execute(worker=WORKER)
        assert claimed is not None
        current = claimed
        for stage in DEFAULT_STAGE_PLAN:
            current = current.with_checkpoint(
                await harness.worker.services.checkpoint.execute(current, stage)
            )
        await recover(harness)

        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.calls() == {stage.value: 0 for stage in DEFAULT_STAGE_PLAN}


class TestPartialDownloadRecovery:
    async def test_an_interrupted_transfer_is_continued_rather_than_restarted(
        self, tmp_path: Path
    ) -> None:
        # Restarting a 2 GB download after a 90% failure is the difference
        # between a usable product and an unusable one.
        engine = FakeDownloader(
            fetch_error=ProviderError("the source went away"),
            fetch_error_times=1,
            leave_partial=True,
        )
        harness = PipelineHarness.build(tmp_path, downloader=engine)
        harness.worker.timings = FAST
        job_id = await harness.worker.enqueue(max_attempts=3, backoff_seconds=0)

        # The first attempt fails inside the stage, so the lease survives the
        # attempt and the second try continues into it.
        with harness.lease(label=f"job-{job_id}") as scope:
            with pytest.raises(ProviderError):
                await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

            state = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

        assert engine.resumptions == [False, True], "the second attempt continued the first"
        assert state.artifact is not None

    async def test_the_engine_is_always_told_it_may_resume(self, harness: PipelineHarness) -> None:
        await harness.worker.enqueue()

        await harness.loop().run_once()

        assert all(request.resume for request in harness.downloader.requests)

    async def test_a_retry_after_a_failed_transfer_still_completes(self, tmp_path: Path) -> None:
        engine = FakeDownloader(
            fetch_error=ProviderError("connection reset"),
            fetch_error_times=1,
            leave_partial=True,
        )
        harness = PipelineHarness.build(tmp_path, downloader=engine)
        harness.worker.timings = FAST
        job_id = await harness.worker.enqueue(max_attempts=3, backoff_seconds=0)

        await harness.loop().run_once()
        harness.worker.clock.advance(1)
        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.worker.workspace_directories() == []


class TestCancellation:
    async def test_a_job_cancelled_before_it_starts_never_runs(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        await harness.worker.queue.request_cancellation(job_id, now=harness.worker.clock.now())
        claimed = await harness.worker.services.claim.execute(worker=WORKER)
        assert claimed is None, "a cancelled job is not claimable"

    @pytest.mark.parametrize(
        "stage",
        [JobStage.PROBE, JobStage.DOWNLOAD, JobStage.VERIFY, JobStage.DELIVER],
    )
    async def test_cancellation_is_honoured_at_every_stage(
        self, harness: PipelineHarness, stage: JobStage
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.handler(stage).before = request_cancellation

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.CANCELLED
        assert harness.worker.workspace_directories() == []

    async def test_a_cancelled_job_is_not_delivered(self, harness: PipelineHarness) -> None:
        await harness.worker.enqueue()
        harness.handler(JobStage.VERIFY).before = request_cancellation

        await harness.loop().run_once()

        assert harness.destination.calls == 0
        assert len(await harness.journal.recent(PRINCIPAL)) == 0

    async def test_a_delivery_that_already_happened_is_not_undone(
        self, harness: PipelineHarness
    ) -> None:
        # Telling someone their delivery was cancelled after it arrived would
        # be a lie, so a cancellation during the last stage is not honoured.
        job_id = await harness.worker.enqueue()
        harness.handler(JobStage.CLEANUP).before = request_cancellation

        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 1

    async def test_a_drain_hands_the_job_back_without_charging_it(
        self, harness: PipelineHarness
    ) -> None:
        # A nightly reboot must not slowly exhaust the retry budget of healthy
        # work.
        job_id = await harness.worker.enqueue()
        loop = harness.loop()
        harness.handler(JobStage.DOWNLOAD).before = lambda context: loop.drain()

        await loop.run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 0, "the attempt was refunded"
        assert harness.completed_stages(job_id) == (JobStage.PROBE,)

    async def test_a_drained_job_resumes_where_it_stopped(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()
        loop = harness.loop()
        harness.handler(JobStage.DOWNLOAD).before = lambda context: loop.drain()
        await loop.run_once()

        harness.handler(JobStage.DOWNLOAD).before = None
        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.downloader.probe_calls == 1


class TestDuplicateAcquisition:
    async def test_two_jobs_for_the_same_source_are_each_delivered_once(
        self, harness: PipelineHarness
    ) -> None:
        first = await harness.worker.enqueue()
        second = await harness.worker.enqueue()
        loop = harness.loop()

        await loop.run_once()
        await loop.run_once()

        assert (await harness.worker.job(first)).status is JobStatus.SUCCEEDED
        assert (await harness.worker.job(second)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 2
        assert len(await harness.journal.recent(PRINCIPAL, limit=10)) == 2

    async def test_one_job_never_becomes_two_deliveries(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        await harness.loop().run_once()
        # Running the loop again must find nothing: the job is closed.
        assert await harness.loop().run_once() is False

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 1

    async def test_a_reclaimed_job_cannot_be_run_by_two_workers(
        self, harness: PipelineHarness
    ) -> None:
        await cut_power_during(harness, JobStage.VERIFY)
        await recover(harness)

        first = await harness.worker.services.claim.execute(worker=WORKER)
        second = await harness.worker.services.claim.execute(worker=OTHER_WORKER)

        assert first is not None
        assert second is None

    async def test_a_job_that_keeps_killing_workers_eventually_stops(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue(max_attempts=2)
        harness.handler(JobStage.DOWNLOAD).error = PowerLoss()

        for _ in range(2):
            with pytest.raises(PowerLoss):
                await harness.loop().run_once()
            await recover(harness)

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.FAILED
        assert job.attempts == 2
        assert harness.destination.calls == 0
        assert harness.worker.workspace_directories() == []
