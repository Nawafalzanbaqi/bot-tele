"""Every job queue behaves the same way, or the tests above it are lying.

One suite, run against every implementation of
:class:`~mediahub.application.download.queue.JobQueue` - the in-memory adapter
the unit tests are built on, and the SQLite adapter the device actually runs.
Both must pass exactly this, which is the specific mitigation for the risk
accepted in ADR-0005 (``docs/architecture/17-testing-strategy.md`` §17.4): tests
written against a dictionary prove nothing about a database unless something
holds the two to the same promises.

The five properties a queue must have, and what each one prevents:

===========================  ================================================
Property                     Without it
===========================  ================================================
One claim, one winner        Two workers download the same file
Ownership guards writes      A stalled worker overwrites its replacement
Backoff is honoured          A failing job retries in a hot loop
Expired leases come back     A crashed worker's jobs are lost forever
Cancellation is visible      A cancelled job runs to completion anyway
===========================  ================================================
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import LeaseLostError
from mediahub.application.download.queue import Checkpoint, JobStage, StageProgress
from mediahub.domain.download.enums import JobPriority
from tests.support.worker_fakes import (
    OTHER_WORKER,
    WORKER,
    QueueHarness,
    SqliteQueueHarness,
    WorkerHarness,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mediahub.application.download.queue import JobQueue

pytestmark = pytest.mark.integration

LEASE_SECONDS = 120.0


@pytest.fixture(params=["memory", "sqlite"])
async def harness(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[QueueHarness]:
    """Build a harness whose queue is the implementation under test."""
    if request.param == "memory":
        yield WorkerHarness.build(tmp_path)
        return
    if request.param != "sqlite":  # pragma: no cover - defensive
        message = f"unknown queue implementation: {request.param}"
        raise AssertionError(message)
    durable = await SqliteQueueHarness.build(tmp_path)
    try:
        yield durable
    finally:
        # Windows will not delete a file a pooled connection still holds, so
        # tmp_path cleanup fails loudly rather than leaking quietly.
        await durable.aclose()


@pytest.fixture
def queue(harness: QueueHarness) -> JobQueue:
    """Return the queue under test."""
    return harness.queue


class TestClaiming:
    async def test_a_queued_job_is_claimable(self, harness: QueueHarness, queue: JobQueue) -> None:
        job_id = await harness.enqueue()

        claimed = await queue.claim(
            worker=WORKER, lease_seconds=LEASE_SECONDS, now=harness.clock.now()
        )

        assert claimed is not None
        assert claimed.job_id == job_id
        assert claimed.lease.is_held_by(WORKER)
        assert claimed.checkpoint.is_fresh

    async def test_an_empty_queue_returns_nothing(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        assert (
            await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=harness.clock.now())
            is None
        )

    async def test_only_one_worker_wins_a_job(self, harness: QueueHarness, queue: JobQueue) -> None:
        await harness.enqueue()
        now = harness.clock.now()

        first = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        second = await queue.claim(worker=OTHER_WORKER, lease_seconds=LEASE_SECONDS, now=now)

        assert first is not None
        assert second is None

    async def test_priority_then_age_decides_who_runs_first(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue(priority=JobPriority.LOW)
        harness.clock.advance(1)
        high = await harness.enqueue(priority=JobPriority.HIGH)
        harness.clock.advance(1)
        await harness.enqueue(priority=JobPriority.NORMAL)

        claimed = await queue.claim(
            worker=WORKER, lease_seconds=LEASE_SECONDS, now=harness.clock.now()
        )

        assert claimed is not None
        assert claimed.job_id == high

    async def test_a_closed_job_is_never_claimed_again(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        claimed = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert claimed is not None

        await queue.close(claimed.lease, now=now)

        assert await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now) is None


class TestOwnership:
    async def test_writes_from_a_worker_that_lost_the_lease_are_refused(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        first = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert first is not None

        later = now + timedelta(seconds=LEASE_SECONDS + 1)
        await queue.reclaim_expired(now=later)
        second = await queue.claim(worker=OTHER_WORKER, lease_seconds=LEASE_SECONDS, now=later)
        assert second is not None

        with pytest.raises(LeaseLostError):
            await queue.extend_lease(first.lease, lease_seconds=LEASE_SECONDS, now=later)
        with pytest.raises(LeaseLostError):
            await queue.save_checkpoint(first.lease, Checkpoint.empty())
        with pytest.raises(LeaseLostError):
            await queue.record_progress(first.lease, StageProgress.starting(JobStage.DOWNLOAD))
        with pytest.raises(LeaseLostError):
            await queue.close(first.lease, now=later)

    async def test_an_unleased_job_cannot_be_written_to(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        claimed = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert claimed is not None
        await queue.release(claimed.lease, now=now)

        with pytest.raises(LeaseLostError):
            await queue.save_checkpoint(claimed.lease, Checkpoint.empty())


class TestLeaseLifetime:
    async def test_extending_keeps_the_job_and_reports_cancellation(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        job_id = await harness.enqueue()
        now = harness.clock.now()
        claimed = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert claimed is not None

        state = await queue.extend_lease(
            claimed.lease, lease_seconds=LEASE_SECONDS, now=now + timedelta(seconds=30)
        )
        assert state.lease.expires_at > claimed.lease.expires_at
        assert not state.cancel_requested

        await queue.request_cancellation(job_id, now=now)
        state = await queue.extend_lease(
            state.lease, lease_seconds=LEASE_SECONDS, now=now + timedelta(seconds=60)
        )
        assert state.cancel_requested

    async def test_an_expired_lease_is_reclaimed_and_the_job_returns(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        claimed = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert claimed is not None

        expired = now + timedelta(seconds=LEASE_SECONDS + 1)
        reclaimed = await queue.reclaim_expired(now=expired)

        assert [lease.job_id for lease in reclaimed] == [claimed.job_id]
        assert await queue.claim(worker=OTHER_WORKER, lease_seconds=LEASE_SECONDS, now=expired)

    async def test_a_live_lease_is_left_alone(self, harness: QueueHarness, queue: JobQueue) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)

        assert await queue.reclaim_expired(now=now + timedelta(seconds=60)) == ()

    async def test_a_worker_can_take_back_its_own_live_leases(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        claimed = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert claimed is not None

        released = await queue.release_owned_by(WORKER, now=now)

        assert [lease.job_id for lease in released] == [claimed.job_id]
        assert await queue.release_owned_by(OTHER_WORKER, now=now) == ()


class TestBackoffAndCheckpoints:
    async def test_a_job_released_with_a_delay_is_not_claimable_yet(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        claimed = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert claimed is not None

        await queue.release(claimed.lease, now=now, available_at=now + timedelta(seconds=30))

        assert await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now) is None
        assert await queue.claim(
            worker=WORKER, lease_seconds=LEASE_SECONDS, now=now + timedelta(seconds=30)
        )

    async def test_a_checkpoint_survives_the_worker_that_wrote_it(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        await harness.enqueue()
        now = harness.clock.now()
        first = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert first is not None
        await queue.save_checkpoint(
            first.lease,
            Checkpoint.empty().with_stage(JobStage.PROBE, at=now, resume_token="offset=1"),
        )

        expired = now + timedelta(seconds=LEASE_SECONDS + 1)
        await queue.reclaim_expired(now=expired)
        second = await queue.claim(worker=OTHER_WORKER, lease_seconds=LEASE_SECONDS, now=expired)

        assert second is not None
        assert second.checkpoint.completed_stages == (JobStage.PROBE,)
        assert second.checkpoint.resume_token == "offset=1"

    async def test_a_cancelled_job_is_not_handed_out(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        job_id = await harness.enqueue()
        now = harness.clock.now()

        assert await queue.request_cancellation(job_id, now=now)

        assert await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now) is None

    async def test_cancelling_a_finished_job_is_refused(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        job_id = await harness.enqueue()
        now = harness.clock.now()
        claimed = await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)
        assert claimed is not None
        await queue.close(claimed.lease, now=now)

        assert not await queue.request_cancellation(job_id, now=now)


class TestDiagnostics:
    async def test_the_queue_can_describe_a_leased_job(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        job_id = await harness.enqueue()
        now = harness.clock.now()
        await queue.claim(worker=WORKER, lease_seconds=LEASE_SECONDS, now=now)

        state = await queue.state_of(job_id)

        assert state is not None
        assert state.lease.is_held_by(WORKER)

    async def test_an_unclaimed_job_has_no_queue_state(
        self, harness: QueueHarness, queue: JobQueue
    ) -> None:
        job_id = await harness.enqueue()

        assert await queue.state_of(job_id) is None
