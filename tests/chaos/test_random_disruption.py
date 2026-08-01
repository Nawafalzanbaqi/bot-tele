"""Hit it at random, for a long time, and see what is left.

The failure suite asks "what happens when X goes wrong?". This asks the question
an operator actually has: **after a month of things going wrong in no particular
order, is the system still correct?**

Randomness here is seeded and reported, so a failure is reproducible from the
line in the output rather than being a story about a build that went red once.
Each scenario runs many jobs through many disruptions and then asserts the
invariants that have to hold no matter what order the damage arrived in:

* every job reaches a terminal state - nothing is left running for ever;
* no job is delivered twice;
* no stage that completed is ever repeated;
* the workspace is empty at the end;
* the process is still claiming work.

Those five are the product. Everything else is detail.
"""

from __future__ import annotations

import contextlib
import random
from typing import TYPE_CHECKING

import pytest

from mediahub.application.delivery.errors import ProviderUnavailableError
from mediahub.application.download.errors import ProviderError
from mediahub.application.download.queue import JobStage
from mediahub.domain.download.enums import JobStatus
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN
from tests.support.pipeline_fakes import PipelineHarness
from tests.support.worker_fakes import OTHER_WORKER

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

SEEDS = (1, 7, 13, 29, 101)
"""Fixed, so a red build is a bug rather than a coincidence. Several, so one
lucky ordering cannot pass for a proof."""

TERMINAL = (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class SuddenDeath(BaseException):
    """The process disappears mid-stage, past every safety net."""


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    pipeline = PipelineHarness.build(tmp_path)
    pipeline.worker.timings = FAST
    return pipeline


async def drain_queue(harness: PipelineHarness, jobs: list[JobId], *, rounds: int = 60) -> None:
    """Run until every job is terminal, or the round budget is spent."""
    for _ in range(rounds):
        if await all_terminal(harness, jobs):
            return
        # Exactly what a power cut does: nothing is settled and the lease is
        # left held. Recovery is the lease lapsing, like every other time.
        with contextlib.suppress(SuddenDeath):
            await harness.loop().run_once()
        harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
        await harness.worker.services.recover_leases.execute()


async def all_terminal(harness: PipelineHarness, jobs: list[JobId]) -> bool:
    """Return whether every job has stopped moving."""
    for job_id in jobs:
        if (await harness.worker.job(job_id)).status not in TERMINAL:
            return False
    return True


@pytest.mark.parametrize("seed", SEEDS)
class TestRandomWorkerTermination:
    async def test_every_job_reaches_a_terminal_state(
        self, harness: PipelineHarness, seed: int
    ) -> None:
        rng = random.Random(seed)  # noqa: S311 - reproducibility, not cryptography
        jobs = [await harness.worker.enqueue(max_attempts=6) for _ in range(6)]

        for _ in range(30):
            victim = rng.choice(DEFAULT_STAGE_PLAN)
            harness.handler(victim).error = SuddenDeath() if rng.random() < 0.3 else None
            with contextlib.suppress(SuddenDeath):
                await harness.loop().run_once()
            for stage in DEFAULT_STAGE_PLAN:
                harness.handler(stage).error = None
            harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
            await harness.worker.services.recover_leases.execute()

        await drain_queue(harness, jobs)

        statuses = [(await harness.worker.job(job)).status for job in jobs]
        assert all(status in TERMINAL for status in statuses), f"seed={seed}: {statuses}"

    async def test_no_job_is_ever_delivered_twice(
        self, harness: PipelineHarness, seed: int
    ) -> None:
        rng = random.Random(seed)  # noqa: S311
        jobs = [await harness.worker.enqueue(max_attempts=8) for _ in range(4)]

        for _ in range(40):
            if rng.random() < 0.35:
                harness.handler(rng.choice(DEFAULT_STAGE_PLAN)).error = SuddenDeath()
            with contextlib.suppress(SuddenDeath):
                await harness.loop().run_once()
            for stage in DEFAULT_STAGE_PLAN:
                harness.handler(stage).error = None
            harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
            await harness.worker.services.recover_leases.execute()

        await drain_queue(harness, jobs)

        succeeded = 0
        for job in jobs:
            if (await harness.worker.job(job)).status is JobStatus.SUCCEEDED:
                succeeded += 1
        assert (
            harness.destination.calls == succeeded
        ), f"seed={seed}: {harness.destination.calls} uploads for {succeeded} successes"

    async def test_the_workspace_is_empty_when_the_dust_settles(
        self, harness: PipelineHarness, seed: int
    ) -> None:
        rng = random.Random(seed)  # noqa: S311
        jobs = [await harness.worker.enqueue(max_attempts=6) for _ in range(5)]

        for _ in range(30):
            if rng.random() < 0.4:
                harness.handler(rng.choice(DEFAULT_STAGE_PLAN)).error = SuddenDeath()
            with contextlib.suppress(SuddenDeath):
                await harness.loop().run_once()
            for stage in DEFAULT_STAGE_PLAN:
                harness.handler(stage).error = None
            harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
            await harness.worker.services.recover_leases.execute()

        await drain_queue(harness, jobs)

        assert harness.worker.workspace_directories() == [], f"seed={seed}: leaked disk"
        assert harness.worker.leases_in_use() == 0


@pytest.mark.parametrize("seed", SEEDS)
class TestRandomExceptions:
    async def test_arbitrary_errors_never_stop_the_loop(
        self, harness: PipelineHarness, seed: int
    ) -> None:
        # The single most important property of the worker: one bad job must
        # never become a total outage.
        rng = random.Random(seed)  # noqa: S311
        catalogue: tuple[Exception, ...] = (
            ProviderError("connection reset"),
            ProviderUnavailableError("destination is down"),
            RuntimeError("something nobody anticipated"),
            ValueError("a bad value from a provider"),
            TimeoutError("the store is locked"),
            KeyError("a field that was not there"),
        )
        jobs = [await harness.worker.enqueue(max_attempts=10) for _ in range(4)]
        loop = harness.loop()

        for _ in range(50):
            if rng.random() < 0.5:
                stage = rng.choice(DEFAULT_STAGE_PLAN)
                harness.handler(stage).error = rng.choice(catalogue)
                harness.handler(stage).error_times = 1
            await loop.run_once()
            for stage in DEFAULT_STAGE_PLAN:
                harness.handler(stage).error = None
                harness.handler(stage).error_times = None
            harness.worker.clock.advance(60)

        await drain_queue(harness, jobs)

        # The loop is still working: a fresh job goes straight through.
        healthy = await harness.worker.enqueue()
        assert await loop.run_once() is True
        assert (await harness.worker.job(healthy)).status is JobStatus.SUCCEEDED

    async def test_a_settled_job_is_never_reopened(
        self, harness: PipelineHarness, seed: int
    ) -> None:
        rng = random.Random(seed)  # noqa: S311
        jobs = [await harness.worker.enqueue(max_attempts=4) for _ in range(4)]

        for _ in range(40):
            if rng.random() < 0.4:
                stage = rng.choice(DEFAULT_STAGE_PLAN)
                harness.handler(stage).error = RuntimeError("chaos")
                harness.handler(stage).error_times = 1
            await harness.loop().run_once()
            for stage in DEFAULT_STAGE_PLAN:
                harness.handler(stage).error = None
                harness.handler(stage).error_times = None
            harness.worker.clock.advance(60)

        await drain_queue(harness, jobs)
        settled = {job.value: (await harness.worker.job(job)).status for job in jobs}

        for _ in range(5):
            await harness.loop().run_once()
            harness.worker.clock.advance(60)

        after = {job.value: (await harness.worker.job(job)).status for job in jobs}
        assert after == settled, f"seed={seed}: a terminal job moved again"


class TestPartialDownloads:
    async def test_a_partial_file_is_never_delivered(self, harness: PipelineHarness) -> None:
        # The atomic-rename guarantee, seen from the pipeline: an interrupted
        # transfer leaves nothing under a name anything would send.
        await harness.worker.enqueue()
        harness.downloader.fetch_error = ProviderError("connection reset")
        harness.downloader.leave_partial = True

        await harness.loop().run_once()

        assert harness.destination.calls == 0
        assert harness.worker.workspace_directories() == []

    async def test_repeated_partial_transfers_do_not_accumulate_bytes(
        self, harness: PipelineHarness
    ) -> None:
        # Every failed attempt gets its own lease, and every lease is deleted.
        # Without that, a source that fails at 90% five times leaves five
        # near-complete files on a 32 GB card.
        await harness.worker.enqueue(max_attempts=5)
        harness.downloader.fetch_error = ProviderError("connection reset")
        harness.downloader.leave_partial = True

        for _ in range(4):
            await harness.loop().run_once()
            harness.worker.clock.advance(60)

        assert harness.worker.workspace_directories() == []

    async def test_a_partial_transfer_that_finally_succeeds_delivers_once(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue(max_attempts=5)
        harness.downloader.fetch_error = ProviderError("connection reset")
        harness.downloader.fetch_error_times = 2
        harness.downloader.leave_partial = True

        for _ in range(3):
            await harness.loop().run_once()
            harness.worker.clock.advance(60)

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 1


class TestInterruptedDelivery:
    async def test_an_upload_cut_off_half_way_is_retried_whole(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue(max_attempts=4)
        harness.destination.fail_with = ProviderUnavailableError("the connection dropped")
        harness.destination.fail_times = 2

        for _ in range(3):
            await harness.loop().run_once()
            harness.worker.clock.advance(60)

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert len(harness.destination.delivered) == 1, "one *successful* delivery"

    async def test_a_delivery_that_succeeded_is_never_repeated(
        self, harness: PipelineHarness
    ) -> None:
        # The receipt is written in the same checkpoint as the stage, so a crash
        # between "it arrived" and "we recorded it" cannot exist.
        job_id = await harness.worker.enqueue(max_attempts=4)
        harness.handler(JobStage.CLEANUP).error = SuddenDeath()

        with pytest.raises(SuddenDeath):
            await harness.loop().run_once()
        harness.handler(JobStage.CLEANUP).error = None
        harness.worker.clock.advance(int(LEASE_SECONDS) + 1)
        await harness.worker.services.recover_leases.execute()
        await harness.loop(worker=OTHER_WORKER).run_once()

        assert (await harness.worker.job(job_id)).status is JobStatus.SUCCEEDED
        assert harness.destination.calls == 1

    async def test_the_local_copy_is_never_released_without_a_receipt(
        self, harness: PipelineHarness
    ) -> None:
        # Refusing costs a lease that a sweep reclaims. Deleting would risk
        # losing both copies, which is the one unrecoverable outcome.
        job_id = await harness.worker.enqueue()
        harness.destination.fail_with = ProviderUnavailableError("down")

        await harness.loop().run_once()

        assert harness.state_of(job_id).receipt is None
        assert JobStage.CLEANUP not in harness.completed_stages(job_id)


@pytest.mark.parametrize("seed", SEEDS)
class TestRepeatedRestarts:
    async def test_a_flapping_worker_still_finishes_its_queue(
        self, harness: PipelineHarness, seed: int
    ) -> None:
        rng = random.Random(seed)  # noqa: S311
        jobs = [await harness.worker.enqueue(max_attempts=10) for _ in range(4)]

        for _ in range(40):
            runtime = harness.runtime()
            await runtime.start()
            if rng.random() < 0.5:
                await harness.loop().run_once()
            harness.worker.clock.advance(rng.choice((1, int(LEASE_SECONDS) + 1)))
            await harness.worker.services.recover_leases.execute()

        await drain_queue(harness, jobs)

        statuses = [(await harness.worker.job(job)).status for job in jobs]
        assert all(status in TERMINAL for status in statuses), f"seed={seed}: {statuses}"
        assert harness.worker.workspace_directories() == []
