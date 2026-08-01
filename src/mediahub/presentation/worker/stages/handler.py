"""What every acquisition stage handler does, which is almost nothing.

A handler decodes the state the last stage left, asks
:class:`~mediahub.presentation.worker.stages.acquisition.AcquisitionSteps` to
guarantee one more thing about it, and hands the result back for the runtime to
checkpoint. That is the whole of it, and it is deliberate: a handler that grew a
decision would have put a rule in the presentation layer, where nothing else can
reach it.

The base class exists so that "decode, advance, encode" is written once and the
five stages differ only in which step they stand for - which is exactly how much
they should differ.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mediahub.presentation.worker.stages.base import StageOutcome
from mediahub.presentation.worker.stages.state import PipelineState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.queue import JobStage
    from mediahub.presentation.worker.stages.acquisition import AcquisitionSteps
    from mediahub.presentation.worker.stages.base import StageContext


class PipelineStage:
    """One stage of the acquisition pipeline, backed by one idempotent step."""

    STAGE: ClassVar[JobStage]
    """Which stage of the plan this handler implements."""

    __slots__ = ("_steps",)

    def __init__(self, steps: AcquisitionSteps) -> None:
        """Bind the handler to the steps every stage shares."""
        self._steps = steps

    @property
    def stage(self) -> JobStage:
        """Return the stage this handler implements."""
        return self.STAGE

    async def execute(self, context: StageContext) -> StageOutcome:
        """Advance the pipeline by one stage and return the state to record.

        The returned resume token is the *whole* durable state of the job, so
        the checkpoint that records this stage as complete and the state it
        produced are one write. They cannot disagree, which is what a resumed
        attempt depends on.

        Raises:
            DownloadError: For anything that went wrong, classified so the
                executor's retry decision can be read from a field.
            DownloadCancelledError: If the job was asked to stop.
        """
        state = PipelineState.decode(context.checkpoint.resume_token)
        advanced = await self.advance(context, state)
        return StageOutcome(resume_token=advanced.encode())

    async def advance(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Return the state with this stage's step guaranteed."""
        raise NotImplementedError
