"""The rules a worker appears to apply, applied where they actually live.

Every decision the worker seems to make is tested here, without a worker: an
attempt is spent at claim time, a transient failure retries with backoff, a
permanent one does not, a graceful release refunds the attempt and a lease
reclaim does not. If these are right, the runtime above them only has to be
correct about *when* to call them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import LeaseLostError
from mediahub.application.download.failures import FailureReport
from mediahub.application.download.queue import JobStage, StageProgress
from mediahub.domain.download.enums import FailureKind, JobStatus
from mediahub.domain.download.errors import (
    DownloadJobNotFoundError,
    InvalidJobTransitionError,
)
from tests.support.worker_fakes import OTHER_WORKER, WORKER, WorkerHarness

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.application.download.queue import ClaimedJob

pytestmark = pytest.mark.unit

LEASE_SECONDS = 120.0


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness.build(tmp_path)


async def claim_one(harness: WorkerHarness) -> ClaimedJob:
    """Claim the next job, asserting that there was one."""
    claimed = await harness.services.claim.execute(worker=WORKER)
    assert claimed is not None
    return claimed


def transient(code: str = "provider_error") -> FailureReport:
    return FailureReport(kind=FailureKind.TRANSIENT, code=code, message="try later")


def permanent(code: str = "unsupported_provider") -> FailureReport:
    return FailureReport(kind=FailureKind.PERMANENT, code=code, message="never")


class TestClaimJob:
    async def test_claiming_starts_the_job_and_spends_an_attempt(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()

        claimed = await claim_one(harness)

        job = await harness.job(job_id)
        assert claimed.job_id == job_id
        assert claimed.attempt == 1
        assert job.status is JobStatus.RUNNING
        assert job.attempts == 1
        assert "DownloadStarted" in harness.events.names()

    async def test_an_empty_queue_claims_nothing(self, harness: WorkerHarness) -> None:
        assert await harness.services.claim.execute(worker=WORKER) is None

    async def test_two_workers_cannot_claim_the_same_job(self, harness: WorkerHarness) -> None:
        await harness.enqueue()

        first = await harness.services.claim.execute(worker=WORKER)
        second = await harness.services.claim.execute(worker=OTHER_WORKER)

        assert first is not None
        assert second is None

    async def test_a_job_that_vanished_surrenders_its_lease(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.delete(job_id)
            await uow.commit()

        assert await harness.services.claim.execute(worker=WORKER) is None
        assert harness.leases_in_use() == 0

    async def test_a_job_cancelled_before_it_ran_is_not_started(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        job = await harness.job(job_id)
        job.cancel(now=harness.clock.now())
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.save(job)
            await uow.commit()

        assert await harness.services.claim.execute(worker=WORKER) is None
        assert (await harness.job(job_id)).status is JobStatus.CANCELLED


class TestHeartbeat:
    async def test_a_heartbeat_extends_the_lease(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        claimed = await claim_one(harness)
        harness.clock.advance(30)

        state = await harness.services.heartbeat.execute(claimed.lease)

        assert state.lease.expires_at > claimed.lease.expires_at
        assert not state.cancel_requested

    async def test_a_heartbeat_reports_a_requested_cancellation(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)
        await harness.queue.request_cancellation(job_id, now=harness.clock.now())

        assert (await harness.services.heartbeat.execute(claimed.lease)).cancel_requested

    async def test_a_heartbeat_from_a_reclaimed_worker_is_refused(
        self, harness: WorkerHarness
    ) -> None:
        await harness.enqueue()
        claimed = await claim_one(harness)
        await harness.queue.reclaim_expired(
            now=harness.clock.now() + timedelta(seconds=LEASE_SECONDS + 1)
        )

        with pytest.raises(LeaseLostError):
            await harness.services.heartbeat.execute(claimed.lease)


class TestCheckpointing:
    async def test_a_completed_stage_is_recorded(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        checkpoint = await harness.services.checkpoint.execute(claimed, JobStage.PROBE)

        assert checkpoint.completed_stages == (JobStage.PROBE,)
        assert harness.queue.checkpoint_of(job_id).completed_stages == (JobStage.PROBE,)

    async def test_checkpoints_accumulate_in_order(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        claimed = await claim_one(harness)

        first = await harness.services.checkpoint.execute(claimed, JobStage.PROBE)
        second = await harness.services.checkpoint.execute(
            claimed.with_checkpoint(first), JobStage.DOWNLOAD, resume_token="offset=8"
        )

        assert second.completed_stages == (JobStage.PROBE, JobStage.DOWNLOAD)
        assert second.resume_token == "offset=8"

    async def test_checkpointing_without_the_lease_is_refused(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        claimed = await claim_one(harness)
        await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        with pytest.raises(LeaseLostError):
            await harness.services.checkpoint.execute(claimed, JobStage.PROBE)


class TestProgress:
    async def test_progress_reaches_the_queue_and_the_job(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        await harness.services.report_progress.execute(
            claimed.lease,
            StageProgress(
                stage=JobStage.DOWNLOAD,
                transferred_bytes=512,
                total_bytes=2048,
                speed_bps=256.0,
                eta_seconds=6.0,
            ),
        )

        observed = harness.queue.progress_of(job_id)
        job = await harness.job(job_id)
        assert observed is not None
        assert observed.stage is JobStage.DOWNLOAD
        assert observed.percentage == 25.0
        assert observed.speed_bps == 256.0
        assert observed.eta_seconds == 6.0
        assert job.progress.downloaded_bytes == 512
        assert job.progress.percentage == 25.0

    async def test_an_under_declared_total_does_not_break_the_job(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        await harness.services.report_progress.execute(
            claimed.lease,
            StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=300, total_bytes=100),
        )

        job = await harness.job(job_id)
        assert job.progress.downloaded_bytes == 300
        assert job.progress.total_bytes == 300

    async def test_progress_for_a_job_that_settled_underneath_is_dropped(
        self, harness: WorkerHarness
    ) -> None:
        # The window a completion opens: the aggregate is committed first and
        # the lease released after, so an observation can arrive in between.
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)
        job = await harness.job(job_id)
        job.complete(now=harness.clock.now())
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.save(job)
            await uow.commit()

        await harness.services.report_progress.execute(
            claimed.lease,
            StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=99),
        )

        assert (await harness.job(job_id)).progress.downloaded_bytes == 0
        assert harness.queue.progress_of(job_id) is not None

    async def test_progress_without_the_lease_is_refused(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        claimed = await claim_one(harness)
        await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        with pytest.raises(LeaseLostError):
            await harness.services.report_progress.execute(
                claimed.lease, StageProgress.starting(JobStage.DOWNLOAD)
            )


class TestCompletion:
    async def test_completing_settles_the_job_and_frees_the_lease(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        settlement = await harness.services.complete.execute(claimed)

        assert settlement.status is JobStatus.SUCCEEDED
        assert not settlement.retrying
        assert (await harness.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.leases_in_use() == 0
        assert "DownloadCompleted" in harness.events.names()

    async def test_completing_is_durable_even_if_the_lease_went(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)
        await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        settlement = await harness.services.complete.execute(claimed)

        assert settlement.status is JobStatus.SUCCEEDED
        assert (await harness.job(job_id)).status is JobStatus.SUCCEEDED

    async def test_completing_a_job_that_vanished_is_reported(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.delete(job_id)
            await uow.commit()

        with pytest.raises(DownloadJobNotFoundError):
            await harness.services.complete.execute(claimed)


class TestFailure:
    async def test_a_transient_failure_returns_the_job_with_backoff(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue(backoff_seconds=30)
        claimed = await claim_one(harness)

        settlement = await harness.services.fail.execute(claimed, transient())

        job = await harness.job(job_id)
        assert settlement.retrying
        assert settlement.available_at == harness.clock.now() + timedelta(seconds=30)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 1
        assert job.last_error is not None
        assert "provider_error" in job.last_error

    async def test_backoff_grows_with_each_attempt(self, harness: WorkerHarness) -> None:
        await harness.enqueue(backoff_seconds=30, max_attempts=5)

        delays: list[float] = []
        for _ in range(3):
            claimed = await claim_one(harness)
            settlement = await harness.services.fail.execute(claimed, transient())
            assert settlement.available_at is not None
            delays.append((settlement.available_at - harness.clock.now()).total_seconds())
            harness.clock.advance(int(delays[-1]))

        assert delays == [30, 60, 120]

    async def test_a_provider_delay_outranks_the_calculated_backoff(
        self, harness: WorkerHarness
    ) -> None:
        await harness.enqueue(backoff_seconds=30)
        claimed = await claim_one(harness)

        report = FailureReport(
            kind=FailureKind.TRANSIENT,
            code="rate_limited",
            message="slow down",
            retry_after_seconds=90.0,
        )
        settlement = await harness.services.fail.execute(claimed, report)

        assert settlement.available_at == harness.clock.now() + timedelta(seconds=90)

    async def test_a_permanent_failure_is_never_retried(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        settlement = await harness.services.fail.execute(claimed, permanent())

        job = await harness.job(job_id)
        assert not settlement.retrying
        assert settlement.available_at is None
        assert job.status is JobStatus.FAILED
        assert harness.leases_in_use() == 0
        assert await harness.services.claim.execute(worker=WORKER) is None

    async def test_the_failure_is_left_on_the_queue_for_triage(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        await harness.services.fail.execute(claimed, permanent("format_unavailable"))

        recorded = harness.queue.failure_of(job_id)
        assert recorded is not None
        assert recorded.code == "format_unavailable"
        assert not recorded.is_retryable

    async def test_a_spent_budget_stops_retrying(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue(max_attempts=2, backoff_seconds=0)

        for _ in range(2):
            claimed = await claim_one(harness)
            settlement = await harness.services.fail.execute(claimed, transient())

        job = await harness.job(job_id)
        assert not settlement.retrying
        assert job.status is JobStatus.FAILED
        assert job.attempts == 2
        assert await harness.services.claim.execute(worker=WORKER) is None

    async def test_a_failure_reported_after_a_cancellation_does_not_overwrite_it(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)
        job = await harness.job(job_id)
        job.cancel(now=harness.clock.now())
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.save(job)
            await uow.commit()

        settlement = await harness.services.fail.execute(claimed, transient())

        assert settlement.status is JobStatus.CANCELLED
        assert (await harness.job(job_id)).status is JobStatus.CANCELLED

    async def test_failing_a_job_that_vanished_is_reported(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.delete(job_id)
            await uow.commit()

        with pytest.raises(DownloadJobNotFoundError):
            await harness.services.fail.execute(claimed, transient())


class TestRelease:
    async def test_releasing_refunds_the_attempt_and_requeues_immediately(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        settlement = await harness.services.release.execute(claimed)

        job = await harness.job(job_id)
        assert settlement.retrying
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 0
        assert job.last_error is None
        assert await harness.services.claim.execute(worker=OTHER_WORKER) is not None

    async def test_a_nightly_restart_does_not_exhaust_the_budget(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue(max_attempts=2)

        for _ in range(5):
            claimed = await claim_one(harness)
            await harness.services.release.execute(claimed)

        job = await harness.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 0

    async def test_releasing_a_job_that_is_not_running_is_refused(
        self, harness: WorkerHarness
    ) -> None:
        await harness.enqueue()
        claimed = await claim_one(harness)
        await harness.services.release.execute(claimed)

        with pytest.raises(InvalidJobTransitionError):
            await harness.services.release.execute(claimed)


class TestCancellationAcknowledgement:
    async def test_acknowledging_makes_the_job_terminal(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)

        settlement = await harness.services.acknowledge_cancellation.execute(claimed)

        job = await harness.job(job_id)
        assert settlement.status is JobStatus.CANCELLED
        assert job.status is JobStatus.CANCELLED
        assert harness.leases_in_use() == 0
        assert "DownloadCancelled" in harness.events.names()
        assert await harness.services.claim.execute(worker=WORKER) is None

    async def test_acknowledging_a_job_already_cancelled_is_harmless(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        claimed = await claim_one(harness)
        job = await harness.job(job_id)
        job.cancel(now=harness.clock.now())
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.save(job)
            await uow.commit()

        settlement = await harness.services.acknowledge_cancellation.execute(claimed)

        assert settlement.status is JobStatus.CANCELLED


class TestLeaseRecovery:
    async def test_an_expired_lease_returns_the_job_to_the_queue(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        await claim_one(harness)
        harness.clock.advance(int(LEASE_SECONDS) + 1)

        recovery = await harness.services.recover_leases.execute()

        job = await harness.job(job_id)
        assert recovery.reclaimed == (job_id.value,)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 1, "a crash still costs the attempt it consumed"
        assert await harness.services.claim.execute(worker=OTHER_WORKER) is not None

    async def test_a_live_lease_is_left_alone(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        await claim_one(harness)
        harness.clock.advance(10)

        recovery = await harness.services.recover_leases.execute()

        assert recovery.total == 0
        assert (await harness.job(job_id)).status is JobStatus.RUNNING

    async def test_a_restarted_worker_takes_back_its_own_live_leases(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        await claim_one(harness)

        recovery = await harness.services.recover_leases.execute(worker=WORKER)

        assert recovery.reclaimed == (job_id.value,)
        assert (await harness.job(job_id)).status is JobStatus.QUEUED

    async def test_another_workers_leases_are_not_taken(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        await claim_one(harness)

        recovery = await harness.services.recover_leases.execute(worker=OTHER_WORKER)

        assert recovery.total == 0

    async def test_a_job_with_no_budget_left_is_abandoned_not_requeued(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue(max_attempts=1)
        await claim_one(harness)
        harness.clock.advance(int(LEASE_SECONDS) + 1)

        recovery = await harness.services.recover_leases.execute()

        job = await harness.job(job_id)
        assert recovery.abandoned == (job_id.value,)
        assert recovery.reclaimed == ()
        assert job.status is JobStatus.FAILED
        assert await harness.services.claim.execute(worker=WORKER) is None

    async def test_recovery_ignores_jobs_that_already_settled(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        claimed = await claim_one(harness)
        # Settle the job but leave the lease behind, as a crash between the
        # commit and the lease release would.
        job = await harness.job(claimed.job_id)
        job.complete(now=harness.clock.now())
        async with harness.unit_of_work() as uow:
            await uow.download_jobs.save(job)
            await uow.commit()
        harness.clock.advance(int(LEASE_SECONDS) + 1)

        recovery = await harness.services.recover_leases.execute()

        assert recovery.total == 0
