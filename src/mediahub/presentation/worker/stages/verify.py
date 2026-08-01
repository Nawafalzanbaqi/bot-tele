"""The verify stage: prove the bytes are the bytes that were asked for.

Everything it asserts is asserted by the workspace, which is the only component
that knows what a lease is allowed to contain: the lease still holds nothing but
regular files, the artifact is still the size it was written at, and its digest
matches the one taken from the stream. A mismatch is permanent by
classification - retrying a deterministic corruption wastes an hour
(``docs/architecture/07-download-pipeline.md`` §7.7).

It is also the last cheap place to stop. After this the next thing that happens
is an upload, which on a domestic connection is the slow half.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mediahub.application.download.queue import JobStage
from mediahub.presentation.worker.stages.handler import PipelineStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.presentation.worker.stages.base import StageContext
    from mediahub.presentation.worker.stages.state import PipelineState


class VerifyStage(PipelineStage):
    """Checks the downloaded artifact against what was expected of it."""

    STAGE: ClassVar[JobStage] = JobStage.VERIFY

    __slots__ = ()

    async def advance(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Return the state with the artifact recorded as proved."""
        return await self._steps.ensure_verified(context, state)
