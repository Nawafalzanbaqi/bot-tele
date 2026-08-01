"""Persistence port for the download aggregate.

Mirrors :mod:`mediahub.domain.media.repository`: the interface belongs to the
domain, the implementations to the infrastructure layer.

``claim_next_queued`` is intentionally declared here even though no worker
exists yet. It is the seam a future scheduler will use to take work off the
queue atomically; putting it in the contract now means the SQL adapter can
implement it with ``SELECT ... FOR UPDATE SKIP LOCKED`` without any change to
the core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.domain.common.pagination import Page, PageRequest
    from mediahub.domain.download.entities import DownloadJob
    from mediahub.domain.download.enums import JobPriority, JobStatus
    from mediahub.domain.download.value_objects import JobId
    from mediahub.domain.media.value_objects import MediaId


@dataclass(frozen=True, slots=True)
class DownloadJobFilter:
    """Criteria for narrowing a job listing.

    Every field is optional; ``None`` means "do not filter on this". Fields are
    combined with AND.

    Attributes:
        status: Restrict to a single lifecycle state.
        priority: Restrict to a single scheduling priority.
        media_id: Restrict to the jobs of one media item.
    """

    status: JobStatus | None = None
    priority: JobPriority | None = None
    media_id: MediaId | None = None


class DownloadJobRepository(Protocol):
    """Collection-like access to :class:`~mediahub.domain.download.entities.DownloadJob`."""

    async def add(self, job: DownloadJob) -> None:
        """Stage a newly requested job for insertion."""
        ...

    async def save(self, job: DownloadJob) -> None:
        """Stage the current state of an already-known job."""
        ...

    async def get(self, job_id: JobId) -> DownloadJob | None:
        """Return the job with this identifier, or ``None``."""
        ...

    async def find_active_for_media(self, media_id: MediaId) -> DownloadJob | None:
        """Return the queued or running job for a media item, or ``None``."""
        ...

    async def find_many(
        self, *, filters: DownloadJobFilter, page: PageRequest
    ) -> Page[DownloadJob]:
        """Return one page of jobs matching ``filters``, newest first."""
        ...

    async def claim_next_queued(self, *, limit: int) -> Sequence[DownloadJob]:
        """Atomically take up to ``limit`` queued jobs, highest priority first.

        Reserved for the future scheduler; implementations must guarantee that
        no two callers receive the same job.
        """
        ...

    async def delete(self, job_id: JobId) -> None:
        """Stage removal of a job. Deleting an unknown id is a no-op."""
        ...
