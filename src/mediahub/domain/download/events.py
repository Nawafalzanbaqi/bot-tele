"""Facts published by the download aggregate.

Progress updates deliberately emit *no* event: they occur thousands of times
per transfer and would drown any consumer. Progress is observable through the
job's state instead; only lifecycle changes are announced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mediahub.domain.common.events import DomainEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadRequested(DomainEvent):
    """A download job was accepted into the queue.

    Attributes:
        job_id: Identifier of the job.
        media_id: The media item the job will populate.
        source_url: Where the bytes should come from.
        priority: Scheduling priority assigned at request time.
    """

    job_id: UUID
    media_id: UUID
    source_url: str
    priority: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadStarted(DomainEvent):
    """A worker picked the job up.

    Attributes:
        job_id: Identifier of the job.
        media_id: The media item being populated.
        attempt: 1-based number of the attempt that just began.
    """

    job_id: UUID
    media_id: UUID
    attempt: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadCompleted(DomainEvent):
    """The job finished successfully.

    Attributes:
        job_id: Identifier of the job.
        media_id: The media item that was populated.
        downloaded_bytes: Total bytes transferred.
    """

    job_id: UUID
    media_id: UUID
    downloaded_bytes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadFailed(DomainEvent):
    """The job finished unsuccessfully.

    Attributes:
        job_id: Identifier of the job.
        media_id: The media item that was not populated.
        reason: Operator-facing explanation.
        attempt: The attempt number that failed.
        retryable: Whether attempts remain under the job's retry policy.
    """

    job_id: UUID
    media_id: UUID
    reason: str
    attempt: int
    retryable: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadCancelled(DomainEvent):
    """The job was cancelled on request.

    Attributes:
        job_id: Identifier of the job.
        media_id: The media item the job would have populated.
    """

    job_id: UUID
    media_id: UUID
