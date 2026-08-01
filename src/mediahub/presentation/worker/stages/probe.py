"""The probe stage: find out what is at the URL, without spending bandwidth.

First in the plan, and the cheapest thing that can refuse a job. What it
establishes - the canonical URL, the provider, the title, the size the source
claims and the rendition to take - is written into the checkpoint, so a job
reclaimed after this point never probes twice. Format identifiers do expire, but
re-probing on every attempt would spend a third-party call on every retry of a
download that failed for reasons of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mediahub.application.download.queue import JobStage
from mediahub.presentation.worker.stages.handler import PipelineStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.presentation.worker.stages.base import StageContext
    from mediahub.presentation.worker.stages.state import PipelineState


class ProbeStage(PipelineStage):
    """Describes the source and chooses what to fetch."""

    STAGE: ClassVar[JobStage] = JobStage.PROBE

    __slots__ = ()

    async def advance(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Return the state carrying the probe's answer."""
        return await self._steps.ensure_probed(context, state)
