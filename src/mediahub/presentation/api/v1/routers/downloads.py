"""``/api/v1/downloads`` - the acquisition queue.

Note the honest contract: ``POST /downloads`` answers ``202 Accepted``, not
``201 Created``. The job is durably queued, but nothing executes it yet - no
download engine is wired in (see
:mod:`mediahub.infrastructure.downloader`). Returning ``202`` says exactly
that: the request was accepted for processing, and processing has not happened.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from mediahub.application.download.dto import (
    CancelDownloadJobCommand,
    GetDownloadJobQuery,
    ListDownloadJobsQuery,
    RequestDownloadCommand,
)
from mediahub.domain.download.enums import JobPriority, JobStatus
from mediahub.presentation.api.dependencies import (
    CancelDownloadJobDep,
    GetDownloadJobDep,
    ListDownloadJobsDep,
    RequestDownloadDep,
)
from mediahub.presentation.api.v1.schemas.common import PageResponse, PaginationQuery
from mediahub.presentation.api.v1.schemas.downloads import (
    DownloadJobResponse,
    RequestDownloadRequest,
)

router = APIRouter(prefix="/downloads", tags=["downloads"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a download",
    response_description="The queued job.",
)
async def request_download(
    payload: RequestDownloadRequest,
    use_case: RequestDownloadDep,
) -> DownloadJobResponse:
    """Queue an acquisition for a catalogued item.

    Responses:
        202: The job was queued. It will not run until a download engine is
            configured.
        404: No media item exists with this identifier.
        409: A job for this item is already queued or running.
        422: ``max_attempts`` is outside the allowed range.
    """
    summary = await use_case.execute(
        RequestDownloadCommand(
            media_id=payload.media_id,
            priority=payload.priority,
            max_attempts=payload.max_attempts,
        )
    )
    return DownloadJobResponse.from_dto(summary)


@router.get(
    "",
    summary="List download jobs",
    response_description="A page of jobs, newest first.",
)
async def list_download_jobs(
    use_case: ListDownloadJobsDep,
    pagination: PaginationQuery,
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    priority: Annotated[JobPriority | None, Query()] = None,
    media_id: Annotated[UUID | None, Query()] = None,
) -> PageResponse[DownloadJobResponse]:
    """Browse the queue, optionally filtered by state, priority or item."""
    page = await use_case.execute(
        ListDownloadJobsQuery(
            status=job_status,
            priority=priority,
            media_id=media_id,
            page=pagination,
        )
    )
    return PageResponse.from_page(page, [DownloadJobResponse.from_dto(job) for job in page.items])


@router.get(
    "/{job_id}",
    summary="Get a download job",
    response_description="The requested job.",
)
async def get_download_job(job_id: UUID, use_case: GetDownloadJobDep) -> DownloadJobResponse:
    """Read a single job, including progress and the last failure reason.

    Responses:
        200: The job was found.
        404: No job exists with this identifier.
    """
    summary = await use_case.execute(GetDownloadJobQuery(job_id=job_id))
    return DownloadJobResponse.from_dto(summary)


@router.post(
    "/{job_id}/cancel",
    summary="Cancel a download job",
    response_description="The cancelled job.",
)
async def cancel_download_job(job_id: UUID, use_case: CancelDownloadJobDep) -> DownloadJobResponse:
    """Stop a queued or running job.

    Responses:
        200: The job was cancelled.
        404: No job exists with this identifier.
        409: The job already reached an end state.
    """
    summary = await use_case.execute(CancelDownloadJobCommand(job_id=job_id))
    return DownloadJobResponse.from_dto(summary)
