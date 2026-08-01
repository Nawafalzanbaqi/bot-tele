"""The acquisition pipeline, assembled from real parts on top of fake edges.

Everything between the queue and the workspace is production code here - the
claim loop, the executor, the checkpoints, the leases, the five stage handlers
and the steps they drive. What is faked is the internet at one end and the
destination at the other, which is the only honest place to fake anything: a
crash recovered in these tests is recovered by the same code a power cut would
exercise.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mediahub.application.common.cancellation import CancellationReason, CancellationSource
from mediahub.application.delivery.ports import DeliveryTarget, TargetAddress
from mediahub.application.download.queue import Checkpoint, JobStage, StageProgress
from mediahub.application.download.use_cases.get_download_job import GetDownloadJob
from mediahub.infrastructure.delivery.registry import (
    DeliveryProviderRegistry,
    ProviderRegistration,
)
from mediahub.infrastructure.persistence.memory.journal import InMemoryAcquisitionJournal
from mediahub.presentation.worker.executor import StageExecutor
from mediahub.presentation.worker.loop import ClaimLoop
from mediahub.presentation.worker.runtime import WorkerRuntime
from mediahub.presentation.worker.stages import (
    AcquisitionPolicy,
    AcquisitionSteps,
    PipelineState,
    StageContext,
    StageRegistry,
    acquisition_handlers,
)
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN
from tests.support.delivery_fakes import FakeDeliveryProvider
from tests.support.download_fakes import FakeDownloader
from tests.support.worker_fakes import WORKER, WorkerHarness

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from mediahub.application.download.queue import WorkerId
    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.domain.download.value_objects import JobId
    from mediahub.presentation.worker.stages import StageHandler, StageOutcome

TARGET_PROVIDER = "fake"
PRINCIPAL = "pi:worker:0"


def request_cancellation(context: StageContext) -> None:
    """Ask the running attempt to stop, from inside a stage.

    A stage is handed the *read* side of the token, which is right: work must
    not be able to cancel itself. A test standing in for the person who pressed
    cancel reaches for the write side the claim loop actually holds.
    """
    source = context.cancellation
    assert isinstance(source, CancellationSource)
    source.cancel(CancellationReason.REQUESTED)


def target_for(provider: str = TARGET_PROVIDER) -> DeliveryTarget:
    """Return the destination the worker delivers a queued job to."""
    return DeliveryTarget(
        provider=provider,
        address=TargetAddress(provider=provider),
        label="default destination",
    )


@dataclass
class ObservedStage:
    """A real stage handler, with a place to count it and a place to kill it.

    Wrapping rather than replacing matters: the handler underneath is the
    production one, so a crash injected here is a crash in the real pipeline
    rather than in a stand-in for it.

    Attributes:
        inner: The production handler.
        error: Raised instead of running, when set.
        error_times: How many calls raise it. ``None`` means every call.
        before: Hook run before the handler, for a test that needs to interfere
            - request a cancellation, fill the disk, reclaim the lease.
        calls: How many times this stage has been entered.
    """

    inner: StageHandler
    error: BaseException | None = None
    error_times: int | None = None
    before: Callable[[StageContext], None] | None = None
    calls: int = 0

    @property
    def stage(self) -> JobStage:
        """Return the stage the wrapped handler implements."""
        return self.inner.stage

    async def execute(self, context: StageContext) -> StageOutcome:
        """Count the call, run the hook, then crash or delegate."""
        self.calls += 1
        if self.before is not None:
            self.before(context)
        if self.error is not None and (self.error_times is None or self.calls <= self.error_times):
            raise self.error
        return await self.inner.execute(context)


@dataclass
class PipelineHarness:
    """A worker whose stages are the real acquisition pipeline."""

    worker: WorkerHarness
    downloader: FakeDownloader
    destination: FakeDeliveryProvider
    router: DeliveryProviderRegistry
    journal: InMemoryAcquisitionJournal
    steps: AcquisitionSteps
    handlers: tuple[ObservedStage, ...]
    observed: list[StageProgress] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        tmp_path: Path,
        *,
        downloader: FakeDownloader | None = None,
        destination: FakeDeliveryProvider | None = None,
        max_item_bytes: int | None = None,
        plan: Sequence[JobStage] = DEFAULT_STAGE_PLAN,
    ) -> PipelineHarness:
        """Assemble the whole pipeline over one temporary workspace."""
        worker = WorkerHarness.build(tmp_path, plan=plan)
        engine = downloader or FakeDownloader()
        provider = destination or FakeDeliveryProvider()
        router = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(provider=provider)],
            default_provider=provider.name,
        )
        journal = InMemoryAcquisitionJournal()
        steps = AcquisitionSteps(
            downloader=engine,
            delivery=router,
            journal=journal,
            jobs=GetDownloadJob(unit_of_work=worker.unit_of_work),
            policy=AcquisitionPolicy(
                target=target_for(provider.name),
                principal=PRINCIPAL,
                max_item_bytes=max_item_bytes,
            ),
        )
        return cls(
            worker=worker,
            downloader=engine,
            destination=provider,
            router=router,
            journal=journal,
            steps=steps,
            handlers=tuple(ObservedStage(inner=handler) for handler in acquisition_handlers(steps)),
        )

    # -- Wiring --------------------------------------------------------------

    def registry(self) -> StageRegistry:
        """Return the registry a worker would be started with."""
        return StageRegistry(self.handlers)

    def executor(self) -> StageExecutor:
        """Return an executor over the real handlers."""
        return StageExecutor(
            handlers=self.registry(),
            checkpoint=self.worker.services.checkpoint,
            plan=self.worker.plan,
        )

    def loop(self, *, worker: WorkerId = WORKER) -> ClaimLoop:
        """Return a claim loop that executes the real pipeline."""
        return ClaimLoop(
            services=self.worker.services,
            executor=self.executor(),
            worker=worker,
            timings=self.worker.timings,
        )

    def runtime(self, *, worker: WorkerId = WORKER, slots: int = 1) -> WorkerRuntime:
        """Return a supervisor over the real pipeline."""
        return WorkerRuntime(
            services=self.worker.services,
            handlers=self.registry(),
            worker=worker,
            timings=self.worker.timings,
            plan=self.worker.plan,
            slots=slots,
        )

    def handler(self, stage: JobStage) -> ObservedStage:
        """Return the observable wrapper around one stage's handler."""
        return next(handler for handler in self.handlers if handler.stage is stage)

    def calls(self) -> dict[str, int]:
        """Return how many times each stage has been entered, for assertions."""
        return {handler.stage.value: handler.calls for handler in self.handlers}

    # -- Driving stages directly ---------------------------------------------

    @contextlib.contextmanager
    def lease(self, label: str = "job") -> Iterator[WorkspaceScope]:
        """Open a workspace lease, exactly as the claim loop would."""
        with self.worker.workspace.lease(label=label) as scope:
            yield scope

    def context(
        self,
        stage: JobStage,
        *,
        job_id: JobId,
        workspace: WorkspaceScope,
        checkpoint: Checkpoint | None = None,
        cancellation: CancellationSource | None = None,
        attempt: int = 1,
    ) -> StageContext:
        """Return the context the executor would hand a handler."""
        return StageContext(
            job_id=job_id,
            stage=stage,
            attempt=attempt,
            checkpoint=checkpoint if checkpoint is not None else Checkpoint(),
            workspace=workspace,
            cancellation=cancellation or CancellationSource(),
            report=self.observed.append,
        )

    async def run_stage(
        self,
        stage: JobStage,
        *,
        job_id: JobId,
        workspace: WorkspaceScope,
        state: PipelineState | None = None,
        cancellation: CancellationSource | None = None,
    ) -> PipelineState:
        """Run one stage over ``state`` and return the state it recorded."""
        outcome = await self.handler(stage).execute(
            self.context(
                stage,
                job_id=job_id,
                workspace=workspace,
                checkpoint=Checkpoint(resume_token=None if state is None else state.encode()),
                cancellation=cancellation,
            )
        )
        return PipelineState.decode(outcome.resume_token)

    # -- Reading the world ---------------------------------------------------

    def state_of(self, job_id: JobId) -> PipelineState:
        """Return the durable pipeline state recorded for a job."""
        return PipelineState.decode(self.worker.queue.checkpoint_of(job_id).resume_token)

    def completed_stages(self, job_id: JobId) -> tuple[JobStage, ...]:
        """Return the stages the queue believes are finished."""
        return self.worker.queue.checkpoint_of(job_id).completed_stages

    def progress_for(self, stage: JobStage) -> list[StageProgress]:
        """Return every observation reported while ``stage`` was running."""
        return [update for update in self.observed if update.stage is stage]
