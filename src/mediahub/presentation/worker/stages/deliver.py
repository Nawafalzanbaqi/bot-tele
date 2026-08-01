"""The delivery stage: hand the artifact over, and take a receipt for it.

This is the durability point of the whole product. Before it, the only copy is
on a device with a small card; after it, the destination is the custodian and
the local bytes are disposable. Everything either side of this stage is arranged
around that one fact.

Two rules are enforced by the step this stage drives and are worth naming here
because they are the ones that would be quietly lost in a refactor:

* **A delivery that has succeeded is never repeated.** The receipt lives in the
  checkpoint, so a job reclaimed between the upload and the write that records
  it sends nothing a second time.
* **A receipt is taken only once the destination has confirmed**, never
  optimistically - it is what authorises deleting the local copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from mediahub.application.download.queue import JobStage
from mediahub.presentation.worker.stages.handler import PipelineStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.presentation.worker.stages.base import StageContext
    from mediahub.presentation.worker.stages.state import PipelineState


class DeliveryStage(PipelineStage):
    """Sends the artifact to its destination and records the proof."""

    STAGE: ClassVar[JobStage] = JobStage.DELIVER

    __slots__ = ()

    async def advance(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Return the state carrying durable proof of delivery."""
        return await self._steps.ensure_delivered(context, state)
