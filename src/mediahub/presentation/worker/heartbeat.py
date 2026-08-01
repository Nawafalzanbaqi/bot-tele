"""The background task that keeps one running job honest.

It does three things on a fixed tick, and they belong together:

* **Extends the lease.** This is the only signal that the worker is still alive.
  Stop, and the job is reclaimed and run somewhere else.
* **Reads the cancellation flag** on the same round trip. Cancellation is
  cooperative, so someone has to ask; doing it here means one call, one code
  path, and no separate polling loop to forget
  (``docs/architecture/10-worker-architecture.md`` §10.2).
* **Flushes throttled progress.** The registry decides *whether* a write is due;
  this is what makes the write happen.

The rule that matters most: **this task must never die of an exception.** If it
does, the lease quietly stops being renewed while the job keeps running, the job
is reclaimed, and one job becomes two. Everything below is therefore wrapped,
and the only condition that stops the loop is losing the lease - at which point
stopping is exactly right, because the job now belongs to someone else.

**A backward clock step is treated as "renew now".** A Raspberry Pi has no
battery-backed clock, so it boots in 1970 and jumps to the present the moment
NTP answers - and it is corrected again, in smaller steps, for as long as it
runs. Elapsed time measured by subtracting two wall-clock readings therefore goes
*negative*, and a renewal gated on ``elapsed >= interval`` would then stop firing
until the clock had climbed back past where it started. The lease would expire
under a perfectly healthy job and the work would be run twice. Nothing about that
looks like a clock problem in a log, which is why it is handled here explicitly
rather than assumed away.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.common.cancellation import CancellationReason
from mediahub.application.download.errors import LeaseLostError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.cancellation import CancellationSource
    from mediahub.application.common.ports import Clock
    from mediahub.application.download.queue import ClaimedJob, Lease
    from mediahub.application.download.use_cases.heartbeat_job import HeartbeatJob
    from mediahub.application.download.use_cases.report_job_progress import ReportJobProgress
    from mediahub.presentation.worker.progress import ProgressRegistry

DEFAULT_INTERVAL_SECONDS: Final[float] = 30.0
DEFAULT_FLUSH_SECONDS: Final[float] = 5.0


class Heartbeat:
    """Renews the lease on one job, and notices when it should stop."""

    __slots__ = (
        "_cancellation",
        "_clock",
        "_extend",
        "_flush_seconds",
        "_interval",
        "_last_renewal",
        "_lease",
        "_lost",
        "_progress",
        "_report_progress",
        "_stopping",
    )

    def __init__(
        self,
        claimed: ClaimedJob,
        *,
        extend: HeartbeatJob,
        report_progress: ReportJobProgress,
        progress: ProgressRegistry,
        cancellation: CancellationSource,
        clock: Clock,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
    ) -> None:
        """Bind the heartbeat to one claim and the use cases it drives."""
        self._lease = claimed.lease
        self._extend = extend
        self._report_progress = report_progress
        self._progress = progress
        self._cancellation = cancellation
        self._clock = clock
        self._interval = interval_seconds
        self._flush_seconds = flush_seconds
        self._last_renewal = claimed.lease.acquired_at
        self._lost = False
        self._stopping = asyncio.Event()

    @property
    def lease(self) -> Lease:
        """Return the most recent lease, which may have been extended."""
        return self._lease

    @property
    def lease_lost(self) -> bool:
        """Return whether the job stopped belonging to this worker."""
        return self._lost

    @property
    def renewal_due(self) -> bool:
        """Return whether the lease would be renewed on the next tick.

        Exposed alongside :attr:`lease_lost` because it is the other question
        worth asking from outside: the clock-jump behaviour is a decision, and a
        decision that can only be observed by ticking is a decision that gets
        tested through its side effects instead of directly.
        """
        return self._renewal_due()

    async def run(self) -> None:
        """Tick until asked to stop, or until the lease is gone."""
        while not self._stopping.is_set() and not self._lost:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick_seconds)
            if self._stopping.is_set():
                break
            await self.tick()

    def stop(self) -> None:
        """Ask the heartbeat to finish after the current tick."""
        self._stopping.set()

    async def tick(self) -> None:
        """Flush what is due and renew the lease if it is time.

        Exposed so the behaviour can be tested without waiting on wall-clock
        time - the loop is a timer around this method and nothing more.
        """
        await self._flush(final=False)
        if self._renewal_due():
            await self._renew()

    async def flush_final(self) -> None:
        """Write the last observation, throttle or no throttle."""
        await self._flush(final=True)

    @property
    def _tick_seconds(self) -> float:
        """Return how long to sleep between ticks.

        The faster of the two duties wins: progress must not lag by a lease
        period, and the lease must not lag by anything at all.
        """
        return max(0.05, min(self._interval, self._flush_seconds))

    def _renewal_due(self) -> bool:
        """Return whether enough time has passed to renew the lease.

        Negative elapsed time means the wall clock moved backwards under us. The
        safe reading of that is "renew now": renewing early costs one round trip,
        and not renewing costs the job.
        """
        elapsed = (self._clock.now() - self._last_renewal).total_seconds()
        if elapsed < 0:
            logger.bind(job_id=str(self._lease.job_id), drift_seconds=round(elapsed, 3)).warning(
                "The clock stepped backwards; renewing the lease immediately"
            )
            return True
        return elapsed >= self._interval

    async def _renew(self) -> None:
        """Extend the lease and act on what comes back."""
        try:
            state = await self._extend.execute(self._lease)
        except LeaseLostError:
            self._surrender(CancellationReason.TIMEOUT, "Lease lost; abandoning the job")
            return
        except Exception:
            # A database blip is weather, not an incident: the lease still has
            # most of its life, and the next tick will try again.
            logger.opt(exception=True).warning("Heartbeat failed; will retry on the next tick")
            return

        self._lease = state.lease
        self._last_renewal = self._clock.now()
        if state.cancel_requested and not self._cancellation.cancelled:
            logger.bind(job_id=str(self._lease.job_id)).info(
                "Cancellation requested; asking the job to stop"
            )
            self._cancellation.cancel(CancellationReason.REQUESTED)

    async def _flush(self, *, final: bool) -> None:
        """Write pending progress, if any is due."""
        observation = self._progress.take_final() if final else self._progress.take_due()
        if observation is None:
            return
        try:
            await self._report_progress.execute(self._lease, observation)
        except LeaseLostError:
            self._surrender(CancellationReason.TIMEOUT, "Lease lost while reporting progress")
        except Exception:
            # Progress is telemetry. Losing an update costs nothing; failing the
            # job over one would cost everything.
            logger.opt(exception=True).debug("Progress write failed; dropping the observation")

    def _surrender(self, reason: CancellationReason, message: str) -> None:
        """Record that the job is no longer ours and stop touching it."""
        self._lost = True
        self._stopping.set()
        self._cancellation.cancel(reason)
        logger.bind(job_id=str(self._lease.job_id), owner=str(self._lease.owner)).warning(message)
