"""The things that go wrong while the process is perfectly healthy.

None of these is a crash. The database is busy, the network drops, Telegram
takes too long to answer, the source refuses, the clock jumps, memory runs
short. Each is ordinary weather for an appliance that runs for years, and each
has exactly one correct response - which is what these tests pin down.

The distinction they all turn on is **transient versus permanent**. Getting it
wrong in one direction retries a dead link for ever; getting it wrong in the
other abandons a job because a router rebooted. That judgement lives in the
error taxonomy, and these tests are what stop it drifting.

*A note on "SQLite busy".* This build's persistence backends are PostgreSQL and
the in-process store; the SQLite adapter of the phase-02 design is not built
(see ``ARCHITECTURE.md`` §8). The equivalent condition - the store refusing a
write that will succeed on a retry - is exercised against the backend that
actually ships, because a test written against an adapter nobody runs proves
nothing about this deployment.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from mediahub.application.common.cancellation import CancellationSource
from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    DeliveryRateLimitedError,
    ProviderUnavailableError,
    TargetUnreachableError,
)
from mediahub.application.download.errors import (
    DownloadTimeoutError,
    LeaseLostError,
    MetadataUnavailableError,
    ProviderError,
)
from mediahub.application.download.queue import (
    Checkpoint,
    ClaimedJob,
    JobStage,
    Lease,
    StageProgress,
)
from mediahub.application.download.use_cases.heartbeat_job import HeartbeatJob
from mediahub.application.download.use_cases.report_job_progress import ReportJobProgress
from mediahub.domain.download.enums import FailureKind, JobStatus
from mediahub.domain.download.value_objects import JobId
from mediahub.infrastructure.delivery.shared.measured_reader import MeasuredReader
from mediahub.infrastructure.persistence.memory.database import InMemoryDatabase
from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
from mediahub.infrastructure.persistence.memory.queue import InMemoryJobQueue
from mediahub.presentation.worker.heartbeat import Heartbeat
from mediahub.presentation.worker.progress import ProgressRegistry
from tests.conftest import FrozenClock
from tests.support.pipeline_fakes import PipelineHarness
from tests.support.worker_fakes import WORKER

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.application.download.queue import ClaimedJob, Lease, LeaseState

pytestmark = [pytest.mark.failure, pytest.mark.integration]

DATABASE_LOCKED = "database is locked"
JOB_LABEL = "job"
OTHER_OWNER = "another-worker"


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    return PipelineHarness.build(tmp_path)


class TestDatabaseBusy:
    async def test_a_busy_store_costs_the_attempt_and_not_the_worker(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        busy = TimeoutError("database is locked")
        harness.handler(JobStage.PROBE).error = busy

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.QUEUED, "a lock contends; it does not condemn"
        assert harness.worker.leases_in_use() == 0

    async def test_a_busy_store_recovers_on_the_next_attempt(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.handler(JobStage.PROBE).error = TimeoutError("database is locked")
        harness.handler(JobStage.PROBE).error_times = 1

        await harness.loop().run_once()
        harness.worker.clock.advance(60)
        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED

    async def test_a_heartbeat_that_cannot_reach_the_store_keeps_ticking(
        self, harness: PipelineHarness
    ) -> None:
        # If the heartbeat died here, the lease would quietly stop being renewed
        # while the job kept running - and the job would be executed twice.
        job_id = await harness.worker.enqueue()
        claimed = await harness.worker.services.claim.execute(worker=WORKER)
        assert claimed is not None

        heartbeat = _heartbeat_over(
            harness, claimed, queue=LockedQueue(harness.worker.unit_of_work.database)
        )

        await heartbeat.tick()
        await heartbeat.tick()

        assert not heartbeat.lease_lost, "a blip is weather, not a lost lease"
        assert (await harness.worker.job(job_id)).status is JobStatus.RUNNING

    async def test_a_reclaimed_lease_does_stop_the_heartbeat(
        self, harness: PipelineHarness
    ) -> None:
        await harness.worker.enqueue()
        claimed = await harness.worker.services.claim.execute(worker=WORKER)
        assert claimed is not None

        heartbeat = _heartbeat_over(
            harness, claimed, queue=ReclaimedQueue(harness.worker.unit_of_work.database)
        )

        await heartbeat.tick()

        assert heartbeat.lease_lost, "someone else owns this job now; stop touching it"


class LockedQueue(InMemoryJobQueue):
    """A store that refuses writes the way a locked database refuses them."""

    async def extend_lease(
        self, lease: Lease, *, lease_seconds: float, now: datetime
    ) -> LeaseState:
        raise TimeoutError(DATABASE_LOCKED)


class ReclaimedQueue(InMemoryJobQueue):
    """A store that has already given the job to somebody else."""

    async def extend_lease(
        self, lease: Lease, *, lease_seconds: float, now: datetime
    ) -> LeaseState:
        raise LeaseLostError(lease.job_id, OTHER_OWNER)


def _heartbeat_over(
    harness: PipelineHarness, claimed: ClaimedJob, *, queue: InMemoryJobQueue
) -> Heartbeat:
    """Assemble a heartbeat over ``harness`` whose store misbehaves."""
    return Heartbeat(
        claimed,
        extend=HeartbeatJob(queue=queue, clock=harness.worker.clock, lease_seconds=120.0),
        report_progress=ReportJobProgress(
            queue=queue,
            unit_of_work=harness.worker.unit_of_work,
            clock=harness.worker.clock,
        ),
        progress=ProgressRegistry(clock=harness.worker.clock),
        cancellation=CancellationSource(),
        clock=harness.worker.clock,
        interval_seconds=0.0,
    )


class TestNetworkDisconnect:
    async def test_a_source_that_cannot_be_reached_is_retried(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.downloader.probe_error = ProviderError("the network is down")

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert ProviderError.kind is FailureKind.TRANSIENT

    async def test_a_retry_after_a_disconnect_starts_the_transfer_again(
        self, harness: PipelineHarness
    ) -> None:
        # Stated rather than assumed, because it is the sharpest edge in the
        # design. A workspace lease belongs to **one attempt**, so the partial
        # file a dropped connection left behind is deleted along with the lease
        # that held it, and the next attempt starts from zero. Resumption is
        # real *within* one fetch - the engine's own fragment retries - and not
        # across attempts. On a slow connection that is the difference between
        # losing minutes and losing an hour, and an operator sizing
        # `download_timeout_seconds` needs to know which one this is.
        job_id = await harness.worker.enqueue()
        harness.downloader.fetch_error = ProviderError("connection reset")
        harness.downloader.fetch_error_times = 1
        harness.downloader.leave_partial = True

        await harness.loop().run_once()
        harness.worker.clock.advance(60)
        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.downloader.resumptions == [False, False]

    async def test_a_stage_re_run_inside_one_attempt_does_resume(
        self, harness: PipelineHarness
    ) -> None:
        # The other half of the same rule: while the lease is alive, its bytes
        # are still there, so re-entering the download step continues rather
        # than restarting.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            harness.downloader.leave_partial = True
            harness.downloader.fetch_error = ProviderError("connection reset")
            harness.downloader.fetch_error_times = 1
            with pytest.raises(ProviderError):
                await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

            harness.downloader.fetch_error = None
            await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

        assert harness.downloader.resumptions == [False, True], "the same lease still had them"

    async def test_a_disconnect_leaves_no_partial_artifact_visible(
        self, harness: PipelineHarness
    ) -> None:
        await harness.worker.enqueue()
        harness.downloader.fetch_error = ProviderError("connection reset")
        harness.downloader.leave_partial = True

        await harness.loop().run_once()

        assert harness.worker.workspace_directories() == []


class TestTelegramTimeout:
    async def test_a_destination_that_times_out_is_retried_not_abandoned(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.destination.fail_with = ProviderUnavailableError("upload timed out")

        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED

    async def test_a_timeout_does_not_re_download_what_is_already_verified(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.destination.fail_with = ProviderUnavailableError("upload timed out")
        harness.destination.fail_times = 1

        await harness.loop().run_once()
        harness.worker.clock.advance(60)
        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.handler(JobStage.PROBE).calls == 1, "probing is checkpointed"

    async def test_a_rate_limit_is_obeyed_rather_than_guessed_at(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.destination.fail_with = DeliveryRateLimitedError(
            "slow down", retry_after_seconds=90.0
        )

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert DeliveryRateLimitedError.kind is FailureKind.TRANSIENT

    async def test_a_conversation_that_no_longer_exists_is_not_retried(
        self, harness: PipelineHarness
    ) -> None:
        # Permanent. Retrying a deleted chat for three attempts wastes three
        # downloads to learn what the first refusal already said.
        job_id = await harness.worker.enqueue()
        harness.destination.fail_with = TargetUnreachableError("chat not found")

        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.FAILED

    async def test_a_file_the_destination_cannot_accept_stops_immediately(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.destination.fail_with = ArtifactTooLargeError(1024, 999_999)

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is JobStatus.FAILED
        assert ArtifactTooLargeError.kind is FailureKind.POLICY


class TestDownloadEngineFailure:
    async def test_a_source_that_refuses_permanently_is_not_retried(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.downloader.probe_error = MetadataUnavailableError("this video is private")

        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.FAILED

    async def test_a_transfer_that_exceeds_its_budget_is_retried(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.downloader.fetch_error = DownloadTimeoutError("exceeded its 3600s budget")

        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED

    async def test_an_engine_that_produces_nothing_does_not_look_like_success(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.downloader.size_bytes = 0

        await harness.loop().run_once()

        job = await harness.worker.job(job_id)
        assert job.status is not JobStatus.SUCCEEDED
        assert harness.destination.calls == 0, "nothing empty was ever sent"

    async def test_an_unrecognised_engine_error_is_transient_not_fatal(
        self, harness: PipelineHarness
    ) -> None:
        # An unfamiliar failure gets one honest attempt later rather than being
        # written off - and, critically, does not stop the worker.
        job_id = await harness.worker.enqueue()
        harness.downloader.fetch_error = RuntimeError("something nobody anticipated")

        await harness.loop().run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED

        harness.downloader.fetch_error = None
        harness.worker.clock.advance(60)
        await harness.loop().run_once()
        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED


class TestClockJumps:
    """NTP answers, and time moves sideways.

    A Raspberry Pi has no battery-backed clock. It boots in the past and is
    corrected the moment the network answers, then nudged for the rest of its
    life. Anything that measures a duration by subtracting two wall-clock
    readings has to survive that, and the direction that hurts is *backwards*.
    """

    def test_a_backward_step_does_not_stall_lease_renewal(self) -> None:
        clock = FrozenClock()
        heartbeat = _detached_heartbeat(clock)

        clock.advance(-3600)  # the clock was an hour fast; NTP corrects it

        assert heartbeat.renewal_due is True, "otherwise the lease dies under a live job"

    def test_without_a_jump_the_interval_is_still_respected(self) -> None:
        clock = FrozenClock()
        heartbeat = _detached_heartbeat(clock)

        clock.advance(5)
        assert heartbeat.renewal_due is False
        clock.advance(30)
        assert heartbeat.renewal_due is True

    def test_a_backward_step_re_anchors_progress_instead_of_muting_it(self) -> None:
        clock = FrozenClock()
        registry = ProgressRegistry(clock=clock, min_interval_seconds=5.0, min_percent_step=50.0)
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=10))
        assert registry.take_due() is not None

        clock.advance(-3600)
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=20))

        assert registry.take_due() is not None, "progress must not go silent for an hour"

    def test_a_forward_step_does_not_produce_a_flood(self) -> None:
        clock = FrozenClock()
        registry = ProgressRegistry(clock=clock, min_interval_seconds=5.0)
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=10))
        registry.take_due()

        clock.advance(86_400)
        # Nothing new to say, however much time appears to have passed.
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=10))

        assert registry.take_due() is None


class TestStalledTransfers:
    def test_a_stalled_download_stops_writing_the_same_number(self) -> None:
        # One durable write every five seconds, for hours, all saying 41%, is
        # exactly the SD-card wear the throttle exists to prevent.
        clock = FrozenClock()
        registry = ProgressRegistry(clock=clock, min_interval_seconds=5.0)
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=4096))
        assert registry.take_due() is not None

        writes = 0
        for _ in range(100):
            clock.advance(5)
            registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=4096))
            if registry.take_due() is not None:
                writes += 1

        assert writes == 0, "a stall is not news"

    def test_the_final_observation_is_still_never_dropped(self) -> None:
        clock = FrozenClock()
        registry = ProgressRegistry(clock=clock, min_interval_seconds=5.0)
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=4096))
        registry.take_due()
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=4096))

        assert registry.take_final() is not None

    def test_movement_resumes_reporting_immediately(self) -> None:
        clock = FrozenClock()
        registry = ProgressRegistry(clock=clock, min_interval_seconds=5.0)
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=4096))
        registry.take_due()

        clock.advance(5)
        registry.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=8192))

        assert registry.take_due() is not None


class TestLowMemory:
    def test_a_whole_stream_read_is_refused_above_the_ceiling(self, tmp_path: Path) -> None:
        # The client library reads the entire file into memory before uploading.
        # Without a ceiling that is an OOM kill, which loses the worker as well
        # as the job; with one it is a classified, non-retryable refusal.
        payload = tmp_path / "big.bin"
        payload.write_bytes(b"x" * 8192)

        with payload.open("rb") as handle:
            reader = MeasuredReader(
                handle, chunk_bytes=1024, max_buffer_bytes=4096, provider="telegram"
            )
            with pytest.raises(ArtifactTooLargeError) as refusal:
                reader.read()

        assert refusal.value.kind is FailureKind.POLICY, "retrying will not find more memory"
        assert refusal.value.provider == "telegram"

    def test_a_stream_within_the_ceiling_is_unaffected(self, tmp_path: Path) -> None:
        payload = tmp_path / "ok.bin"
        payload.write_bytes(b"x" * 4096)

        with payload.open("rb") as handle:
            reader = MeasuredReader(handle, chunk_bytes=1024, max_buffer_bytes=1024 * 1024)
            data = reader.read()

        assert len(data) == 4096
        assert reader.bytes_read == 4096

    def test_chunked_reading_is_never_capped(self, tmp_path: Path) -> None:
        # A caller that reads in bounded chunks is doing the right thing and
        # must not be punished for the size of the file.
        payload = tmp_path / "big.bin"
        payload.write_bytes(b"x" * 8192)

        with payload.open("rb") as handle:
            reader = MeasuredReader(handle, max_buffer_bytes=1024)
            total = 0
            while chunk := reader.read(512):
                total += len(chunk)

        assert total == 8192

    def test_the_digest_still_covers_everything_that_passed(self, tmp_path: Path) -> None:
        payload = tmp_path / "ok.bin"
        payload.write_bytes(b"payload" * 100)

        with payload.open("rb") as handle:
            reader = MeasuredReader(handle, chunk_bytes=64)
            reader.read()

        assert reader.fingerprint().digest == hashlib.sha256(b"payload" * 100).hexdigest()


def _detached_heartbeat(clock: FrozenClock) -> Heartbeat:
    """Return a heartbeat over a queue nothing will ask anything of.

    The clock-jump tests never tick it - they ask only whether a renewal is
    *due*, which is the decision a backward step used to get wrong.
    """
    queue = InMemoryJobQueue(InMemoryDatabase())
    return Heartbeat(
        _claim_at(clock.now()),
        extend=HeartbeatJob(queue=queue, clock=clock, lease_seconds=120.0),
        report_progress=ReportJobProgress(
            queue=queue, unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock
        ),
        progress=ProgressRegistry(clock=clock),
        cancellation=CancellationSource(),
        clock=clock,
        interval_seconds=30.0,
    )


def _claim_at(moment: datetime) -> ClaimedJob:
    """Return a claim whose lease was acquired at ``moment``."""
    job_id = JobId(UUID(int=7))
    return ClaimedJob(
        lease=Lease(
            job_id=job_id,
            owner=WORKER,
            acquired_at=moment,
            expires_at=moment + timedelta(seconds=120),
        ),
        checkpoint=Checkpoint(),
        attempt=1,
    )
