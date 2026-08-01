"""The ``DownloadJob`` aggregate root.

The job is a state machine over :class:`~mediahub.domain.download.enums.JobStatus`.
It owns the rules that stay true no matter which engine performs the transfer:
what may follow what, how many attempts are allowed, and when a failure is
still retryable.

Allowed transitions::

    QUEUED ──> RUNNING ──> SUCCEEDED (terminal)
       │  │        │
       │  │        ├──> FAILED ──> QUEUED (retry, budget permitting)
       │  │        │
       │  │        └──> QUEUED (released: shutdown, attempt refunded)
       │  │
       │  └────────┴──> CANCELLED (terminal)
       └──> FAILED

The two ways back to ``QUEUED`` are not the same and must not be merged.
:meth:`DownloadJob.requeue` follows a *failure* and keeps the attempt spent, so
a job that reliably breaks its worker eventually runs out of budget.
:meth:`DownloadJob.release` follows a *graceful interruption* - a deploy, a
drain - and refunds the attempt, because a nightly reboot must not slowly
exhaust the retry budget of healthy work
(``docs/architecture/08-state-machine.md`` §8.4).

Deliberately out of scope: sockets, protocols, retries in wall-clock time and
provider quirks. Those live behind
:class:`~mediahub.application.download.ports.DownloaderPort`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mediahub.domain.common.entity import AggregateRoot
from mediahub.domain.common.time import ensure_utc
from mediahub.domain.download.enums import JobPriority, JobStatus
from mediahub.domain.download.errors import (
    InvalidJobTransitionError,
    JobNotRetryableError,
)
from mediahub.domain.download.events import (
    DownloadCancelled,
    DownloadCompleted,
    DownloadFailed,
    DownloadRequested,
    DownloadStarted,
)
from mediahub.domain.download.value_objects import DownloadProgress, RetryPolicy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from datetime import datetime, timedelta

    from mediahub.domain.download.value_objects import JobId
    from mediahub.domain.media.value_objects import MediaId, SourceUrl

ALLOWED_TRANSITIONS: Final[Mapping[JobStatus, frozenset[JobStatus]]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED}
    ),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}
"""The complete lifecycle contract for download jobs."""

MAX_ERROR_LENGTH: Final[int] = 1000


class DownloadJob(AggregateRoot["JobId"]):
    """A unit of acquisition work for exactly one media item."""

    __slots__ = (
        "_attempts",
        "_created_at",
        "_finished_at",
        "_last_error",
        "_media_id",
        "_priority",
        "_progress",
        "_retry_policy",
        "_source_url",
        "_started_at",
        "_status",
        "_updated_at",
    )

    def __init__(  # noqa: PLR0913 - an aggregate's full state is rehydrated at once
        self,
        *,
        job_id: JobId,
        media_id: MediaId,
        source_url: SourceUrl,
        status: JobStatus,
        priority: JobPriority,
        retry_policy: RetryPolicy,
        progress: DownloadProgress,
        attempts: int,
        created_at: datetime,
        updated_at: datetime,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        last_error: str | None = None,
    ) -> None:
        """Rehydrate a job from stored state.

        Repositories use this constructor; application code should call
        :meth:`request`, which applies the rules for a brand-new job.
        """
        super().__init__(job_id)
        self._media_id = media_id
        self._source_url = source_url
        self._status = status
        self._priority = priority
        self._retry_policy = retry_policy
        self._progress = progress
        self._attempts = attempts
        self._created_at = ensure_utc(created_at, field_name="created_at")
        self._updated_at = ensure_utc(updated_at, field_name="updated_at")
        self._started_at = (
            None if started_at is None else ensure_utc(started_at, field_name="started_at")
        )
        self._finished_at = (
            None if finished_at is None else ensure_utc(finished_at, field_name="finished_at")
        )
        self._last_error = last_error

    # -- Construction -------------------------------------------------------

    @classmethod
    def request(
        cls,
        *,
        job_id: JobId,
        media_id: MediaId,
        source_url: SourceUrl,
        now: datetime,
        priority: JobPriority = JobPriority.NORMAL,
        retry_policy: RetryPolicy | None = None,
    ) -> DownloadJob:
        """Create a job in :attr:`JobStatus.QUEUED`.

        Args:
            job_id: Identifier produced by the application layer.
            media_id: The media item this job will populate.
            source_url: Where the bytes should come from.
            now: Current time, supplied by the caller's clock.
            priority: Scheduling priority.
            retry_policy: Attempt budget; the default policy when omitted.

        Returns:
            A queued job carrying a
            :class:`~mediahub.domain.download.events.DownloadRequested` event.
        """
        stamped = ensure_utc(now, field_name="now")
        policy = retry_policy or RetryPolicy.default()
        job = cls(
            job_id=job_id,
            media_id=media_id,
            source_url=source_url,
            status=JobStatus.QUEUED,
            priority=priority,
            retry_policy=policy,
            progress=DownloadProgress.none(),
            attempts=0,
            created_at=stamped,
            updated_at=stamped,
        )
        job.record_event(
            DownloadRequested(
                occurred_at=stamped,
                job_id=job_id.value,
                media_id=media_id.value,
                source_url=str(source_url),
                priority=priority.value,
            )
        )
        return job

    # -- State ---------------------------------------------------------------

    @property
    def media_id(self) -> MediaId:
        """Return the media item this job populates."""
        return self._media_id

    @property
    def source_url(self) -> SourceUrl:
        """Return the origin the bytes are fetched from."""
        return self._source_url

    @property
    def status(self) -> JobStatus:
        """Return the current lifecycle state."""
        return self._status

    @property
    def priority(self) -> JobPriority:
        """Return the scheduling priority."""
        return self._priority

    @property
    def retry_policy(self) -> RetryPolicy:
        """Return the attempt budget frozen at request time."""
        return self._retry_policy

    @property
    def progress(self) -> DownloadProgress:
        """Return the most recently reported progress."""
        return self._progress

    @property
    def attempts(self) -> int:
        """Return how many attempts have been started."""
        return self._attempts

    @property
    def last_error(self) -> str | None:
        """Return why the job last failed, if it did."""
        return self._last_error

    @property
    def created_at(self) -> datetime:
        """Return when the job was requested (UTC)."""
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        """Return when the job last changed (UTC)."""
        return self._updated_at

    @property
    def started_at(self) -> datetime | None:
        """Return when the job first started running (UTC)."""
        return self._started_at

    @property
    def finished_at(self) -> datetime | None:
        """Return when the job reached an end state (UTC)."""
        return self._finished_at

    @property
    def is_terminal(self) -> bool:
        """Return whether the job can never change again."""
        return self._status.is_terminal

    @property
    def can_retry(self) -> bool:
        """Return whether a failed job still has attempts left."""
        return self._status is JobStatus.FAILED and self._attempts < self._retry_policy.max_attempts

    @property
    def duration(self) -> timedelta | None:
        """Return how long the job ran, once it has both timestamps."""
        if self._started_at is None or self._finished_at is None:
            return None
        return self._finished_at - self._started_at

    # -- Behaviour -----------------------------------------------------------

    def start(self, *, now: datetime) -> None:
        """Mark the job as picked up by a worker and consume one attempt.

        Args:
            now: Current time.

        Raises:
            InvalidJobTransitionError: If the job is not queued.
        """
        self._transition_to(JobStatus.RUNNING, now=now)
        self._attempts += 1
        self._last_error = None
        if self._started_at is None:
            self._started_at = self._updated_at
        self.record_event(
            DownloadStarted(
                occurred_at=self._updated_at,
                job_id=self.id.value,
                media_id=self._media_id.value,
                attempt=self._attempts,
            )
        )

    def report_progress(self, progress: DownloadProgress, *, now: datetime) -> None:
        """Record transfer progress. Emits no event by design.

        Args:
            progress: The newly observed progress.
            now: Current time.

        Raises:
            InvalidJobTransitionError: If the job is not running.
        """
        if self._status is not JobStatus.RUNNING:
            raise InvalidJobTransitionError(self._status, JobStatus.RUNNING)
        self._progress = progress
        self._touch(now)

    def complete(self, *, now: datetime, progress: DownloadProgress | None = None) -> None:
        """Mark the job as finished successfully.

        Args:
            now: Current time.
            progress: Final progress; the last reported value when omitted.

        Raises:
            InvalidJobTransitionError: If the job is not running.
        """
        self._transition_to(JobStatus.SUCCEEDED, now=now)
        if progress is not None:
            self._progress = progress
        self._finished_at = self._updated_at
        self._last_error = None
        self.record_event(
            DownloadCompleted(
                occurred_at=self._updated_at,
                job_id=self.id.value,
                media_id=self._media_id.value,
                downloaded_bytes=self._progress.downloaded_bytes,
            )
        )

    def fail(self, *, reason: str, now: datetime) -> None:
        """Mark the job as finished unsuccessfully.

        Args:
            reason: Operator-facing explanation, truncated to a sane length.
            now: Current time.

        Raises:
            InvalidJobTransitionError: If the job already reached an end state.
        """
        self._transition_to(JobStatus.FAILED, now=now)
        self._last_error = reason.strip()[:MAX_ERROR_LENGTH] or "unknown error"
        self._finished_at = self._updated_at
        self.record_event(
            DownloadFailed(
                occurred_at=self._updated_at,
                job_id=self.id.value,
                media_id=self._media_id.value,
                reason=self._last_error,
                attempt=self._attempts,
                retryable=self.can_retry,
            )
        )

    def cancel(self, *, now: datetime) -> None:
        """Stop the job on request. This is terminal.

        Args:
            now: Current time.

        Raises:
            InvalidJobTransitionError: If the job already reached an end state.
        """
        self._transition_to(JobStatus.CANCELLED, now=now)
        self._finished_at = self._updated_at
        self.record_event(
            DownloadCancelled(
                occurred_at=self._updated_at,
                job_id=self.id.value,
                media_id=self._media_id.value,
            )
        )

    def requeue(self, *, now: datetime) -> None:
        """Return a failed job to the queue for another attempt.

        The attempt that failed stays spent. That is the point of the counter:
        a job that reliably breaks its worker must run out of budget rather than
        retry forever.

        Args:
            now: Current time.

        Raises:
            JobNotRetryableError: If the retry budget is exhausted.
            InvalidJobTransitionError: If the job has not failed.
        """
        if self._status is not JobStatus.FAILED:
            raise InvalidJobTransitionError(self._status, JobStatus.QUEUED)
        if not self.can_retry:
            raise JobNotRetryableError(self._attempts, self._retry_policy.max_attempts)
        self._transition_to(JobStatus.QUEUED, now=now)
        self._finished_at = None
        self._progress = DownloadProgress.none()

    def release(self, *, now: datetime) -> None:
        """Hand a running job back to the queue without blaming it.

        Used when a worker is interrupted for reasons that are nothing to do
        with the work - a deploy, a drain, an operator restart. The attempt
        consumed at claim time is **refunded**, because charging one would mean
        a nightly reboot slowly exhausts the retry budget of perfectly healthy
        jobs (``docs/architecture/08-state-machine.md`` §8.4).

        Progress is deliberately kept: the bytes already fetched are still on
        disk under a checkpoint, and the next attempt resumes from there rather
        than starting again.

        Args:
            now: Current time.

        Raises:
            InvalidJobTransitionError: If the job is not running.
        """
        if self._status is not JobStatus.RUNNING:
            raise InvalidJobTransitionError(self._status, JobStatus.QUEUED)
        self._transition_to(JobStatus.QUEUED, now=now)
        self._attempts = max(0, self._attempts - 1)
        self._finished_at = None
        self._last_error = None

    # -- Internals -----------------------------------------------------------

    def _transition_to(self, target: JobStatus, *, now: datetime) -> None:
        """Move to ``target`` if the lifecycle contract allows it."""
        if target not in ALLOWED_TRANSITIONS[self._status]:
            raise InvalidJobTransitionError(self._status, target)
        self._status = target
        self._touch(now)

    def _touch(self, now: datetime) -> None:
        """Stamp the aggregate as modified at ``now``."""
        self._updated_at = ensure_utc(now, field_name="now")
