"""Everything the worker is allowed to call.

One object, assembled by the composition root, holding the use cases and the two
ports a worker legitimately needs. It exists to make the boundary *visible*: the
worker's entire vocabulary is on this page, and anything not here - a
repository, an engine, a delivery provider, a settings object - is by
construction out of reach.

That is the mechanical form of "the worker contains no business logic". It
cannot apply a rule it has no way to reach.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock
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
    from mediahub.application.workspace.ports import WorkspaceLeasing


@dataclass(frozen=True, slots=True)
class WorkerServices:
    """The use cases and ports one worker process drives.

    Attributes:
        claim: Take the next due job.
        heartbeat: Keep the lease and read the cancellation flag.
        checkpoint: Record that a stage finished.
        report_progress: Publish an observation.
        complete: Settle a job that succeeded.
        fail: Settle a job that did not, and let the rules decide about a retry.
        release: Hand a job back unharmed when the process is stopping.
        acknowledge_cancellation: Confirm that a job stopped on request.
        recover_leases: Take back work whose owner is gone.
        workspace: Where an attempt may write, and where it is deleted from.
        clock: The only source of time in the process.
    """

    claim: ClaimJob
    heartbeat: HeartbeatJob
    checkpoint: CheckpointJob
    report_progress: ReportJobProgress
    complete: CompleteJob
    fail: FailJob
    release: ReleaseJob
    acknowledge_cancellation: AcknowledgeCancellation
    recover_leases: RecoverLeases
    workspace: WorkspaceLeasing
    clock: Clock
