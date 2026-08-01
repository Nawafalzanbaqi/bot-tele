"""Explicit translation between ORM rows and domain aggregates.

Every conversion the SQL adapters need lives here, in one direction per
function. Keeping it in a single module means a schema change has exactly one
place to break, and that break is a type error rather than a subtle runtime
surprise.

Aggregates are rebuilt through their full constructor - never through
``__new__`` or attribute injection - so a row that violates a domain invariant
(a naive timestamp, an impossible byte count) fails loudly at load time instead
of propagating corrupt state.
"""

from __future__ import annotations

from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm
from mediahub.domain.download.entities import DownloadJob
from mediahub.domain.download.enums import JobPriority, JobStatus
from mediahub.domain.download.value_objects import DownloadProgress, JobId, RetryPolicy
from mediahub.domain.media.entities import MediaItem
from mediahub.domain.media.enums import MediaStatus, MediaType
from mediahub.domain.media.value_objects import (
    FileSize,
    MediaId,
    MediaTitle,
    SourceUrl,
    StorageKey,
)
from mediahub.infrastructure.persistence.sqlalchemy.models import (
    DownloadJobModel,
    MediaItemModel,
)


def media_to_model(item: MediaItem) -> MediaItemModel:
    """Build a row from a media aggregate."""
    return MediaItemModel(
        id=item.id.value,
        source_url=str(item.source_url),
        title=str(item.title),
        media_type=item.media_type.value,
        status=item.status.value,
        storage_key=None if item.storage_key is None else str(item.storage_key),
        size_bytes=None if item.size is None else item.size.bytes_,
        checksum_algorithm=None if item.checksum is None else item.checksum.algorithm.value,
        checksum_digest=None if item.checksum is None else item.checksum.digest,
        failure_reason=item.failure_reason,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def media_to_domain(row: MediaItemModel) -> MediaItem:
    """Rebuild a media aggregate from a row."""
    checksum: Fingerprint | None = None
    if row.checksum_algorithm is not None and row.checksum_digest is not None:
        checksum = Fingerprint(
            algorithm=HashAlgorithm(row.checksum_algorithm),
            digest=row.checksum_digest,
        )

    return MediaItem(
        media_id=MediaId(row.id),
        source_url=SourceUrl(row.source_url),
        title=MediaTitle(row.title),
        media_type=MediaType(row.media_type),
        status=MediaStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        storage_key=None if row.storage_key is None else StorageKey(row.storage_key),
        size=None if row.size_bytes is None else FileSize(row.size_bytes),
        checksum=checksum,
        failure_reason=row.failure_reason,
    )


def apply_media_to_model(item: MediaItem, row: MediaItemModel) -> None:
    """Copy an aggregate's current state onto an already-loaded row.

    Used by ``save`` so SQLAlchemy emits an ``UPDATE`` for a tracked row rather
    than a second ``INSERT``.
    """
    row.source_url = str(item.source_url)
    row.title = str(item.title)
    row.media_type = item.media_type.value
    row.status = item.status.value
    row.storage_key = None if item.storage_key is None else str(item.storage_key)
    row.size_bytes = None if item.size is None else item.size.bytes_
    row.checksum_algorithm = None if item.checksum is None else item.checksum.algorithm.value
    row.checksum_digest = None if item.checksum is None else item.checksum.digest
    row.failure_reason = item.failure_reason
    row.updated_at = item.updated_at


def job_to_model(job: DownloadJob) -> DownloadJobModel:
    """Build a row from a download job aggregate."""
    return DownloadJobModel(
        id=job.id.value,
        media_id=job.media_id.value,
        source_url=str(job.source_url),
        status=job.status.value,
        priority=job.priority.value,
        priority_weight=job.priority.weight,
        downloaded_bytes=job.progress.downloaded_bytes,
        total_bytes=job.progress.total_bytes,
        attempts=job.attempts,
        max_attempts=job.retry_policy.max_attempts,
        backoff_seconds=job.retry_policy.backoff_seconds,
        last_error=job.last_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def job_to_domain(row: DownloadJobModel) -> DownloadJob:
    """Rebuild a download job aggregate from a row."""
    return DownloadJob(
        job_id=JobId(row.id),
        media_id=MediaId(row.media_id),
        source_url=SourceUrl(row.source_url),
        status=JobStatus(row.status),
        priority=JobPriority(row.priority),
        retry_policy=RetryPolicy(
            max_attempts=row.max_attempts,
            backoff_seconds=row.backoff_seconds,
        ),
        progress=DownloadProgress(
            downloaded_bytes=row.downloaded_bytes,
            total_bytes=row.total_bytes,
        ),
        attempts=row.attempts,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        last_error=row.last_error,
    )


def apply_job_to_model(job: DownloadJob, row: DownloadJobModel) -> None:
    """Copy a job's current state onto an already-loaded row."""
    row.status = job.status.value
    row.priority = job.priority.value
    row.priority_weight = job.priority.weight
    row.downloaded_bytes = job.progress.downloaded_bytes
    row.total_bytes = job.progress.total_bytes
    row.attempts = job.attempts
    row.max_attempts = job.retry_policy.max_attempts
    row.backoff_seconds = job.retry_policy.backoff_seconds
    row.last_error = job.last_error
    row.updated_at = job.updated_at
    row.started_at = job.started_at
    row.finished_at = job.finished_at
