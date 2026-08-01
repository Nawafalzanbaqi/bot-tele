"""The download aggregate enforces its lifecycle and retry budget."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from mediahub.domain.download.entities import DownloadJob
from mediahub.domain.download.enums import JobPriority, JobStatus
from mediahub.domain.download.errors import (
    InvalidJobTransitionError,
    InvalidProgressError,
    InvalidRetryPolicyError,
    JobNotRetryableError,
)
from mediahub.domain.download.value_objects import DownloadProgress, JobId, RetryPolicy
from mediahub.domain.media.value_objects import MediaId, SourceUrl

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)


def make_job(*, max_attempts: int = 3) -> DownloadJob:
    """Build a freshly queued job."""
    return DownloadJob.request(
        job_id=JobId(UUID(int=1)),
        media_id=MediaId(UUID(int=2)),
        source_url=SourceUrl("https://example.com/a.mp4"),
        priority=JobPriority.HIGH,
        retry_policy=RetryPolicy(max_attempts=max_attempts),
        now=NOW,
    )


class TestRequest:
    def test_starts_queued_with_no_attempts(self) -> None:
        job = make_job()

        assert job.status is JobStatus.QUEUED
        assert job.attempts == 0
        assert job.progress.downloaded_bytes == 0
        assert [event.name for event in job.events] == ["DownloadRequested"]

    def test_priority_orders_by_weight(self) -> None:
        assert JobPriority.HIGH.weight > JobPriority.NORMAL.weight > JobPriority.LOW.weight


class TestExecutionLifecycle:
    def test_start_consumes_an_attempt(self) -> None:
        job = make_job()
        job.start(now=LATER)

        assert job.status is JobStatus.RUNNING
        assert job.attempts == 1
        assert job.started_at == LATER

    def test_progress_requires_a_running_job(self) -> None:
        job = make_job()

        with pytest.raises(InvalidJobTransitionError):
            job.report_progress(DownloadProgress(downloaded_bytes=1), now=LATER)

    def test_progress_is_recorded_without_an_event(self) -> None:
        job = make_job()
        job.start(now=NOW)
        job.pull_events()
        job.report_progress(DownloadProgress(downloaded_bytes=50, total_bytes=200), now=LATER)

        assert job.progress.percentage == 25.0
        assert job.events == ()

    def test_completion_is_terminal(self) -> None:
        job = make_job()
        job.start(now=NOW)
        job.complete(now=LATER, progress=DownloadProgress(downloaded_bytes=10, total_bytes=10))

        assert job.status is JobStatus.SUCCEEDED
        assert job.is_terminal
        assert job.finished_at == LATER
        assert job.duration is not None

        with pytest.raises(InvalidJobTransitionError):
            job.cancel(now=LATER)

    def test_cancellation_is_terminal(self) -> None:
        job = make_job()
        job.cancel(now=LATER)

        assert job.status is JobStatus.CANCELLED
        with pytest.raises(InvalidJobTransitionError):
            job.start(now=LATER)


class TestRetry:
    def test_failure_within_budget_is_retryable(self) -> None:
        job = make_job(max_attempts=2)
        job.start(now=NOW)
        job.fail(reason="connection reset", now=LATER)

        assert job.status is JobStatus.FAILED
        assert job.can_retry
        assert job.last_error == "connection reset"

    def test_requeue_returns_a_failed_job_to_the_queue(self) -> None:
        job = make_job(max_attempts=2)
        job.start(now=NOW)
        job.report_progress(DownloadProgress(downloaded_bytes=5), now=NOW)
        job.fail(reason="connection reset", now=LATER)

        job.requeue(now=LATER)

        assert job.status is JobStatus.QUEUED
        assert job.progress.downloaded_bytes == 0
        assert job.finished_at is None

    def test_exhausted_budget_cannot_be_requeued(self) -> None:
        job = make_job(max_attempts=1)
        job.start(now=NOW)
        job.fail(reason="gone", now=LATER)

        assert not job.can_retry
        with pytest.raises(JobNotRetryableError):
            job.requeue(now=LATER)

    def test_failure_event_reports_retryability(self) -> None:
        job = make_job(max_attempts=1)
        job.start(now=NOW)
        job.pull_events()
        job.fail(reason="gone", now=LATER)

        (event,) = job.events
        assert event.name == "DownloadFailed"


class TestRelease:
    """A graceful interruption is not the job's fault, and is not charged."""

    def test_release_returns_a_running_job_to_the_queue(self) -> None:
        job = make_job()
        job.start(now=NOW)

        job.release(now=LATER)

        assert job.status is JobStatus.QUEUED
        assert job.finished_at is None
        assert job.updated_at == LATER

    def test_release_refunds_the_attempt_it_was_charged(self) -> None:
        job = make_job(max_attempts=2)
        job.start(now=NOW)
        assert job.attempts == 1

        job.release(now=LATER)

        assert job.attempts == 0

    def test_repeated_restarts_never_exhaust_the_budget(self) -> None:
        job = make_job(max_attempts=1)

        for _ in range(5):
            job.start(now=NOW)
            job.release(now=LATER)

        assert job.attempts == 0
        assert job.status is JobStatus.QUEUED

    def test_release_keeps_the_progress_a_checkpoint_will_resume_from(self) -> None:
        job = make_job()
        job.start(now=NOW)
        job.report_progress(DownloadProgress(downloaded_bytes=512), now=NOW)

        job.release(now=LATER)

        assert job.progress.downloaded_bytes == 512, "unlike a retry, nothing was lost"

    def test_release_clears_any_earlier_error(self) -> None:
        job = make_job(max_attempts=3)
        job.start(now=NOW)
        job.fail(reason="connection reset", now=NOW)
        job.requeue(now=NOW)
        job.start(now=NOW)

        job.release(now=LATER)

        assert job.last_error is None

    def test_release_records_no_event(self) -> None:
        job = make_job()
        job.start(now=NOW)
        job.pull_events()

        job.release(now=LATER)

        assert job.events == ()

    @pytest.mark.parametrize("settle", ["complete", "cancel", "leave_queued"])
    def test_only_a_running_job_can_be_released(self, settle: str) -> None:
        job = make_job()
        if settle != "leave_queued":
            job.start(now=NOW)
            getattr(job, settle)(now=NOW)

        with pytest.raises(InvalidJobTransitionError):
            job.release(now=LATER)

    def test_a_running_job_cannot_be_requeued_directly(self) -> None:
        job = make_job()
        job.start(now=NOW)

        with pytest.raises(InvalidJobTransitionError):
            job.requeue(now=LATER)


class TestValueObjects:
    def test_progress_cannot_exceed_total(self) -> None:
        with pytest.raises(InvalidProgressError):
            DownloadProgress(downloaded_bytes=11, total_bytes=10)

    def test_progress_percentage_is_none_without_total(self) -> None:
        assert DownloadProgress(downloaded_bytes=5).percentage is None

    def test_retry_policy_bounds_are_enforced(self) -> None:
        with pytest.raises(InvalidRetryPolicyError):
            RetryPolicy(max_attempts=0)
        with pytest.raises(InvalidRetryPolicyError):
            RetryPolicy(max_attempts=RetryPolicy.MAX_ALLOWED_ATTEMPTS + 1)

    def test_backoff_grows_exponentially(self) -> None:
        policy = RetryPolicy(max_attempts=5, backoff_seconds=10)

        assert policy.delay_for(1) == 0
        assert policy.delay_for(2) == 10
        assert policy.delay_for(3) == 20
        assert policy.delay_for(4) == 40
