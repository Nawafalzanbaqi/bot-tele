"""Commands, queries and result objects for the download use cases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mediahub.application.common.use_case import Command, Query
from mediahub.domain.common.pagination import PageRequest
from mediahub.domain.download.enums import JobPriority

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime
    from uuid import UUID

    from mediahub.application.delivery.ports import DeliveryTarget
    from mediahub.domain.download.entities import DownloadJob
    from mediahub.domain.download.enums import JobStatus
    from mediahub.domain.media.enums import MediaType


@dataclass(frozen=True, slots=True)
class RequestDownloadCommand(Command):
    """Queue an acquisition for an already-catalogued media item.

    Attributes:
        media_id: The item whose bytes should be fetched.
        priority: Scheduling priority for the new job.
        max_attempts: Optional override of the default retry budget.
    """

    media_id: UUID
    priority: JobPriority = JobPriority.NORMAL
    max_attempts: int | None = None


@dataclass(frozen=True, slots=True)
class GetDownloadJobQuery(Query):
    """Read one job.

    Attributes:
        job_id: Identifier of the job to read.
    """

    job_id: UUID


@dataclass(frozen=True, slots=True)
class ListDownloadJobsQuery(Query):
    """Read a page of jobs.

    Attributes:
        status: Optional lifecycle-state filter.
        priority: Optional priority filter.
        media_id: Optional filter to one media item's jobs.
        page: The requested window; defaults to the first page.
    """

    status: JobStatus | None = None
    priority: JobPriority | None = None
    media_id: UUID | None = None
    page: PageRequest = field(default_factory=PageRequest)


@dataclass(frozen=True, slots=True)
class CancelDownloadJobCommand(Command):
    """Stop a job that has not finished yet.

    Attributes:
        job_id: Identifier of the job to cancel.
    """

    job_id: UUID


@dataclass(frozen=True, slots=True)
class DownloadJobSummary:
    """A read-only projection of a :class:`~mediahub.domain.download.entities.DownloadJob`.

    Attributes:
        job_id: Identifier of the job.
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

    job_id: UUID
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
    def from_entity(cls, job: DownloadJob) -> DownloadJobSummary:
        """Project a domain aggregate into a transport-safe summary."""
        return cls(
            job_id=job.id.value,
            media_id=job.media_id.value,
            source_url=str(job.source_url),
            status=job.status,
            priority=job.priority,
            downloaded_bytes=job.progress.downloaded_bytes,
            total_bytes=job.progress.total_bytes,
            percentage=job.progress.percentage,
            attempts=job.attempts,
            max_attempts=job.retry_policy.max_attempts,
            last_error=job.last_error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


# --------------------------------------------------------------------------- #
# Direct acquisition                                                           #
#                                                                              #
# The DTOs below serve the "submit a link, get the file" flow that interfaces  #
# drive today. They are separate from the job DTOs above because a job is a    #
# queued unit of work with a lifecycle, while this is a single request a       #
# caller waits on. When the queue and worker arrive, the flow becomes          #
# enqueue-and-observe and these DTOs describe the same information, sourced    #
# from a job rather than from a live call.                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class QualityOption:
    """One choice a caller may make about how to acquire a source.

    Attributes:
        key: Short, opaque, stable identifier. Travels in places with tight
            size limits, so it is never a URL or a format expression.
        label: What to show a person.
        format_id: Engine-specific rendition, when the option names one.
        height: Pixel height the engine is capped at, when the option caps one.
            **Not the number in the label**, and the two differ on vertical
            video: a rung called "1080p" on a 1080x1920 clip caps at 1920,
            because that is how tall the 1080p rendition is. Capping at 1080
            would exclude the very rendition the label promises.
        approx_bytes: Rough size, when the source declares one. Always a hint.
        is_audio_only: Whether choosing this yields audio without video.
    """

    key: str
    label: str
    format_id: str | None = None
    height: int | None = None
    approx_bytes: int | None = None
    is_audio_only: bool = False


@dataclass(frozen=True, slots=True)
class ProbeSourceQuery(Query):
    """Ask what is at a URL, without downloading it.

    Attributes:
        url: The raw value the caller supplied. Validated downstream.
    """

    url: str


@dataclass(frozen=True, slots=True)
class SourceSummary:
    """What a caller is told about a source before choosing anything.

    Attributes:
        url: Canonical form of the source.
        provider: Platform the source came from.
        title: Title as advertised.
        kind: Broad media category.
        duration_seconds: Length, when the source declares one.
        thumbnail_url: Largest advertised poster image.
        is_live: Whether this is an endless stream.
        is_playlist: Whether the URL denotes a collection.
        expected_bytes: Rough size of the default choice.
        qualities: The choices on offer, best first.
        from_playlist: Whether the link was a collection and this is its first
            item. ``playlist_size`` is how many the collection held.
    """

    url: str
    provider: str
    title: str
    kind: MediaType
    is_live: bool
    is_playlist: bool
    qualities: tuple[QualityOption, ...]
    duration_seconds: float | None = None
    thumbnail_url: str | None = None
    expected_bytes: int | None = None
    from_playlist: bool = False
    playlist_size: int | None = None


@dataclass(frozen=True, slots=True)
class AcquireMediaCommand(Command):
    """Acquire a source at a chosen quality and deliver it somewhere.

    Attributes:
        url: The source to acquire.
        quality_key: Key of a previously offered
            :class:`QualityOption`.
        target: Where the result should be delivered.
        requested_by: Identity of the caller, for the history record.
        caption: Optional text to accompany the delivery.
    """

    url: str
    quality_key: str
    target: DeliveryTarget
    requested_by: str
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class StageTimings:
    """How the wall-clock time of one acquisition was spent.

    Three numbers, because they point at three different things: a slow probe
    is the source (or a tunnel), a slow download is bandwidth or the engine, a
    slow delivery is the destination. One total hides which of them it was.

    Attributes:
        probe_seconds: Resolving the link to a description.
        download_seconds: Fetching the bytes, including any merge.
        deliver_seconds: Uploading the result and any companions.
    """

    probe_seconds: float
    download_seconds: float
    deliver_seconds: float

    @property
    def total_seconds(self) -> float:
        """Return the sum of the three stages."""
        return self.probe_seconds + self.download_seconds + self.deliver_seconds


@dataclass(frozen=True, slots=True)
class AcquisitionSummary:
    """What happened, once the bytes are somewhere else and gone from here.

    Attributes:
        url: Canonical source.
        provider: Platform it came from.
        title: What it was called.
        quality_label: Which rendition was taken.
        bytes_delivered: Size of what was sent.
        elapsed_seconds: Wall-clock time for the whole operation.
        remote_id: The destination's reference to the stored bytes.
        remote_unique_id: Stable identifier that survives credential changes.
        message_id: Where the destination announced it.
        delivered_at: When the destination confirmed (UTC).
        local_copy_released: Whether the local bytes have been deleted. Always
            true on success - it is the point of the design, and it is reported
            so a caller can assert on it.
        items_delivered: How many files were sent. More than one when the post
            was a carousel or a slideshow, and worth reporting: a caller that
            sees "1" for a five-picture post has lost four of them and would
            otherwise have no way to know.
    """

    url: str
    provider: str
    title: str
    quality_label: str
    bytes_delivered: int
    elapsed_seconds: float
    remote_id: str
    delivered_at: datetime
    remote_unique_id: str | None = None
    message_id: str | None = None
    local_copy_released: bool = True
    items_delivered: int = 1
    via_proxy: bool = False
    """Whether the fetch went through the egress proxy rather than the direct path."""
    capped_from: str | None = None
    """Label of the better rung that was skipped because it would not fit the destination."""
    sent_as_document: bool = False
    """Whether a video went as a file because its codec does not play inline."""
    stages: StageTimings | None = None
    """Where the time went, when the use case measured it."""


# --------------------------------------------------------------------------- #
# Worker settlement                                                            #
#                                                                              #
# What a worker learns after reporting the end of an attempt. Returned rather  #
# than logged so that the caller - the claim loop - can act on it without      #
# re-reading the job, and so that tests assert on an outcome instead of on a   #
# side effect.                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class JobSettlement:
    """How an attempt ended, from the queue's point of view.

    Attributes:
        job_id: The job that was settled.
        status: The lifecycle state it now holds.
        retrying: Whether it went back to the queue for another attempt.
        available_at: When it may next be claimed, if it is retrying.
        failure_code: Stable code of the failure, when it failed.
    """

    job_id: UUID
    status: JobStatus
    retrying: bool = False
    available_at: datetime | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class LeaseRecovery:
    """What a recovery sweep took back.

    Attributes:
        reclaimed: Jobs whose lease was taken back and returned to the queue.
        abandoned: Jobs whose lease was taken back but which could not be
            requeued - their retry budget was already spent. They stay failed,
            which is the honest outcome: a job that reliably kills its worker
            must not be retried forever.
    """

    reclaimed: tuple[UUID, ...] = ()
    abandoned: tuple[UUID, ...] = ()

    @property
    def total(self) -> int:
        """Return how many leases the sweep touched."""
        return len(self.reclaimed) + len(self.abandoned)


@dataclass(frozen=True, slots=True)
class GetHistoryQuery(Query):
    """Read a principal's recent acquisitions.

    Attributes:
        principal: Whose history to read. A caller only ever sees their own.
        limit: How many entries, newest first.
    """

    principal: str
    limit: int = 10


@dataclass(frozen=True, slots=True)
class HistoryEntrySummary:
    """One remembered acquisition.

    Attributes:
        title: What it was called.
        url: Canonical source, so it can be re-acquired.
        provider: Platform it came from.
        quality_label: Which rendition was taken.
        bytes_delivered: Size of what was sent.
        delivered_at: When the destination confirmed (UTC).
        message_id: Where the destination announced it.
    """

    title: str
    url: str
    provider: str
    quality_label: str
    bytes_delivered: int
    delivered_at: datetime
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilitiesSummary:
    """What this deployment can currently do.

    Answers "why was my file refused?" before it is refused, which is the only
    genuinely useful thing a settings screen can offer on a system with no
    per-user settings.

    Attributes:
        engine: Download engine name.
        engine_version: Its version.
        max_item_bytes: Largest source this deployment will fetch.
        delivery_provider: Where results are sent.
        delivery_max_bytes: Largest artifact that destination accepts.
        effective_max_bytes: The smaller of the two, which is what actually
            applies.
        supports_audio_only: Whether audio-only acquisition is available.
        allow_live: Whether live sources may be captured.
        allow_playlist: Whether collections may be acquired.
    """

    engine: str
    engine_version: str
    max_item_bytes: int
    delivery_provider: str
    delivery_max_bytes: int
    effective_max_bytes: int
    supports_audio_only: bool
    allow_live: bool
    allow_playlist: bool
