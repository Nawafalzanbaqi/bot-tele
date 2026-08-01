"""One claim loop: claim a job, run it, settle it, and go round again.

There is one of these per concurrency slot. Each is a small state machine with
exactly four outcomes for an attempt - completed, failed, cancelled, released -
and one absolute rule:

**The loop must never die from a job's exception.** An unhandled error here
turns one bad URL into a total outage: no job ever gets claimed again, and
nothing looks broken from the outside because the process is still running
(``docs/architecture/10-worker-architecture.md`` §10.10). Every cycle is
therefore wrapped, and the executor below it is already built to return failures
rather than raise them.

Three orderings in this module are load-bearing:

* **The workspace lease wraps the whole attempt.** Leaving it deletes every byte
  the attempt wrote, whether it succeeded, failed, was cancelled or crashed
  mid-stage. Nothing else has to remember to tidy up.
* **The workspace is released before the job is settled**, so a crash between
  the two leaves a job to reclaim rather than a file to leak.
* **A lost lease means stop, silently.** Not fail, not retry, not report: the
  job belongs to another worker now, and writing to it is how one job becomes
  two.

And one refusal, which is backpressure rather than an ordering: **once a full
device has been seen, the loop stops claiming until headroom returns.**

The first job to meet a full disk is still claimed, still fails as a transient
``insufficient_disk_space``, and is still requeued on the long disk-specific
backoff - that path is what tells an operator what is wrong, and it stays. What
does not stay is doing it again for every other job in the queue. Claiming costs
an attempt, so a device that fills with fifty jobs waiting spends fifty attempts
discovering the same fact, and a queue with a retry budget of three is
permanently failed within minutes of an event that a single ``rm`` would have
fixed.

The refusal is armed by an *observed* failure and cleared by the workspace's own
headroom figure - free space minus outstanding reservations minus the emergency
floor. Arming on observation rather than on the probe alone matters: a probe can
be wrong about a network mount or a quota, and a worker that stops claiming
because of a bad reading is an outage nobody ordered.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.common.cancellation import CancellationReason, CancellationSource
from mediahub.application.download.errors import LeaseLostError
from mediahub.application.download.failures import FailureReport, classify
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.workspace.errors import InsufficientDiskSpaceError
from mediahub.presentation.worker.executor import (
    CANCELLED_CODE,
    CANCELLED_MESSAGE,
    ExecutionResult,
)
from mediahub.presentation.worker.heartbeat import Heartbeat
from mediahub.presentation.worker.progress import ProgressRegistry
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.queue import ClaimedJob, WorkerId
    from mediahub.presentation.worker.config import WorkerTimings
    from mediahub.presentation.worker.executor import StageExecutor
    from mediahub.presentation.worker.services import WorkerServices


class ClaimLoop:
    """Claims and executes one job at a time, for one slot."""

    __slots__ = (
        "_active",
        "_draining",
        "_executor",
        "_services",
        "_slot",
        "_starved",
        "_stopping",
        "_timings",
        "_worker",
    )

    def __init__(
        self,
        *,
        services: WorkerServices,
        executor: StageExecutor,
        worker: WorkerId,
        timings: WorkerTimings,
        slot: int = 0,
    ) -> None:
        """Bind the loop to its services, its identity and its timings."""
        self._services = services
        self._executor = executor
        self._worker = worker
        self._timings = timings
        self._slot = slot
        self._stopping = asyncio.Event()
        self._draining = False
        self._active: CancellationSource | None = None
        self._starved = False

    @property
    def draining(self) -> bool:
        """Return whether the loop has been asked to wind down."""
        return self._draining

    @property
    def starved(self) -> bool:
        """Return whether the loop is refusing to claim for want of disk."""
        return self._starved

    async def run(self) -> None:
        """Claim and execute until asked to stop."""
        bound = logger.bind(worker=str(self._worker), slot=self._slot)
        bound.info("Claim loop started")
        wait_seconds = self._timings.idle_poll_seconds
        try:
            while not self._stopping.is_set():
                worked = await self._cycle()
                if self._stopping.is_set():
                    break
                if worked:
                    wait_seconds = self._timings.idle_poll_seconds
                    continue
                await self._idle(wait_seconds)
                wait_seconds = self._timings.backoff_from(wait_seconds)
        finally:
            bound.info("Claim loop stopped")

    def drain(self) -> None:
        """Stop claiming, and ask any running job to wind down.

        The job is asked, not killed. It stops at its next stage boundary, is
        checkpointed and is released, so the next worker resumes it rather than
        starting again (``docs/architecture/10-worker-architecture.md`` §10.7).
        """
        self._draining = True
        self._stopping.set()
        if self._active is not None and not self._active.cancelled:
            self._active.cancel(CancellationReason.SHUTDOWN)

    async def _cycle(self) -> bool:
        """Run one claim-and-execute cycle, absorbing anything it throws.

        Returns:
            Whether a job was claimed. Used only to decide how long to wait
            before asking again.
        """
        try:
            return await self.run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The last line of defence. A job that fails in a way nothing below
            # anticipated must cost that job, never the loop.
            logger.opt(exception=True).error("Claim cycle failed; the loop continues")
            return False

    async def run_once(self) -> bool:
        """Claim one job and take it as far as it goes.

        Exposed so the loop's behaviour can be tested without running a timer.

        Returns:
            Whether there was anything to do. A loop that declined to claim for
            want of disk reports ``False``, so it backs off exactly as an idle
            one does rather than spinning on a full device.
        """
        if not self._may_claim():
            return False
        claimed = await self._services.claim.execute(worker=self._worker)
        if claimed is None:
            return False
        await self._execute(claimed)
        return True

    def _may_claim(self) -> bool:
        """Return whether a job may be claimed, given what the disk last did.

        Only consulted once the loop has actually been refused a lease for want
        of space, so a healthy worker never pays for the probe.
        """
        if not self._starved:
            return True
        try:
            free = self._services.workspace.free_bytes()
        except Exception:
            # The probe itself failed - an unmounted volume, most likely. Let
            # the claim through: the attempt fails with the real reason, which
            # is far more useful than a worker that has silently stopped
            # taking work and explains itself nowhere.
            logger.opt(exception=True).warning("Could not read workspace headroom; claiming anyway")
            return True
        if free <= 0:
            return False
        self._starved = False
        logger.bind(worker=str(self._worker), free_bytes=free).info(
            "Workspace headroom recovered; claiming again"
        )
        return True

    def _note_disk_pressure(self, error: BaseException) -> None:
        """Arm the refusal when a lease was denied for want of space.

        Quota refusals are deliberately excluded. A lease that exceeded a
        configured ceiling says nothing about the device, and waiting for space
        that was never the problem would stall the queue indefinitely.
        """
        if not isinstance(error, InsufficientDiskSpaceError) or self._starved:
            return
        self._starved = True
        logger.bind(worker=str(self._worker)).error(
            "The workspace is out of space. This job is requeued; further claims are "
            "held off until headroom returns, so the queue is not spent on the same "
            "failure once per job."
        )

    async def _execute(self, claimed: ClaimedJob) -> None:
        """Run one attempt of a claimed job, then settle it."""
        cancellation = CancellationSource()
        if claimed.cancel_requested:
            cancellation.cancel(CancellationReason.REQUESTED)
        if self._draining:
            cancellation.cancel(CancellationReason.SHUTDOWN)
        self._active = cancellation

        progress = ProgressRegistry(
            clock=self._services.clock,
            min_interval_seconds=self._timings.progress_interval_seconds,
            min_percent_step=self._timings.progress_percent_step,
        )
        heartbeat = Heartbeat(
            claimed,
            extend=self._services.heartbeat,
            report_progress=self._services.report_progress,
            progress=progress,
            cancellation=cancellation,
            clock=self._services.clock,
            interval_seconds=self._timings.heartbeat_seconds,
            flush_seconds=self._timings.progress_interval_seconds,
        )
        beating = asyncio.create_task(heartbeat.run())

        try:
            if cancellation.cancelled:
                # Already stopping before a byte was written. Opening a lease
                # here would create a directory and a manifest only to delete
                # them a moment later - three SD-card writes and three more ways
                # for shutdown to fail, to run zero stages.
                result = ExecutionResult(claim=claimed, failure=_cancelled_before_starting(claimed))
            else:
                with self._services.workspace.lease(label=f"job-{claimed.job_id}") as scope:
                    result = await self._executor.execute(
                        claimed,
                        workspace=scope,
                        cancellation=cancellation,
                        report=progress.observe,
                    )
        except Exception as error:
            # The lease itself failed - most often a full disk, which is an
            # expected outcome on a small device rather than an exception path.
            # Classify it so the job is settled and retried, instead of being
            # left leased until it expires.
            logger.bind(job_id=str(claimed.job_id)).opt(exception=True).warning(
                "Could not run the attempt inside a workspace lease"
            )
            self._note_disk_pressure(error)
            result = ExecutionResult(claim=claimed, failure=classify(error))
        finally:
            heartbeat.stop()
            await _quietly(beating)
            self._active = None

        if not (result.lease_lost or heartbeat.lease_lost):
            await heartbeat.flush_final()
        await self._settle(result, heartbeat=heartbeat, cancellation=cancellation)

    async def _settle(
        self,
        result: ExecutionResult,
        *,
        heartbeat: Heartbeat,
        cancellation: CancellationSource,
    ) -> None:
        """Report the outcome of an attempt, exactly once."""
        if result.lease_lost or heartbeat.lease_lost:
            logger.bind(job_id=str(result.claim.job_id), worker=str(self._worker)).warning(
                "Abandoning a job whose lease was reclaimed; it belongs to another worker"
            )
            return

        claim = result.claim.with_lease(heartbeat.lease)
        try:
            await self._report(result, claim=claim, cancellation=cancellation)
        except LeaseLostError:
            # Reclaimed between the last heartbeat and this write. The other
            # worker's version of events is the one that stands.
            logger.bind(job_id=str(claim.job_id)).warning(
                "Lease was reclaimed while settling; leaving the job to its new owner"
            )

    async def _report(
        self,
        result: ExecutionResult,
        *,
        claim: ClaimedJob,
        cancellation: CancellationSource,
    ) -> None:
        """Call whichever settlement use case this outcome calls for."""
        failure = result.failure
        if failure is None:
            await self._services.complete.execute(claim)
            return
        if failure.is_cancellation:
            if cancellation.reason is CancellationReason.SHUTDOWN:
                # Not a cancellation at all: the process is going away. The job
                # goes back unharmed, and its attempt is refunded.
                await self._services.release.execute(claim)
                return
            await self._services.acknowledge_cancellation.execute(claim)
            return
        await self._services.fail.execute(claim, failure)

    async def _idle(self, seconds: float) -> None:
        """Wait before asking for work again, or until asked to stop."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)


def _cancelled_before_starting(claimed: ClaimedJob) -> FailureReport:
    """Return the report for an attempt that was cancelled before it began.

    Reported against the first stage still outstanding, so the settlement path
    is identical to a cancellation observed between any other two stages.
    """
    stage = next(iter(claimed.checkpoint.remaining(DEFAULT_STAGE_PLAN)), DEFAULT_STAGE_PLAN[0])
    return FailureReport(
        kind=FailureKind.CANCELLED,
        code=CANCELLED_CODE,
        message=CANCELLED_MESSAGE,
        stage=stage,
    )


async def _quietly(task: asyncio.Task[None]) -> None:
    """Await a background task without letting its failure become ours."""
    try:
        await task
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.opt(exception=True).warning("Heartbeat task ended badly")
