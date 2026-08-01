"""The download stage: move the bytes into this attempt's lease.

The longest stage by far, and the one cancellation and progress exist for. It
owns no transfer logic of its own - that is the download engine's, behind
:class:`~mediahub.application.download.ports.DownloaderPort` - and no location
of its own either: the engine is handed the lease and writes inside it, or it
writes nowhere.

Re-running it is safe and cheap. If the lease already holds the artifact the
state describes, the stage returns without touching the network; if it holds a
*partial* file from an interrupted attempt, the engine is asked to continue it
rather than start again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mediahub.application.download.queue import JobStage
from mediahub.presentation.worker.stages.handler import PipelineStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.presentation.worker.stages.base import StageContext
    from mediahub.presentation.worker.stages.state import PipelineState


class DownloadStage(PipelineStage):
    """Fetches the chosen rendition into the workspace lease."""

    STAGE: ClassVar[JobStage] = JobStage.DOWNLOAD

    __slots__ = ()

    async def advance(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Return the state carrying the artifacts the engine produced."""
        return await self._steps.ensure_downloaded(context, state)
