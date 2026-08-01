"""Runs the stages of one job, in order, checkpointing between them.

The executor is the smallest interesting object in the worker, and it is
deliberately boring:

1. work out which stages are left, from the checkpoint and the plan;
2. before each one, check whether we have been asked to stop;
3. run it;
4. record that it finished.

Two properties come out of that shape and are worth naming.

**Resuming is free.** Step 1 is a set difference, so a job reclaimed after two
completed stages starts at the third without anybody writing resume logic.

**Nothing escapes untyped.** Step 3 is the only place a stage's exception can
appear, and it is classified there into a
:class:`~mediahub.application.download.failures.FailureReport`. The executor
therefore does not raise for job-level problems - it *returns* how the job went,
which is what lets the claim loop stay free of exception handling and lets tests
assert on an outcome instead of on a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.errors import LeaseLostError
from mediahub.application.download.failures import FailureReport, classify
from mediahub.domain.download.enums import FailureKind
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN, StageContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.queue import ClaimedJob, JobStage
    from mediahub.application.download.use_cases.checkpoint_job import CheckpointJob
    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.presentation.worker.stages.base import ProgressSink, StageRegistry

CANCELLED_CODE = "cancelled"
CANCELLED_MESSAGE = "the job was asked to stop"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """How one attempt went.

    Attributes:
        claim: The claim as it now stands, carrying the latest checkpoint. Even
            a failed attempt returns it: the stages that *did* finish must not
            be forgotten, or the next attempt repeats them.
        failure: Why it stopped, or ``None`` if every stage ran.
        lease_lost: Whether the job stopped belonging to this worker. When true
            the caller must not settle the job: someone else owns it now, and a
            second writer is how one job becomes two.
    """

    claim: ClaimedJob
    failure: FailureReport | None = None
    lease_lost: bool = False

    @property
    def succeeded(self) -> bool:
        """Return whether the whole plan ran."""
        return self.failure is None

    @property
    def cancelled(self) -> bool:
        """Return whether the attempt stopped because it was asked to."""
        return self.failure is not None and self.failure.is_cancellation


class StageExecutor:
    """Executes the remaining stages of one claimed job."""

    __slots__ = ("_checkpoint", "_handlers", "_plan")

    def __init__(
        self,
        *,
        handlers: StageRegistry,
        checkpoint: CheckpointJob,
        plan: Sequence[JobStage] = DEFAULT_STAGE_PLAN,
    ) -> None:
        """Bind the executor to its handlers and the sequence they run in."""
        self._handlers = handlers
        self._checkpoint = checkpoint
        self._plan = tuple(plan)

    @property
    def plan(self) -> tuple[JobStage, ...]:
        """Return the stage sequence this executor runs."""
        return self._plan

    async def execute(
        self,
        claimed: ClaimedJob,
        *,
        workspace: WorkspaceScope,
        cancellation: CancellationToken,
        report: ProgressSink,
    ) -> ExecutionResult:
        """Run every stage that has not already been completed.

        Args:
            claimed: The claim held by the caller.
            workspace: The lease this attempt writes inside.
            cancellation: Checked before each stage, and by the stages
                themselves while they work.
            report: Where handlers send progress.

        Returns:
            The outcome. Never raises for a job-level failure - that is the
            point.
        """
        current = claimed
        for stage in claimed.checkpoint.remaining(self._plan):
            if cancellation.cancelled:
                return ExecutionResult(claim=current, failure=_cancelled_at(stage))

            bound = logger.bind(
                job_id=str(current.job_id), stage=stage.value, attempt=current.attempt
            )
            try:
                handler = self._handlers.for_stage(stage)
                outcome = await handler.execute(
                    StageContext(
                        job_id=current.job_id,
                        stage=stage,
                        attempt=current.attempt,
                        checkpoint=current.checkpoint,
                        workspace=workspace,
                        cancellation=cancellation,
                        report=report,
                    )
                )
                checkpoint = await self._checkpoint.execute(
                    current, stage, resume_token=outcome.resume_token
                )
            except LeaseLostError as error:
                # Someone else owns this job now. Stop immediately and do not
                # settle it: whatever it is doing, it is not ours to report on.
                bound.warning("Lease lost mid-stage; abandoning the attempt")
                return ExecutionResult(
                    claim=current, failure=classify(error, stage=stage), lease_lost=True
                )
            except Exception as error:
                # Deliberately broad: classification is this executor's job, and
                # an unrecognised error becomes a transient failure rather than
                # a dead worker.
                bound.opt(exception=True).warning("Stage failed")
                return ExecutionResult(claim=current, failure=classify(error, stage=stage))

            current = current.with_checkpoint(checkpoint)
            bound.debug("Stage completed")

        # A cancellation that arrives during the final stage is deliberately not
        # honoured here: the work is done, and telling a user their delivery was
        # cancelled after it succeeded would be a lie
        # (``docs/architecture/07-download-pipeline.md`` §7.6).
        return ExecutionResult(claim=current)


def _cancelled_at(stage: JobStage) -> FailureReport:
    """Return the report for a job that stopped between stages."""
    return FailureReport(
        kind=FailureKind.CANCELLED,
        code=CANCELLED_CODE,
        message=CANCELLED_MESSAGE,
        stage=stage,
    )
