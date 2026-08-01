"""Stage handlers, and the contract every one of them satisfies.

A stage is one resumable step of a job. The contract lives in
:mod:`mediahub.presentation.worker.stages.base`; concrete handlers - probe,
download, verify, deliver, cleanup - are modules beside it.

A handler is **thin by rule**. It translates one step into calls on application
use cases and ports, honours the cancellation token, reports progress, and
returns. A handler that starts deciding whether something should be retried, or
where a file should end up, has stopped being a handler and has become a rule in
the wrong layer.

The five handlers here share one collaborator,
:class:`~mediahub.presentation.worker.stages.acquisition.AcquisitionSteps`, and
one durable record,
:class:`~mediahub.presentation.worker.stages.state.PipelineState`, carried
between them in the checkpoint's resume token. :func:`acquisition_handlers`
builds the set in plan order; a composition root registers it and the runtime
refuses to start if anything is missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.presentation.worker.stages.acquisition import (
    AcquisitionPolicy,
    AcquisitionSteps,
)
from mediahub.presentation.worker.stages.base import (
    DEFAULT_STAGE_PLAN,
    ProgressSink,
    StageContext,
    StageHandler,
    StageOutcome,
    StageRegistry,
)
from mediahub.presentation.worker.stages.cleanup import CleanupStage
from mediahub.presentation.worker.stages.deliver import DeliveryStage
from mediahub.presentation.worker.stages.download import DownloadStage
from mediahub.presentation.worker.stages.handler import PipelineStage
from mediahub.presentation.worker.stages.probe import ProbeStage
from mediahub.presentation.worker.stages.state import ArtifactState, PipelineState, ReceiptState
from mediahub.presentation.worker.stages.verify import VerifyStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = [
    "DEFAULT_STAGE_PLAN",
    "AcquisitionPolicy",
    "AcquisitionSteps",
    "ArtifactState",
    "CleanupStage",
    "DeliveryStage",
    "DownloadStage",
    "PipelineStage",
    "PipelineState",
    "ProbeStage",
    "ProgressSink",
    "ReceiptState",
    "StageContext",
    "StageHandler",
    "StageOutcome",
    "StageRegistry",
    "VerifyStage",
    "acquisition_handlers",
]


def acquisition_handlers(steps: AcquisitionSteps) -> Sequence[StageHandler]:
    """Return one handler per stage of the acquisition plan, in plan order.

    Order is presentational only - the executor works from
    :data:`~mediahub.presentation.worker.stages.base.DEFAULT_STAGE_PLAN` and the
    registry is a mapping - but returning them in the order they run makes a
    misconfigured worker's log read the way the pipeline does.
    """
    return (
        ProbeStage(steps),
        DownloadStage(steps),
        VerifyStage(steps),
        DeliveryStage(steps),
        CleanupStage(steps),
    )
