"""Leases, checkpoints, progress and failure reports hold their own rules.

These are the values the whole worker rests on: if a lease can be extended
backwards, or a checkpoint can claim a stage twice, every guarantee above them
is decoration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from mediahub.application.common.errors import ApplicationError
from mediahub.application.delivery.errors import DeliveryRateLimitedError
from mediahub.application.download.errors import (
    InvalidCheckpointError,
    InvalidLeaseError,
    InvalidWorkerIdentityError,
    LeaseLostError,
)
from mediahub.application.download.failures import (
    DISK_RETRY_SECONDS,
    UNKNOWN_FAILURE_CODE,
    FailureReport,
    classify,
)
from mediahub.application.download.queue import (
    Checkpoint,
    ClaimedJob,
    JobStage,
    Lease,
    StageProgress,
    WorkerId,
)
from mediahub.domain.common.errors import InvariantViolationError
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.download.errors import (
    DownloadJobNotFoundError,
    InvalidProgressError,
)
from mediahub.domain.download.value_objects import JobId
from mediahub.domain.workspace.errors import InsufficientDiskSpaceError
from mediahub.presentation.worker.stages.base import DEFAULT_STAGE_PLAN

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
JOB = JobId(UUID(int=7))
WORKER = WorkerId(host="pi", role="worker", index=0)


def make_lease(*, seconds: int = 120, owner: WorkerId = WORKER) -> Lease:
    return Lease(
        job_id=JOB, owner=owner, acquired_at=NOW, expires_at=NOW + timedelta(seconds=seconds)
    )


class TestWorkerIdentity:
    def test_canonical_form_is_host_role_index(self) -> None:
        assert str(WorkerId(host="pi", role="delivery", index=2)) == "pi:delivery:2"

    def test_identity_is_a_value(self) -> None:
        assert WorkerId(host="pi") == WorkerId(host="pi", role="worker", index=0)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"host": " "},
            {"host": "pi", "role": ""},
            {"host": "pi:1"},
            {"host": "pi", "role": "wor:ker"},
            {"host": "pi", "index": -1},
        ],
    )
    def test_ambiguous_identities_are_refused(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(InvalidWorkerIdentityError):
            WorkerId(**kwargs)  # type: ignore[arg-type]


class TestLease:
    def test_a_lease_must_have_life_in_it(self) -> None:
        with pytest.raises(InvalidLeaseError):
            Lease(job_id=JOB, owner=WORKER, acquired_at=NOW, expires_at=NOW)

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(InvariantViolationError):
            Lease(
                job_id=JOB,
                owner=WORKER,
                acquired_at=datetime(2026, 1, 1, 12, 0),  # noqa: DTZ001 - the point of the test
                expires_at=NOW + timedelta(seconds=1),
            )

    def test_expiry_is_read_against_a_supplied_clock(self) -> None:
        lease = make_lease(seconds=120)

        assert not lease.is_expired(NOW + timedelta(seconds=119))
        assert lease.is_expired(NOW + timedelta(seconds=120))

    def test_remaining_time_never_goes_negative(self) -> None:
        lease = make_lease(seconds=60)

        assert lease.remaining_seconds(NOW + timedelta(seconds=10)) == 50
        assert lease.remaining_seconds(NOW + timedelta(seconds=600)) == 0

    def test_ownership_is_by_identity(self) -> None:
        lease = make_lease()

        assert lease.is_held_by(WorkerId(host="pi"))
        assert not lease.is_held_by(WorkerId(host="pi", index=1))

    def test_extension_moves_the_expiry_forward(self) -> None:
        lease = make_lease(seconds=60)
        extended = lease.extended_to(NOW + timedelta(seconds=120))

        assert extended.expires_at == NOW + timedelta(seconds=120)
        assert extended.acquired_at == lease.acquired_at

    def test_extension_may_not_move_backwards(self) -> None:
        lease = make_lease(seconds=120)

        with pytest.raises(InvalidLeaseError):
            lease.extended_to(NOW + timedelta(seconds=60))

    def test_renewing_to_the_same_instant_is_a_no_op(self) -> None:
        lease = make_lease(seconds=120)

        assert lease.extended_to(lease.expires_at) == lease


class TestCheckpoint:
    def test_a_fresh_checkpoint_has_finished_nothing(self) -> None:
        checkpoint = Checkpoint.empty()

        assert checkpoint.is_fresh
        assert checkpoint.last_completed is None
        assert checkpoint.remaining(DEFAULT_STAGE_PLAN) == tuple(DEFAULT_STAGE_PLAN)

    def test_completing_a_stage_removes_it_from_what_is_left(self) -> None:
        checkpoint = Checkpoint.empty().with_stage(JobStage.PROBE, at=NOW)

        assert checkpoint.has_completed(JobStage.PROBE)
        assert checkpoint.last_completed is JobStage.PROBE
        assert JobStage.PROBE not in checkpoint.remaining(DEFAULT_STAGE_PLAN)

    def test_resuming_keeps_plan_order(self) -> None:
        checkpoint = Checkpoint(completed_stages=(JobStage.DOWNLOAD, JobStage.PROBE))

        assert checkpoint.remaining(DEFAULT_STAGE_PLAN) == (
            JobStage.VERIFY,
            JobStage.DELIVER,
            JobStage.CLEANUP,
        )

    def test_completing_the_same_stage_twice_is_harmless(self) -> None:
        once = Checkpoint.empty().with_stage(JobStage.PROBE, at=NOW)
        twice = once.with_stage(JobStage.PROBE, at=NOW + timedelta(seconds=1))

        assert twice.completed_stages == (JobStage.PROBE,)
        assert twice.updated_at == NOW + timedelta(seconds=1)

    def test_a_duplicated_stage_cannot_be_constructed(self) -> None:
        with pytest.raises(InvalidCheckpointError):
            Checkpoint(completed_stages=(JobStage.PROBE, JobStage.PROBE))

    def test_a_resume_token_survives_later_stages(self) -> None:
        checkpoint = Checkpoint.empty().with_stage(
            JobStage.DOWNLOAD, at=NOW, resume_token="offset=512"
        )
        later = checkpoint.with_stage(JobStage.VERIFY, at=NOW)

        assert later.resume_token == "offset=512"

    def test_a_resume_token_can_be_replaced_without_completing_a_stage(self) -> None:
        checkpoint = Checkpoint.empty().with_resume_token("offset=1024", at=NOW)

        assert checkpoint.resume_token == "offset=1024"
        assert checkpoint.is_fresh

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(InvariantViolationError):
            Checkpoint(updated_at=datetime(2026, 1, 1))  # noqa: DTZ001 - the point of the test


class TestStageProgress:
    def test_percentage_needs_a_total(self) -> None:
        assert StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=10).percentage is None

    def test_percentage_is_clamped_when_a_source_under_declares(self) -> None:
        progress = StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=150, total_bytes=100)

        assert progress.percentage == 100.0

    def test_progress_reports_speed_and_eta(self) -> None:
        progress = StageProgress(
            stage=JobStage.DOWNLOAD,
            transferred_bytes=50,
            total_bytes=200,
            speed_bps=1024.0,
            eta_seconds=12.5,
            observed_at=NOW,
        )

        assert progress.percentage == 25.0
        assert progress.speed_bps == 1024.0
        assert progress.eta_seconds == 12.5
        assert progress.observed_at == NOW

    def test_a_stage_starts_at_zero(self) -> None:
        assert StageProgress.starting(JobStage.VERIFY).transferred_bytes == 0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"transferred_bytes": -1},
            {"total_bytes": -1},
            {"speed_bps": -1.0},
            {"eta_seconds": -1.0},
        ],
    )
    def test_impossible_observations_are_refused(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(InvalidProgressError):
            StageProgress(stage=JobStage.DOWNLOAD, **kwargs)  # type: ignore[arg-type]


class TestClaimedJob:
    def test_a_claim_exposes_the_job_and_its_owner(self) -> None:
        claim = ClaimedJob(lease=make_lease())

        assert claim.job_id == JOB
        assert claim.owner == WORKER
        assert claim.attempt == 0

    def test_a_claim_is_updated_by_replacement(self) -> None:
        claim = ClaimedJob(lease=make_lease())
        renewed = make_lease(seconds=240)
        checkpoint = Checkpoint.empty().with_stage(JobStage.PROBE, at=NOW)

        updated = claim.with_attempt(2).with_lease(renewed).with_checkpoint(checkpoint)

        assert updated.attempt == 2
        assert updated.lease is renewed
        assert updated.checkpoint is checkpoint
        assert claim.attempt == 0


class TestFailureReports:
    def test_messages_are_normalised_and_bounded(self) -> None:
        report = FailureReport(
            kind=FailureKind.TRANSIENT, code="x", message="  a\n  very\tlong  " + "y" * 900
        )

        assert report.message.startswith("a very long ")
        assert len(report.message) <= 500

    def test_an_empty_message_still_says_something(self) -> None:
        assert FailureReport(kind=FailureKind.TRANSIENT, code="x", message="   ").message

    def test_retryability_comes_from_the_kind(self) -> None:
        transient = FailureReport(kind=FailureKind.TRANSIENT, code="x", message="m")
        permanent = FailureReport(kind=FailureKind.PERMANENT, code="x", message="m")

        assert transient.is_retryable
        assert not permanent.is_retryable

    def test_a_cancellation_is_recognised_as_one(self) -> None:
        report = FailureReport(kind=FailureKind.CANCELLED, code="cancelled", message="m")

        assert report.is_cancellation
        assert not report.is_retryable

    def test_a_report_can_be_attributed_to_a_stage(self) -> None:
        report = FailureReport(kind=FailureKind.TRANSIENT, code="x", message="m")

        assert report.at_stage(JobStage.DELIVER).stage is JobStage.DELIVER


class TestClassification:
    def test_an_engine_error_classifies_itself(self) -> None:
        report = classify(LeaseLostError(JOB), stage=JobStage.DOWNLOAD)

        assert report.kind is FailureKind.TRANSIENT
        assert report.code == "lease_lost"
        assert report.stage is JobStage.DOWNLOAD

    def test_a_provider_delay_is_carried_through(self) -> None:
        error = DeliveryRateLimitedError("slow down", retry_after_seconds=42.0)

        assert classify(error).retry_after_seconds == 42.0

    def test_a_broken_rule_is_permanent(self) -> None:
        report = classify(DownloadJobNotFoundError(JOB))

        assert report.kind is FailureKind.PERMANENT
        assert report.code == "download_job_not_found"

    def test_an_orchestration_failure_is_permanent(self) -> None:
        report = classify(ApplicationError("nothing is wired"))

        assert report.kind is FailureKind.PERMANENT
        assert report.code == "application_error"

    def test_a_full_disk_is_transient_with_a_long_flat_delay(self) -> None:
        report = classify(InsufficientDiskSpaceError(1_000, 10))

        assert report.kind is FailureKind.TRANSIENT
        assert report.code == "insufficient_disk_space"
        assert report.retry_after_seconds == DISK_RETRY_SECONDS

    def test_an_unknown_failure_is_transient_and_says_so(self) -> None:
        report = classify(RuntimeError("who knows"))

        assert report.kind is FailureKind.TRANSIENT
        assert report.code == UNKNOWN_FAILURE_CODE
        assert "RuntimeError" in report.message
