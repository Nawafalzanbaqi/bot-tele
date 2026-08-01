"""The supervisor: readiness, recovery, slots, drain.

Everything a worker process does that is not "run one job" happens here.

**Readiness is checked before anything is claimed.** A worker with a stage in
its plan and no handler for it would claim jobs only to fail them, burning an
attempt each time; it refuses to start instead
(``docs/architecture/10-worker-architecture.md`` §10.5).

**Startup recovers this identity's own leases.** Worker identity is stable
(``host:role:index``) precisely so that a restarted process can take back the
jobs the previous incarnation was holding, instead of waiting a full lease
period for them to lapse. Expired leases from *any* worker are optionally swept
at the same time, so a single-worker deployment recovers from a crash without a
scheduler being present.

**Slots are just loops.** Concurrency is a number of identical claim loops, not
a pool, a scheduler or a queue of its own. Two workers on one machine differ
only by their index.

**A slot that ends on its own ends the process.** The claim loop is written so
that nothing a job does can stop it, which means a loop that has finished
without being asked to is a defect above the level anything here can repair - a
cancelled task, an error escaping the last safety net, an event loop torn down
underneath it. Waiting only on the stop signal would leave that process alive,
answering its liveness probe, holding its lease, and claiming nothing, for as
long as nobody looked. On an unattended device "nobody looked" is the normal
case, so the supervisor watches the slots as well as the signal and lets the
process exit into whatever restarts it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.dto import LeaseRecovery
from mediahub.application.download.errors import WorkerNotReadyError
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.executor import StageExecutor
from mediahub.presentation.worker.loop import ClaimLoop
from mediahub.presentation.worker.shutdown import ShutdownController
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.download.queue import JobStage, WorkerId
    from mediahub.presentation.worker.services import WorkerServices
    from mediahub.presentation.worker.stages.base import StageRegistry


class WorkerRuntime:
    """Owns the lifecycle of one worker process."""

    __slots__ = (
        "_handlers",
        "_loops",
        "_plan",
        "_reclaim_expired",
        "_recover_own",
        "_services",
        "_shutdown",
        "_slots",
        "_started",
        "_timings",
        "_worker",
    )

    def __init__(
        self,
        *,
        services: WorkerServices,
        handlers: StageRegistry,
        worker: WorkerId,
        timings: WorkerTimings | None = None,
        plan: Sequence[JobStage] = DEFAULT_STAGE_PLAN,
        slots: int = 1,
        recover_own_leases: bool = True,
        reclaim_expired_leases: bool = True,
    ) -> None:
        """Assemble a worker from its services, handlers and identity.

        Raises:
            WorkerNotReadyError: If asked for fewer than one slot.
        """
        if slots < 1:
            message = f"a worker needs at least one slot, got {slots}"
            raise WorkerNotReadyError(message)
        self._services = services
        self._handlers = handlers
        self._worker = worker
        self._timings = timings or WorkerTimings()
        self._plan = tuple(plan)
        self._slots = slots
        self._recover_own = recover_own_leases
        self._reclaim_expired = reclaim_expired_leases
        self._started = False
        self._loops: list[ClaimLoop] = []
        self._shutdown = ShutdownController(on_stop=self.drain)

    @property
    def worker_id(self) -> WorkerId:
        """Return this process's stable identity."""
        return self._worker

    @property
    def shutdown(self) -> ShutdownController:
        """Return the controller signals should be routed to."""
        return self._shutdown

    async def start(self) -> LeaseRecovery:
        """Validate the configuration and take back this worker's own jobs.

        Returns:
            What the startup sweep reclaimed.

        Raises:
            WorkerNotReadyError: If a stage in the plan has no handler. Refusing
                is the point: a worker that cannot finish a job must not start
                one, because claiming costs the job an attempt.
        """
        missing = self._handlers.missing_for(self._plan)
        if missing:
            names = ", ".join(stage.value for stage in missing)
            message = f"no handler is registered for these stages: {names}"
            raise WorkerNotReadyError(message)

        recovery = LeaseRecovery()
        if self._recover_own:
            recovery = await self._services.recover_leases.execute(worker=self._worker)
        if self._reclaim_expired:
            expired = await self._services.recover_leases.execute()
            recovery = _merge(recovery, expired)

        self._started = True
        logger.bind(
            worker=str(self._worker),
            slots=self._slots,
            plan=[stage.value for stage in self._plan],
            recovered=recovery.total,
        ).info("Worker ready")
        return recovery

    async def run(self) -> None:
        """Start every slot and run until asked to stop, or until one dies.

        The process outlives any single job: a slot that dies is a bug, and the
        supervisor reports it and stops rather than idling on quietly.
        """
        if not self._started:
            await self.start()

        self._loops = [self._build_loop(slot) for slot in range(self._slots)]
        tasks = [
            asyncio.create_task(loop.run(), name=f"{self._worker}-slot-{slot}")
            for slot, loop in enumerate(self._loops)
        ]
        stopping = asyncio.create_task(self._shutdown.wait(), name="shutdown")
        try:
            await self._await_stop(tasks, stopping)
            await self._await_drain(tasks)
        finally:
            stopping.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(stopping, *tasks, return_exceptions=True)
            self._loops = []
            logger.bind(worker=str(self._worker)).info("Worker stopped")

    async def _await_stop(
        self, tasks: Sequence[asyncio.Task[None]], stopping: asyncio.Task[None]
    ) -> None:
        """Wait for a stop request, or for a slot to end without one.

        Whichever arrives first, the outcome is the same shape: every slot is
        asked to drain and the process winds down. Only the log line differs,
        and it differs in the way that matters to whoever reads it at 3 a.m.
        """
        await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
        if stopping.done():
            return

        for slot, task in enumerate(tasks):
            if not task.done():
                continue
            failure = None if task.cancelled() else task.exception()
            logger.bind(worker=str(self._worker), slot=slot).opt(exception=failure).error(
                "A claim loop ended without being asked to; stopping so this worker is "
                "restarted rather than left running with a dead slot"
            )
        self.drain()

    def drain(self) -> None:
        """Ask every slot to stop claiming and wind down what it is running."""
        for loop in self._loops:
            loop.drain()

    async def _await_drain(self, tasks: Sequence[asyncio.Task[None]]) -> None:
        """Give in-flight work its grace period, then stop waiting.

        Not waiting for ever is deliberate. A stage that will not wind down is
        recovered by its lease expiring, which is the same path a power cut
        takes - there is no code path here that depends on shutdown being
        graceful.
        """
        grace = 0.0 if self._shutdown.immediate else self._timings.drain_grace_seconds
        _, pending = await asyncio.wait(tasks, timeout=grace)
        if pending:
            logger.bind(worker=str(self._worker), pending=len(pending)).warning(
                "Drain grace expired; the remaining leases will be recovered when they lapse"
            )

    def _build_loop(self, slot: int) -> ClaimLoop:
        """Build one claim loop, with its own executor over the shared plan."""
        return ClaimLoop(
            services=self._services,
            executor=StageExecutor(
                handlers=self._handlers,
                checkpoint=self._services.checkpoint,
                plan=self._plan,
            ),
            worker=self._worker,
            timings=self._timings,
            slot=slot,
        )


def _merge(first: LeaseRecovery, second: LeaseRecovery) -> LeaseRecovery:
    """Combine two recovery sweeps into one report."""
    return LeaseRecovery(
        reclaimed=(*first.reclaimed, *second.reclaimed),
        abandoned=(*first.abandoned, *second.abandoned),
    )
