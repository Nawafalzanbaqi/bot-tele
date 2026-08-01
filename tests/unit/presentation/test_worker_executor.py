"""The executor runs what is left, records what finished, and classifies the rest.

Resuming is the property under test more than anything else here: a stage that
already completed must not run twice, because "idempotent" is a promise handlers
make and a promise the runtime must not need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.application.common.cancellation import CancellationSource, NullCancellation
from mediahub.application.download.errors import (
    DownloadCancelledError,
    LeaseLostError,
    ProviderError,
    UnsupportedProviderError,
)
from mediahub.application.download.queue import Checkpoint, JobStage, StageProgress
from mediahub.domain.download.enums import FailureKind
from mediahub.presentation.worker.executor import StageExecutor
from mediahub.presentation.worker.stages.base import (
    DEFAULT_STAGE_PLAN,
    StageOutcome,
    StageRegistry,
)
from tests.support.worker_fakes import WORKER, RecordingStageHandler, WorkerHarness

if TYPE_CHECKING:
    from pathlib import Path

    from mediahub.application.download.queue import ClaimedJob
    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.presentation.worker.stages.base import StageContext

pytestmark = pytest.mark.unit


@pytest.fixture
def harness(tmp_path: Path) -> WorkerHarness:
    return WorkerHarness.build(tmp_path)


async def claim_one(harness: WorkerHarness) -> ClaimedJob:
    await harness.enqueue()
    claimed = await harness.services.claim.execute(worker=WORKER)
    assert claimed is not None
    return claimed


class TestHappyPath:
    async def test_every_stage_runs_once_in_plan_order(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        seen: list[JobStage] = []
        for handler in harness.handlers:
            handler.on_execute = lambda context: seen.append(context.stage)

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.succeeded
        assert seen == list(DEFAULT_STAGE_PLAN)
        assert all(handler.calls == 1 for handler in harness.handlers)

    async def test_each_completed_stage_is_checkpointed(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        durable = harness.queue.checkpoint_of(claimed.job_id)
        assert result.claim.checkpoint.completed_stages == tuple(DEFAULT_STAGE_PLAN)
        assert durable.completed_stages == tuple(DEFAULT_STAGE_PLAN)

    async def test_a_resume_token_is_carried_into_the_checkpoint(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.DOWNLOAD).resume_token = "offset=4096"

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.claim.checkpoint.resume_token == "offset=4096"
        assert harness.queue.checkpoint_of(claimed.job_id).resume_token == "offset=4096"

    async def test_handlers_receive_the_attempt_and_the_lease_directory(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        captured: list[StageContext] = []
        harness.handler(JobStage.PROBE).on_execute = captured.append

        with harness.workspace.lease(label="job") as scope:
            await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )
            directory = scope.directory()

        assert captured[0].attempt == 1
        assert captured[0].job_id == claimed.job_id
        assert captured[0].workspace.directory() == directory

    async def test_a_handler_reports_progress_through_its_context(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        observed: list[StageProgress] = []
        harness.handler(JobStage.DOWNLOAD).on_execute = lambda context: context.observe(
            transferred_bytes=512, total_bytes=1024, speed_bps=128.0, eta_seconds=4.0
        )

        with harness.workspace.lease(label="job") as scope:
            await harness.executor().execute(
                claimed, workspace=scope, cancellation=NullCancellation(), report=observed.append
            )

        assert len(observed) == 1
        assert observed[0].stage is JobStage.DOWNLOAD
        assert observed[0].percentage == 50.0


class TestResuming:
    async def test_completed_stages_are_not_run_again(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        resumed = claimed.with_checkpoint(
            Checkpoint(completed_stages=(JobStage.PROBE, JobStage.DOWNLOAD))
        )

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                resumed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.succeeded
        assert harness.handler(JobStage.PROBE).calls == 0
        assert harness.handler(JobStage.DOWNLOAD).calls == 0
        assert harness.handler(JobStage.VERIFY).calls == 1

    async def test_a_fully_checkpointed_job_has_nothing_left_to_do(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        resumed = claimed.with_checkpoint(Checkpoint(completed_stages=tuple(DEFAULT_STAGE_PLAN)))

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                resumed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.succeeded
        assert all(handler.calls == 0 for handler in harness.handlers)

    async def test_a_failed_attempt_keeps_the_stages_that_did_finish(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.VERIFY).error = ProviderError("upstream 503")

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert not result.succeeded
        assert result.claim.checkpoint.completed_stages == (JobStage.PROBE, JobStage.DOWNLOAD)


class TestFailureClassification:
    async def test_an_engine_failure_keeps_its_classification(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.DOWNLOAD).error = ProviderError("upstream 503")

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.failure is not None
        assert result.failure.kind is FailureKind.TRANSIENT
        assert result.failure.code == "provider_error"
        assert result.failure.stage is JobStage.DOWNLOAD

    async def test_a_permanent_failure_stays_permanent(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.PROBE).error = UnsupportedProviderError("no extractor")

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.failure is not None
        assert result.failure.kind is FailureKind.PERMANENT

    async def test_an_unknown_exception_becomes_a_transient_failure(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.DELIVER).error = ZeroDivisionError("surprise")

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.failure is not None
        assert result.failure.kind is FailureKind.TRANSIENT
        assert result.failure.code == "unknown_failure"
        assert not result.lease_lost

    async def test_a_missing_handler_is_a_permanent_failure(
        self, harness: WorkerHarness, tmp_path: Path
    ) -> None:
        claimed = await claim_one(harness)
        executor = harness.executor(handlers=[harness.handler(JobStage.PROBE)])

        with harness.workspace.lease(label="job") as scope:
            result = await executor.execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.failure is not None
        assert result.failure.code == "stage_handler_missing"
        assert result.failure.kind is FailureKind.PERMANENT

    async def test_a_lost_lease_stops_everything_and_says_so(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.DOWNLOAD).error = LeaseLostError(claimed.job_id)

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.lease_lost
        assert harness.handler(JobStage.VERIFY).calls == 0

    async def test_a_checkpoint_that_cannot_be_written_stops_the_attempt(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        await harness.queue.release_owned_by(WORKER, now=harness.clock.now())

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.lease_lost
        assert harness.handler(JobStage.PROBE).calls == 1
        assert harness.handler(JobStage.DOWNLOAD).calls == 0


class TestCancellation:
    async def test_a_cancelled_job_stops_before_the_next_stage(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        harness.handler(JobStage.PROBE).on_execute = lambda _: cancellation.cancel()

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=cancellation.token,
                report=lambda _: None,
            )

        assert result.cancelled
        assert result.failure is not None
        assert result.failure.stage is JobStage.DOWNLOAD
        assert result.failure.kind is FailureKind.CANCELLED
        assert harness.handler(JobStage.DOWNLOAD).calls == 0

    async def test_a_stage_that_stops_on_the_token_is_a_cancellation(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.DOWNLOAD).error = DownloadCancelledError("token was set")

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.cancelled

    async def test_work_already_finished_is_not_undone(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        harness.handler(JobStage.CLEANUP).on_execute = lambda _: cancellation.cancel()

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=cancellation.token,
                report=lambda _: None,
            )

        assert result.succeeded, "a cancellation during the last stage does not undo it"

    async def test_a_job_cancelled_before_it_starts_runs_nothing(
        self, harness: WorkerHarness
    ) -> None:
        claimed = await claim_one(harness)
        cancellation = CancellationSource()
        cancellation.cancel()

        with harness.workspace.lease(label="job") as scope:
            result = await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=cancellation.token,
                report=lambda _: None,
            )

        assert result.cancelled
        assert all(handler.calls == 0 for handler in harness.handlers)


class TestTheRegistry:
    def test_a_registry_reports_what_it_can_run(self, harness: WorkerHarness) -> None:
        registry = harness.registry()

        assert registry.stages == frozenset(DEFAULT_STAGE_PLAN)
        assert registry.missing_for(DEFAULT_STAGE_PLAN) == ()

    def test_a_registry_names_the_stages_it_cannot_run(self, harness: WorkerHarness) -> None:
        registry = StageRegistry([harness.handler(JobStage.PROBE)])

        assert registry.missing_for(DEFAULT_STAGE_PLAN) == (
            JobStage.DOWNLOAD,
            JobStage.VERIFY,
            JobStage.DELIVER,
            JobStage.CLEANUP,
        )

    def test_a_later_registration_replaces_an_earlier_one(self, harness: WorkerHarness) -> None:
        replacement = RecordingStageHandler(stage=JobStage.PROBE)
        registry = StageRegistry([harness.handler(JobStage.PROBE), replacement])

        assert registry.for_stage(JobStage.PROBE) is replacement

    def test_an_empty_outcome_hands_nothing_on(self) -> None:
        assert StageOutcome.done().resume_token is None


class TestPlan:
    async def test_a_shorter_plan_runs_only_its_stages(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        executor = StageExecutor(
            handlers=StageRegistry(harness.handlers),
            checkpoint=harness.services.checkpoint,
            plan=(JobStage.DELIVER, JobStage.CLEANUP),
        )

        with harness.workspace.lease(label="job") as scope:
            result = await executor.execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )

        assert result.succeeded
        assert executor.plan == (JobStage.DELIVER, JobStage.CLEANUP)
        assert harness.handler(JobStage.PROBE).calls == 0
        assert harness.handler(JobStage.DELIVER).calls == 1


class TestWorkspaceScope:
    async def test_bytes_written_by_a_stage_live_in_the_lease(self, harness: WorkerHarness) -> None:
        claimed = await claim_one(harness)
        harness.handler(JobStage.DOWNLOAD).bytes_written = 64
        scope_used: list[WorkspaceScope] = []
        harness.handler(JobStage.CLEANUP).on_execute = lambda ctx: scope_used.append(ctx.workspace)

        with harness.workspace.lease(label="job") as scope:
            await harness.executor().execute(
                claimed,
                workspace=scope,
                cancellation=NullCancellation(),
                report=lambda _: None,
            )
            assert scope.used_bytes() == 64

        assert scope_used[0].used_bytes() == 0, "the lease is gone once the attempt ends"
