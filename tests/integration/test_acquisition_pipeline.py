"""A link goes in, a file comes out, and the device forgets it.

The whole pipeline, through the real claim loop and the real runtime: claim,
lease, probe, download, verify, deliver, record, release, complete. The only
fakes are the internet and the destination.

What these tests are really asserting is the product's central promise
(``docs/architecture/07-download-pipeline.md`` §7.5): after a successful
acquisition the destination holds the bytes, the history holds a few hundred
bytes of metadata, and the disk holds nothing at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.application.common.cancellation import NullCancellation
from mediahub.application.delivery.errors import (
    DeliveryRateLimitedError,
    TargetUnreachableError,
)
from mediahub.application.download.errors import MetadataUnavailableError, ProviderError
from mediahub.application.download.queue import JobStage
from mediahub.domain.download.enums import JobStatus
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN
from tests.support.delivery_fakes import FakeDeliveryProvider
from tests.support.download_fakes import FakeDownloader
from tests.support.pipeline_fakes import PRINCIPAL, PipelineHarness
from tests.support.worker_fakes import WORKER

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.application.download.dto import JobSettlement

pytestmark = pytest.mark.integration

FAST = WorkerTimings(
    lease_seconds=20.0,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.0,
)


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    built = PipelineHarness.build(tmp_path, downloader=FakeDownloader(chunks=(1024, 4096)))
    built.worker.timings = FAST
    return built


class TestTheHappyPath:
    async def test_a_queued_job_is_acquired_delivered_and_completed(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        assert await harness.loop().run_once() is True

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.SUCCEEDED
        assert job.attempts == 1

    async def test_every_stage_runs_once_and_in_order(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        await harness.loop().run_once()

        assert harness.completed_stages(job_id) == DEFAULT_STAGE_PLAN
        assert harness.calls() == {stage.value: 1 for stage in DEFAULT_STAGE_PLAN}

    async def test_the_destination_ends_up_with_the_file(self, harness: PipelineHarness) -> None:
        await harness.worker.enqueue()

        await harness.loop().run_once()

        assert len(harness.destination.delivered) == 1
        assert harness.destination.delivered[0].artifact.size_bytes == 4096

    async def test_the_receipt_is_persisted_in_the_checkpoint(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        await harness.loop().run_once()

        receipt = harness.state_of(job_id).receipt
        assert receipt is not None
        assert receipt.provider == "fake"
        assert receipt.remote_id == "fake-ref-1"
        assert receipt.can_serve_back is True
        assert receipt.confirmed_at is not None

    async def test_the_acquisition_is_remembered_after_the_bytes_are_gone(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        job = await harness.worker.job(job_id)

        await harness.loop().run_once()

        entries = await harness.journal.recent(PRINCIPAL)
        assert len(entries) == 1
        assert entries[0].url == str(job.source_url)
        assert entries[0].bytes_delivered == 4096
        assert entries[0].message_id == "1"

    async def test_the_disk_holds_nothing_afterwards(self, harness: PipelineHarness) -> None:
        # The point of the whole design, asserted on a real filesystem.
        await harness.worker.enqueue()

        await harness.loop().run_once()

        assert harness.worker.workspace_directories() == []
        assert harness.worker.workspace.usage().used_bytes == 0

    async def test_progress_reaches_the_job_a_client_would_read(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.progress.downloaded_bytes == 4096
        assert job.progress.percentage == 100.0

    async def test_nothing_stays_leased(self, harness: PipelineHarness) -> None:
        await harness.worker.enqueue()

        await harness.loop().run_once()

        assert harness.worker.leases_in_use() == 0

    async def test_the_runtime_runs_the_pipeline_the_same_way(
        self, harness: PipelineHarness
    ) -> None:
        # The supervisor's slots are the same loop; asserting it here keeps the
        # composition honest rather than only the unit under it.
        job_id = await harness.worker.enqueue()
        runtime = harness.runtime()
        await runtime.start()

        assert await runtime._build_loop(0).run_once() is True
        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED


class TestSeveralJobs:
    async def test_a_queue_of_jobs_is_drained_one_at_a_time(self, harness: PipelineHarness) -> None:
        jobs = [await harness.worker.enqueue() for _ in range(3)]
        loop = harness.loop()

        for _ in jobs:
            assert await loop.run_once() is True

        for job_id in jobs:
            assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 3
        assert harness.worker.workspace_directories() == []

    async def test_each_job_gets_a_lease_of_its_own(self, harness: PipelineHarness) -> None:
        first = await harness.worker.enqueue()
        second = await harness.worker.enqueue()
        loop = harness.loop()

        await loop.run_once()
        await loop.run_once()

        assert harness.state_of(first).lease_id != harness.state_of(second).lease_id


class TestFailures:
    async def test_a_permanent_probe_failure_stops_the_job_without_retrying(
        self, harness: PipelineHarness
    ) -> None:
        # Retrying a private video three times with exponential backoff wastes
        # an hour and teaches the user the product is unreliable.
        job_id = await harness.worker.enqueue(max_attempts=3)
        harness.downloader.probe_error = MetadataUnavailableError("the video is private")

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.FAILED
        assert job.attempts == 1
        assert "metadata_unavailable" in (job.last_error or "")
        assert await harness.worker.services.claim.execute(worker=WORKER) is None

    async def test_a_transient_download_failure_is_retried_and_then_succeeds(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue(max_attempts=3, backoff_seconds=0)
        harness.downloader.fetch_error = ProviderError("the source reset the connection")
        harness.downloader.fetch_error_times = 1

        await harness.loop().run_once()
        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED

        harness.worker.clock.advance(1)
        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 1

    async def test_a_probe_is_not_repeated_when_the_download_is_retried(
        self, harness: PipelineHarness
    ) -> None:
        # The probe was checkpointed; retrying the transfer must not spend
        # another third-party call on it.
        await harness.worker.enqueue(max_attempts=3, backoff_seconds=0)
        harness.downloader.fetch_error = ProviderError("connection reset")
        harness.downloader.fetch_error_times = 1

        await harness.loop().run_once()
        harness.worker.clock.advance(1)
        await harness.loop().run_once()

        assert harness.downloader.probe_calls == 1
        assert harness.downloader.fetch_calls == 2

    async def test_a_destination_that_asks_us_to_slow_down_is_obeyed(self, tmp_path: Path) -> None:
        destination = FakeDeliveryProvider(
            fail_with=DeliveryRateLimitedError("slow down", retry_after_seconds=90.0),
            fail_times=1,
        )
        harness = PipelineHarness.build(tmp_path, destination=destination)
        job_id = await harness.worker.enqueue(max_attempts=3, backoff_seconds=5)

        settlement = await _settle(harness)

        assert settlement.retrying is True
        assert settlement.available_at is not None
        elapsed = settlement.available_at - harness.worker.clock.now()
        assert elapsed.total_seconds() == 90.0, "the destination's own delay outranks our backoff"
        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED

    async def test_a_destination_that_refuses_us_is_permanent(self, tmp_path: Path) -> None:
        destination = FakeDeliveryProvider(
            fail_with=TargetUnreachableError("that conversation no longer exists")
        )
        harness = PipelineHarness.build(tmp_path, destination=destination)
        job_id = await harness.worker.enqueue(max_attempts=3)

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.FAILED
        assert job.attempts == 1, "a permanent failure spends one attempt, not three"

    async def test_a_failed_job_leaves_nothing_on_disk(self, harness: PipelineHarness) -> None:
        await harness.worker.enqueue()
        harness.downloader.fetch_error = ProviderError("connection reset")

        await harness.loop().run_once()

        assert harness.worker.workspace_directories() == []

    async def test_a_failure_after_download_still_releases_the_lease(self, tmp_path: Path) -> None:
        harness = PipelineHarness.build(
            tmp_path, destination=FakeDeliveryProvider(fail_with=TargetUnreachableError("gone"))
        )
        await harness.worker.enqueue()

        await harness.loop().run_once()

        assert harness.worker.workspace_directories() == []
        assert harness.worker.leases_in_use() == 0

    async def test_the_stage_that_failed_is_named_on_the_job(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue(max_attempts=1)
        harness.downloader.fetch_error = ProviderError("connection reset")

        await harness.loop().run_once()

        assert harness.worker.queue.failure_of(job_id) is not None
        assert harness.worker.queue.failure_of(job_id).stage is JobStage.DOWNLOAD  # type: ignore[union-attr]


async def _settle(harness: PipelineHarness) -> JobSettlement:
    """Run one attempt through the executor and return how it was settled.

    Driven a level below the claim loop only so the test can read the
    settlement the loop discards.
    """
    claimed = await harness.worker.services.claim.execute(worker=WORKER)
    assert claimed is not None
    with harness.worker.workspace.lease(label="job") as scope:
        result = await harness.executor().execute(
            claimed,
            workspace=scope,
            cancellation=NullCancellation(),
            report=harness.observed.append,
        )
    assert result.failure is not None
    return await harness.worker.services.fail.execute(result.claim, result.failure)
