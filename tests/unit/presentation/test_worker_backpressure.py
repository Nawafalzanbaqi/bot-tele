"""Refusing to claim, and starting again once there is somewhere to write.

The claim loop's newest rule is a refusal, and a refusal is the kind of thing
that silently becomes permanent. These tests pin down all four of its edges:

* it does not engage until a full device has actually been *seen*, so a wrong
  reading of free space cannot stop a healthy worker on its own;
* while engaged, queued jobs stay queued rather than being spent one attempt at
  a time on the same fact;
* it clears the moment there is headroom again;
* and if the probe itself fails, the claim goes through - a worker that has
  quietly stopped taking work is a worse outcome than a job that fails with a
  real reason attached.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mediahub.domain.download.enums import JobStatus
from mediahub.domain.workspace.errors import (
    InsufficientDiskSpaceError,
    WorkspaceQuotaExceededError,
)
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.loop import ClaimLoop
from tests.support.worker_fakes import WORKER, WorkerHarness

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit

FAST = WorkerTimings(
    lease_seconds=20.0,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.01,
)


class ScriptedWorkspace:
    """A real workspace on a device whose free space the test controls.

    Leases are refused exactly while the device says it has nothing left, and
    granted for real otherwise - so a loop that recovers actually runs a job
    rather than merely reporting that it would have.
    """

    def __init__(
        self,
        inner: FilesystemWorkspace,
        *,
        free: int = 0,
        refuse: Exception | None = None,
    ) -> None:
        self._inner = inner
        self.free = free
        self.refuse = refuse
        self.probe_error: Exception | None = None
        self.lease_calls = 0
        self.probe_calls = 0

    def lease(
        self, *, label: str, reserve_bytes: int | None = None
    ) -> AbstractContextManager[WorkspaceScope]:
        self.lease_calls += 1
        if self.refuse is not None:
            raise self.refuse
        if self.free <= 0:
            raise InsufficientDiskSpaceError(reserve_bytes or 0, 0)
        return self._inner.lease(label=label, reserve_bytes=reserve_bytes)

    def free_bytes(self) -> int:
        self.probe_calls += 1
        if self.probe_error is not None:
            raise self.probe_error
        return self.free


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness.build(tmp_path, timings=FAST)


def scripted(harness: WorkerHarness, **kwargs: object) -> ScriptedWorkspace:
    """Return a workspace over the harness's real root, scripted by ``kwargs``."""
    return ScriptedWorkspace(harness.workspace, **kwargs)  # type: ignore[arg-type]


def loop_over(harness: WorkerHarness, workspace: ScriptedWorkspace) -> ClaimLoop:
    """Return a claim loop writing into ``workspace``."""
    return ClaimLoop(
        services=replace(harness.services, workspace=workspace),
        executor=harness.executor(),
        worker=WORKER,
        timings=harness.timings,
    )


class TestArming:
    async def test_a_healthy_loop_never_probes_the_disk(self, harness: WorkerHarness) -> None:
        # The probe is a syscall per cycle. A worker polling every second for a
        # year should not pay for it while nothing is wrong.
        workspace = scripted(harness, free=1_000_000)
        loop = loop_over(harness, workspace)

        for _ in range(5):
            await loop.run_once()

        assert workspace.probe_calls == 0
        assert not loop.starved

    async def test_the_first_full_disk_is_still_claimed_and_reported(
        self, harness: WorkerHarness
    ) -> None:
        job_id = await harness.enqueue()
        loop = loop_over(harness, scripted(harness, free=0))

        assert await loop.run_once() is True

        job = await harness.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert job.last_error is not None
        assert "insufficient_disk_space" in job.last_error
        assert loop.starved

    async def test_a_quota_refusal_does_not_arm_the_refusal(self, harness: WorkerHarness) -> None:
        # A lease that exceeded a configured ceiling says nothing about the
        # device. Waiting for space that was never the problem would stall the
        # queue indefinitely.
        await harness.enqueue()
        loop = loop_over(harness, scripted(harness, refuse=WorkspaceQuotaExceededError(1024, 4096)))

        await loop.run_once()

        assert not loop.starved


class TestWhileStarved:
    async def test_queued_jobs_are_left_queued_rather_than_spent(
        self, harness: WorkerHarness
    ) -> None:
        jobs = [await harness.enqueue(max_attempts=1) for _ in range(4)]
        loop = loop_over(harness, scripted(harness, free=0))

        await loop.run_once()
        later = [await loop.run_once() for _ in range(3)]

        assert later == [False, False, False]
        statuses = [(await harness.job(job)).status for job in jobs[1:]]
        assert all(status is JobStatus.QUEUED for status in statuses)

    async def test_no_further_lease_is_even_attempted(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        await harness.enqueue()
        workspace = scripted(harness, free=0)
        loop = loop_over(harness, workspace)

        await loop.run_once()
        leases_after_first = workspace.lease_calls
        await loop.run_once()

        assert workspace.lease_calls == leases_after_first


class TestRecovery:
    async def test_headroom_returning_clears_the_refusal(self, harness: WorkerHarness) -> None:
        await harness.enqueue()
        await harness.enqueue()
        workspace = scripted(harness, free=0)
        loop = loop_over(harness, workspace)

        await loop.run_once()
        assert loop.starved
        assert await loop.run_once() is False

        workspace.free = 4 * 1024**3
        await loop.run_once()

        assert not loop.starved

    async def test_an_unreadable_probe_lets_the_claim_through(self, harness: WorkerHarness) -> None:
        # An unmounted volume, most likely. The attempt then fails with the real
        # reason, which beats a worker that has silently stopped taking work and
        # explains itself nowhere.
        await harness.enqueue()
        await harness.enqueue()
        workspace = scripted(harness, free=0)
        loop = loop_over(harness, workspace)

        await loop.run_once()
        workspace.probe_error = OSError("the volume is not mounted")

        assert await loop.run_once() is True


class TestCancelledBeforeStarting:
    async def test_a_draining_loop_opens_no_lease_at_all(self, harness: WorkerHarness) -> None:
        # Creating a directory and a manifest only to delete them a moment later
        # is three SD-card writes and three more ways for shutdown to fail, to
        # run zero stages.
        await harness.enqueue()
        workspace = scripted(harness, free=4 * 1024**3)
        loop = loop_over(harness, workspace)
        loop.drain()

        await loop.run_once()

        assert workspace.lease_calls == 0

    async def test_the_job_is_handed_back_unharmed(self, harness: WorkerHarness) -> None:
        job_id = await harness.enqueue()
        loop = loop_over(harness, scripted(harness, free=4 * 1024**3))
        loop.drain()

        await loop.run_once()

        job = await harness.job(job_id)
        assert job.status is JobStatus.QUEUED
        assert job.attempts == 0, "a shutdown is not the job's fault"

    async def test_a_job_asked_to_stop_is_never_claimed_in_the_first_place(
        self, harness: WorkerHarness
    ) -> None:
        # The cheapest possible cancellation: the queue simply stops offering
        # the job, so no lease, no attempt and no stage ever happen. Worth
        # pinning down because the *expensive* path - claim, notice, unwind,
        # acknowledge - is the one the pipeline is written around, and it would
        # be easy to assume it is the only one.
        job_id = await harness.enqueue()
        await harness.queue.request_cancellation(job_id, now=harness.clock.now())
        workspace = scripted(harness, free=4 * 1024**3)
        loop = loop_over(harness, workspace)

        assert await loop.run_once() is False

        assert workspace.lease_calls == 0
        assert (await harness.job(job_id)).attempts == 0, "not claiming costs nothing"
        assert all(handler.calls == 0 for handler in harness.handlers)
