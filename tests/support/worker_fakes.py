"""A worker assembled from real parts, with scriptable stages.

Everything here is the production object except the stage handlers, which is
exactly the seam the runtime is designed around: the loop, the executor, the
heartbeat, the leases, the checkpoints and the use cases are all real, and a
test decides only what a stage *does*.

That is what makes the crash and cancellation tests meaningful. Nothing is
mocked out from under them - a job that is reclaimed here is reclaimed by the
same code a power cut would exercise.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from mediahub.application.download.queue import JobStage, WorkerId
from mediahub.application.download.use_cases.acknowledge_cancellation import (
    AcknowledgeCancellation,
)
from mediahub.application.download.use_cases.checkpoint_job import CheckpointJob
from mediahub.application.download.use_cases.claim_job import ClaimJob
from mediahub.application.download.use_cases.complete_job import CompleteJob
from mediahub.application.download.use_cases.fail_job import FailJob
from mediahub.application.download.use_cases.heartbeat_job import HeartbeatJob
from mediahub.application.download.use_cases.recover_leases import RecoverLeases
from mediahub.application.download.use_cases.release_job import ReleaseJob
from mediahub.application.download.use_cases.report_job_progress import ReportJobProgress
from mediahub.domain.download.entities import DownloadJob
from mediahub.domain.download.enums import JobPriority
from mediahub.domain.download.value_objects import JobId, RetryPolicy
from mediahub.domain.media.value_objects import MediaId, SourceUrl
from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
from mediahub.infrastructure.persistence.memory.queue import InMemoryJobQueue
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.executor import StageExecutor
from mediahub.presentation.worker.loop import ClaimLoop
from mediahub.presentation.worker.runtime import WorkerRuntime
from mediahub.presentation.worker.services import WorkerServices
from mediahub.presentation.worker.stages.base import (
    DEFAULT_STAGE_PLAN,
    StageOutcome,
    StageRegistry,
)
from tests.conftest import FrozenClock, RecordingEventPublisher

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mediahub.presentation.worker.stages.base import StageContext, StageHandler

WORKER = WorkerId(host="pi", role="worker", index=0)
OTHER_WORKER = WorkerId(host="pi", role="worker", index=1)


@dataclass
class RecordingStageHandler:
    """A stage that records every time it runs, and does what it is told.

    Attributes:
        stage: Which stage it implements.
        calls: How many times it has been executed. The count is the assertion
            that matters for crash recovery: a resumed job must not re-run a
            stage it already finished.
        error: Raised on execution, when set.
        error_times: How many calls raise it. ``None`` means every call, which
            is what a permanent failure looks like; ``1`` means "fails once,
            then works", which is what a retry has to survive.
        resume_token: Handed back for the checkpoint, when set.
        on_execute: Arbitrary hook, for tests that need to interfere - report
            progress, request a cancellation, kill a worker mid-stage.
        bytes_written: Bytes to write into the lease, to prove the workspace is
            reclaimed afterwards.
    """

    stage: JobStage
    calls: int = 0
    error: BaseException | None = None
    error_times: int | None = None
    resume_token: str | None = None
    on_execute: Callable[[StageContext], object] | None = None
    bytes_written: int = 0
    contexts: list[StageContext] = field(default_factory=list)

    async def execute(self, context: StageContext) -> StageOutcome:
        """Record the call, run the hook, then succeed or fail as scripted."""
        self.calls += 1
        self.contexts.append(context)
        if self.bytes_written:
            path = context.workspace.path_for(f"{self.stage.value}.bin")
            path.write_bytes(b"x" * self.bytes_written)
        if self.on_execute is not None:
            hooked = self.on_execute(context)
            if inspect.isawaitable(hooked):
                await hooked
        if self.error is not None and (self.error_times is None or self.calls <= self.error_times):
            raise self.error
        return StageOutcome(resume_token=self.resume_token)


def handlers_for(stages: Sequence[JobStage] = DEFAULT_STAGE_PLAN) -> list[RecordingStageHandler]:
    """Return one recording handler per stage, in plan order."""
    return [RecordingStageHandler(stage=stage) for stage in stages]


@dataclass
class WorkerHarness:
    """A complete worker, wired to in-memory adapters and a real workspace."""

    clock: FrozenClock
    unit_of_work: InMemoryUnitOfWorkFactory
    queue: InMemoryJobQueue
    events: RecordingEventPublisher
    workspace: FilesystemWorkspace
    handlers: list[RecordingStageHandler]
    timings: WorkerTimings
    plan: tuple[JobStage, ...]
    next_id: int = 100

    @classmethod
    def build(
        cls,
        tmp_path: Path,
        *,
        plan: Sequence[JobStage] = DEFAULT_STAGE_PLAN,
        timings: WorkerTimings | None = None,
    ) -> WorkerHarness:
        """Assemble a harness whose only fakes are the stage handlers."""
        unit_of_work = InMemoryUnitOfWorkFactory()
        return cls(
            clock=FrozenClock(),
            unit_of_work=unit_of_work,
            queue=InMemoryJobQueue(unit_of_work.database),
            events=RecordingEventPublisher(),
            workspace=FilesystemWorkspace(tmp_path / "workspace"),
            handlers=handlers_for(plan),
            timings=timings or WorkerTimings(),
            plan=tuple(plan),
        )

    # -- Wiring --------------------------------------------------------------

    @property
    def services(self) -> WorkerServices:
        """Return the use cases a worker is allowed to call."""
        return WorkerServices(
            claim=ClaimJob(
                queue=self.queue,
                unit_of_work=self.unit_of_work,
                clock=self.clock,
                event_publisher=self.events,
                lease_seconds=self.timings.lease_seconds,
            ),
            heartbeat=HeartbeatJob(
                queue=self.queue,
                clock=self.clock,
                lease_seconds=self.timings.lease_seconds,
            ),
            checkpoint=CheckpointJob(queue=self.queue, clock=self.clock),
            report_progress=ReportJobProgress(
                queue=self.queue, unit_of_work=self.unit_of_work, clock=self.clock
            ),
            complete=CompleteJob(
                queue=self.queue,
                unit_of_work=self.unit_of_work,
                clock=self.clock,
                event_publisher=self.events,
            ),
            fail=FailJob(
                queue=self.queue,
                unit_of_work=self.unit_of_work,
                clock=self.clock,
                event_publisher=self.events,
            ),
            release=ReleaseJob(
                queue=self.queue,
                unit_of_work=self.unit_of_work,
                clock=self.clock,
                event_publisher=self.events,
            ),
            acknowledge_cancellation=AcknowledgeCancellation(
                queue=self.queue,
                unit_of_work=self.unit_of_work,
                clock=self.clock,
                event_publisher=self.events,
            ),
            recover_leases=RecoverLeases(
                queue=self.queue,
                unit_of_work=self.unit_of_work,
                clock=self.clock,
                event_publisher=self.events,
            ),
            workspace=self.workspace,
            clock=self.clock,
        )

    def registry(self, handlers: Sequence[StageHandler] | None = None) -> StageRegistry:
        """Return the stage registry, defaulting to the recording handlers."""
        return StageRegistry(self.handlers if handlers is None else handlers)

    def executor(self, handlers: Sequence[StageHandler] | None = None) -> StageExecutor:
        """Return an executor over this harness's plan."""
        return StageExecutor(
            handlers=self.registry(handlers),
            checkpoint=CheckpointJob(queue=self.queue, clock=self.clock),
            plan=self.plan,
        )

    def loop(
        self,
        *,
        worker: WorkerId = WORKER,
        handlers: Sequence[StageHandler] | None = None,
    ) -> ClaimLoop:
        """Return a claim loop for one slot."""
        return ClaimLoop(
            services=self.services,
            executor=self.executor(handlers),
            worker=worker,
            timings=self.timings,
        )

    def runtime(self, *, worker: WorkerId = WORKER, slots: int = 1) -> WorkerRuntime:
        """Return a supervisor over this harness."""
        return WorkerRuntime(
            services=self.services,
            handlers=self.registry(),
            worker=worker,
            timings=self.timings,
            plan=self.plan,
            slots=slots,
        )

    def handler(self, stage: JobStage) -> RecordingStageHandler:
        """Return the recording handler for one stage."""
        return next(handler for handler in self.handlers if handler.stage is stage)

    # -- Fixtures ------------------------------------------------------------

    async def enqueue(
        self,
        *,
        priority: JobPriority = JobPriority.NORMAL,
        max_attempts: int = 3,
        backoff_seconds: int = 30,
    ) -> JobId:
        """Create a queued job through the domain, as a real request would."""
        self.next_id += 1
        job_id = JobId(UUID(int=self.next_id))
        job = DownloadJob.request(
            job_id=job_id,
            media_id=MediaId(UUID(int=self.next_id + 1000)),
            source_url=SourceUrl(f"https://example.com/{self.next_id}.mp4"),
            priority=priority,
            retry_policy=RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff_seconds),
            now=self.clock.now(),
        )
        async with self.unit_of_work() as uow:
            await uow.download_jobs.add(job)
            await uow.commit()
        return job_id

    async def job(self, job_id: JobId) -> DownloadJob:
        """Return the stored job. Fails loudly if it is missing."""
        async with self.unit_of_work() as uow:
            job = await uow.download_jobs.get(job_id)
        assert job is not None
        return job

    def leases_in_use(self) -> int:
        """Return how many jobs are currently leased."""
        return sum(1 for entry in self.queue._entries.values() if entry.lease is not None)

    def workspace_directories(self) -> list[str]:
        """Return the lease directories still on disk.

        The product's central promise is that these do not survive a job. An
        orphan here is a leaked file on a device with a 32 GB card.
        """
        root = self.workspace.root
        if not root.is_dir():
            return []
        return sorted(entry.name for entry in root.iterdir() if entry.is_dir())
