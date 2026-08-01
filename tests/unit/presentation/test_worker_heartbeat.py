"""The heartbeat holds the lease, hears the stop request, and never dies.

The failure this file exists to prevent: a heartbeat that raises stops renewing
while the job keeps running, the lease lapses, another worker claims the job,
and the same download happens twice. Everything here is about that chain not
starting.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mediahub.application.common.cancellation import CancellationReason, CancellationSource
from mediahub.application.download.queue import JobStage, StageProgress
from mediahub.application.download.use_cases.heartbeat_job import HeartbeatJob
from mediahub.application.download.use_cases.report_job_progress import ReportJobProgress
from mediahub.infrastructure.persistence.memory.queue import InMemoryJobQueue
from mediahub.presentation.worker.heartbeat import Heartbeat
from mediahub.presentation.worker.progress import ProgressRegistry
from tests.support.worker_fakes import WORKER, WorkerHarness

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from mediahub.application.download.queue import ClaimedJob, Lease, LeaseState

pytestmark = pytest.mark.unit

LEASE_SECONDS = 120.0
HEARTBEAT_SECONDS = 30.0


class UnreachableQueue(InMemoryJobQueue):
    """A queue that cannot be reached, the way a locked database cannot."""

    async def extend_lease(
        self, lease: Lease, *, lease_seconds: float, now: datetime
    ) -> LeaseState:
        message = "database is locked"
        raise TimeoutError(message)

    async def record_progress(self, lease: Lease, progress: StageProgress) -> None:
        message = "database is locked"
        raise TimeoutError(message)


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness.build(tmp_path)


async def claim_one(harness: WorkerHarness) -> ClaimedJob:
    await harness.enqueue()
    claimed = await harness.services.claim.execute(worker=WORKER)
    assert claimed is not None
    return claimed


def build(
    harness: WorkerHarness,
    claimed: ClaimedJob,
    *,
    cancellation: CancellationSource,
    progress: ProgressRegistry | None = None,
    queue: InMemoryJobQueue | None = None,
) -> Heartbeat:
    """Assemble a heartbeat over the harness, optionally with a broken queue."""
    return Heartbeat(
        claimed,
        extend=HeartbeatJob(
            queue=queue or harness.queue, clock=harness.clock, lease_seconds=LEASE_SECONDS
        ),
        report_progress=ReportJobProgress(
            queue=queue or harness.queue,
            unit_of_work=harness.unit_of_work,
            clock=harness.clock,
        ),
        progress=progress or ProgressRegistry(clock=harness.clock, min_interval_seconds=0.0),
        cancellation=cancellation,
        clock=harness.clock,
        interval_seconds=HEARTBEAT_SECONDS,
        flush_seconds=1.0,
    )


class TestLeaseRenewal:
    async def test_the_lease_is_renewed_once_the_interval_has_passed(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        heartbeat = build(harness, claimed, cancellation=CancellationSource())

        harness.clock.advance(int(HEARTBEAT_SECONDS))
        await heartbeat.tick()

        assert heartbeat.lease.expires_at > claimed.lease.expires_at
        assert not heartbeat.lease_lost

    async def test_the_lease_is_left_alone_before_the_interval(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        heartbeat = build(harness, claimed, cancellation=CancellationSource())

        harness.clock.advance(5)
        await heartbeat.tick()

        assert heartbeat.lease == claimed.lease

    async def test_losing_the_lease_stops_the_job(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        heartbeat = build(harness, claimed, cancellation=cancellation)
        await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        harness.clock.advance(int(HEARTBEAT_SECONDS))
        await heartbeat.tick()

        assert heartbeat.lease_lost
        assert cancellation.cancelled
        assert cancellation.reason is CancellationReason.TIMEOUT

    async def test_an_unreachable_database_is_weather_not_an_incident(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        heartbeat = build(
            harness,
            claimed,
            cancellation=cancellation,
            queue=UnreachableQueue(harness.unit_of_work.database),
        )

        harness.clock.advance(int(HEARTBEAT_SECONDS))
        await heartbeat.tick()

        assert not heartbeat.lease_lost
        assert not cancellation.cancelled


class TestCancellationPolling:
    async def test_a_requested_cancellation_reaches_the_token(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        heartbeat = build(harness, claimed, cancellation=cancellation)
        await harness.queue.request_cancellation(claimed.job_id, now=harness.clock.now())

        harness.clock.advance(int(HEARTBEAT_SECONDS))
        await heartbeat.tick()

        assert cancellation.cancelled
        assert cancellation.reason is CancellationReason.REQUESTED

    async def test_a_shutdown_already_underway_is_not_relabelled(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        cancellation.cancel(CancellationReason.SHUTDOWN)
        heartbeat = build(harness, claimed, cancellation=cancellation)
        await harness.queue.request_cancellation(claimed.job_id, now=harness.clock.now())

        harness.clock.advance(int(HEARTBEAT_SECONDS))
        await heartbeat.tick()

        assert cancellation.reason is CancellationReason.SHUTDOWN


class TestProgressFlushing:
    async def test_due_progress_is_written_on_a_tick(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        progress = ProgressRegistry(clock=harness.clock, min_interval_seconds=0.0)
        heartbeat = build(harness, claimed, cancellation=CancellationSource(), progress=progress)
        progress.observe(
            StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=256, total_bytes=1024)
        )

        await heartbeat.tick()

        observed = harness.queue.progress_of(claimed.job_id)
        assert observed is not None
        assert observed.transferred_bytes == 256
        assert (await harness.job(claimed.job_id)).progress.downloaded_bytes == 256

    async def test_a_throttled_observation_waits_for_its_turn(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        progress = ProgressRegistry(
            clock=harness.clock, min_interval_seconds=60.0, min_percent_step=50.0
        )
        heartbeat = build(harness, claimed, cancellation=CancellationSource(), progress=progress)
        progress.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=1))
        await heartbeat.tick()
        progress.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=2))

        await heartbeat.tick()

        observed = harness.queue.progress_of(claimed.job_id)
        assert observed is not None
        assert observed.transferred_bytes == 1

    async def test_the_final_flush_ignores_the_throttle(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        progress = ProgressRegistry(clock=harness.clock, min_interval_seconds=60.0)
        heartbeat = build(harness, claimed, cancellation=CancellationSource(), progress=progress)
        progress.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=1))
        await heartbeat.tick()
        progress.observe(StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=999))

        await heartbeat.flush_final()

        observed = harness.queue.progress_of(claimed.job_id)
        assert observed is not None
        assert observed.transferred_bytes == 999

    async def test_a_progress_write_that_fails_is_dropped_not_escalated(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        progress = ProgressRegistry(clock=harness.clock, min_interval_seconds=0.0)
        heartbeat = build(
            harness,
            claimed,
            cancellation=cancellation,
            progress=progress,
            queue=UnreachableQueue(harness.unit_of_work.database),
        )
        progress.observe(StageProgress.starting(JobStage.DOWNLOAD))

        await heartbeat.tick()

        assert not heartbeat.lease_lost, "telemetry must never cost a job"
        assert not cancellation.cancelled

    async def test_a_progress_write_that_finds_no_lease_stops_the_job(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        progress = ProgressRegistry(clock=harness.clock, min_interval_seconds=0.0)
        heartbeat = build(harness, claimed, cancellation=cancellation, progress=progress)
        await harness.queue.release_owned_by(WORKER, now=harness.clock.now())
        progress.observe(StageProgress.starting(JobStage.DOWNLOAD))

        await heartbeat.tick()

        assert heartbeat.lease_lost
        assert cancellation.reason is CancellationReason.TIMEOUT


class TestTheLoop:
    async def test_the_loop_ends_when_asked(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        heartbeat = build(harness, claimed, cancellation=CancellationSource())
        heartbeat._flush_seconds = 0.01

        task = asyncio.create_task(heartbeat.run())
        await asyncio.sleep(0.03)
        heartbeat.stop()
        await asyncio.wait_for(task, timeout=1)

        assert not heartbeat.lease_lost

    async def test_the_loop_ends_when_the_lease_goes(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        heartbeat = build(harness, claimed, cancellation=cancellation)
        heartbeat._flush_seconds = 0.01
        harness.clock.advance(int(HEARTBEAT_SECONDS))
        await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        await asyncio.wait_for(heartbeat.run(), timeout=1)

        assert heartbeat.lease_lost
        assert cancellation.cancelled
