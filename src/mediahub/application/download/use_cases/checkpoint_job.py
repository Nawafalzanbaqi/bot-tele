"""Use case: record that a stage finished.

The checkpoint is what makes a crash cheap. Without it a power cut at 95% of a
delivery costs the whole job; with it, the next worker resumes at the stage
after the last one recorded.

Two properties are worth stating plainly:

* **The write happens after the stage, never before.** A checkpoint claims a
  stage is *done*; writing it optimistically would make a reclaimed job skip
  work that never happened, which is worse than repeating it.
* **Re-completing a stage is not an error.** A job reclaimed mid-stage re-runs
  that stage, and stages are required to be idempotent, so recording the same
  completion twice must be harmless.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock
    from mediahub.application.download.queue import (
        Checkpoint,
        ClaimedJob,
        JobQueue,
        JobStage,
    )


class CheckpointJob:
    """Persist the completion of one stage of a job."""

    def __init__(self, *, queue: JobQueue, clock: Clock) -> None:
        """Wire the use case to its ports."""
        self._queue = queue
        self._clock = clock

    async def execute(
        self,
        claimed: ClaimedJob,
        stage: JobStage,
        *,
        resume_token: str | None = None,
    ) -> Checkpoint:
        """Record ``stage`` as complete and return the new checkpoint.

        Args:
            claimed: The claim held by the caller, carrying the current
                checkpoint.
            stage: The stage that just finished.
            resume_token: Opaque state the next attempt may need. Never
                interpreted - only the stage that wrote it knows what it means.

        Returns:
            The checkpoint as it now stands durably.

        Raises:
            LeaseLostError: If the caller no longer owns the job.
        """
        checkpoint = claimed.checkpoint.with_stage(
            stage, at=self._clock.now(), resume_token=resume_token
        )
        await self._queue.save_checkpoint(claimed.lease, checkpoint)
        logger.bind(
            job_id=str(claimed.job_id),
            stage=stage.value,
            completed=len(checkpoint.completed_stages),
        ).debug("Checkpointed stage")
        return checkpoint
