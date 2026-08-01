"""The cleanup stage: give the local bytes back, once it is safe to.

The claim loop deletes the lease whatever happens, so this stage is not what
stops files leaking. What it does is make the release **ordered**: it refuses to
remove anything until a receipt proves the destination holds it, which is the
one ordering in the pipeline that separates "a leaked file a sweep will find"
from "both copies are gone" (``docs/architecture/07-download-pipeline.md`` §7.5).

Removal goes through the workspace, never through the filesystem. A stage that
knew a path would be a stage that could delete something outside its lease.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mediahub.application.download.queue import JobStage
from mediahub.presentation.worker.stages.handler import PipelineStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.presentation.worker.stages.base import StageContext
    from mediahub.presentation.worker.stages.state import PipelineState


class CleanupStage(PipelineStage):
    """Releases the local copy after - and only after - delivery is proved."""

    STAGE: ClassVar[JobStage] = JobStage.CLEANUP

    __slots__ = ()

    async def advance(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Return the state with the local copy recorded as released."""
        return await self._steps.ensure_released(context, state)
