"""Request and response schemas for the download job resource."""

from __future__ import annotations

# `datetime` and `UUID` are imported at runtime, not under TYPE_CHECKING: Pydantic
# resolves field annotations when it builds the model.
from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mediahub.domain.download.enums import JobPriority, JobStatus
from mediahub.domain.download.value_objects import RetryPolicy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.dto import DownloadJobSummary


class RequestDownloadRequest(BaseModel):
    """Body of ``POST /api/v1/downloads``.

    Attributes:
        media_id: The catalogued item whose bytes should be fetched.
        priority: Scheduling priority for the new job.
        max_attempts: Optional override of the default retry budget.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "media_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
                    "priority": "normal",
                    "max_attempts": 3,
                }
            ]
        },
    )

    media_id: UUID
    priority: JobPriority = JobPriority.NORMAL
    max_attempts: Annotated[
        int | None,
        Field(default=None, ge=1, le=RetryPolicy.MAX_ALLOWED_ATTEMPTS),
    ]


class DownloadJobResponse(BaseModel):
    """A download job as returned by the API.

    Attributes:
        id: Identifier of the job.
        media_id: The media item the job populates.
        source_url: Origin the bytes are fetched from.
        status: Current lifecycle state.
        priority: Scheduling priority.
        downloaded_bytes: Bytes transferred so far.
        total_bytes: Announced total, when known.
        percentage: Completion in percent, when the total is known.
        attempts: Attempts consumed so far.
        max_attempts: Attempt budget from the job's retry policy.
        last_error: Why the job last failed, if it did.
        created_at: When the job was requested (UTC).
        updated_at: When the job last changed (UTC).
        started_at: When the job first started running (UTC).
        finished_at: When the job reached an end state (UTC).
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    media_id: UUID
    source_url: str
    status: JobStatus
    priority: JobPriority
    downloaded_bytes: int
    total_bytes: int | None
    percentage: float | None
    attempts: int
    max_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_dto(cls, summary: DownloadJobSummary) -> DownloadJobResponse:
        """Build the response from an application DTO."""
        return cls(
            id=summary.job_id,
            media_id=summary.media_id,
            source_url=summary.source_url,
            status=summary.status,
            priority=summary.priority,
            downloaded_bytes=summary.downloaded_bytes,
            total_bytes=summary.total_bytes,
            percentage=summary.percentage,
            attempts=summary.attempts,
            max_attempts=summary.max_attempts,
            last_error=summary.last_error,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
        )
