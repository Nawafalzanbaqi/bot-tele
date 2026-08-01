"""Download use cases queue work without executing it."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from mediahub.application.download.dto import (
    CancelDownloadJobCommand,
    GetDownloadJobQuery,
    ListDownloadJobsQuery,
    RequestDownloadCommand,
)
from mediahub.application.download.use_cases.cancel_download_job import CancelDownloadJob
from mediahub.application.download.use_cases.get_download_job import GetDownloadJob
from mediahub.application.download.use_cases.list_download_jobs import ListDownloadJobs
from mediahub.application.download.use_cases.request_download import RequestDownload
from mediahub.application.media.dto import RegisterMediaCommand
from mediahub.application.media.use_cases.register_media import RegisterMedia
from mediahub.domain.download.enums import JobPriority, JobStatus
from mediahub.domain.download.errors import (
    DownloadJobNotFoundError,
    DuplicateActiveJobError,
    InvalidJobTransitionError,
)
from mediahub.domain.media.enums import MediaType
from mediahub.domain.media.errors import MediaNotFoundError

if TYPE_CHECKING:
    from mediahub.application.media.dto import MediaSummary
    from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
    from tests.conftest import (
        FrozenClock,
        RecordingEventPublisher,
        SequentialUuidGenerator,
    )

pytestmark = pytest.mark.unit


@pytest.fixture
async def media(
    unit_of_work: InMemoryUnitOfWorkFactory,
    clock: FrozenClock,
    uuid_generator: SequentialUuidGenerator,
    event_publisher: RecordingEventPublisher,
) -> MediaSummary:
    """A registered media item to hang jobs off."""
    summary = await RegisterMedia(
        unit_of_work=unit_of_work,
        clock=clock,
        uuid_generator=uuid_generator,
        event_publisher=event_publisher,
    ).execute(
        RegisterMediaCommand(
            source_url="https://example.com/a.mp4", title="A", media_type=MediaType.VIDEO
        )
    )
    event_publisher.published.clear()
    return summary


@pytest.fixture
def request_download(
    unit_of_work: InMemoryUnitOfWorkFactory,
    clock: FrozenClock,
    uuid_generator: SequentialUuidGenerator,
    event_publisher: RecordingEventPublisher,
) -> RequestDownload:
    return RequestDownload(
        unit_of_work=unit_of_work,
        clock=clock,
        uuid_generator=uuid_generator,
        event_publisher=event_publisher,
    )


class TestRequestDownload:
    async def test_queues_a_job_without_running_it(
        self,
        media: MediaSummary,
        request_download: RequestDownload,
        event_publisher: RecordingEventPublisher,
    ) -> None:
        job = await request_download.execute(
            RequestDownloadCommand(media_id=media.media_id, priority=JobPriority.HIGH)
        )

        assert job.status is JobStatus.QUEUED
        assert job.media_id == media.media_id
        assert job.source_url == media.source_url
        assert job.attempts == 0
        assert job.started_at is None
        assert event_publisher.names() == ["DownloadRequested"]

    async def test_unknown_media_is_rejected(self, request_download: RequestDownload) -> None:
        with pytest.raises(MediaNotFoundError):
            await request_download.execute(RequestDownloadCommand(media_id=UUID(int=99)))

    async def test_only_one_active_job_per_item(
        self, media: MediaSummary, request_download: RequestDownload
    ) -> None:
        await request_download.execute(RequestDownloadCommand(media_id=media.media_id))

        with pytest.raises(DuplicateActiveJobError):
            await request_download.execute(RequestDownloadCommand(media_id=media.media_id))

    async def test_retry_budget_can_be_overridden(
        self, media: MediaSummary, request_download: RequestDownload
    ) -> None:
        job = await request_download.execute(
            RequestDownloadCommand(media_id=media.media_id, max_attempts=7)
        )

        assert job.max_attempts == 7


class TestReadUseCases:
    async def test_get_returns_the_job(
        self,
        media: MediaSummary,
        request_download: RequestDownload,
        unit_of_work: InMemoryUnitOfWorkFactory,
    ) -> None:
        created = await request_download.execute(RequestDownloadCommand(media_id=media.media_id))

        job = await GetDownloadJob(unit_of_work=unit_of_work).execute(
            GetDownloadJobQuery(job_id=created.job_id)
        )
        assert job.job_id == created.job_id

    async def test_get_raises_for_unknown_job(
        self, unit_of_work: InMemoryUnitOfWorkFactory
    ) -> None:
        with pytest.raises(DownloadJobNotFoundError):
            await GetDownloadJob(unit_of_work=unit_of_work).execute(
                GetDownloadJobQuery(job_id=UUID(int=99))
            )

    async def test_list_filters_by_status(
        self,
        media: MediaSummary,
        request_download: RequestDownload,
        unit_of_work: InMemoryUnitOfWorkFactory,
    ) -> None:
        await request_download.execute(RequestDownloadCommand(media_id=media.media_id))

        listed = await ListDownloadJobs(unit_of_work=unit_of_work).execute(
            ListDownloadJobsQuery(status=JobStatus.QUEUED)
        )
        assert listed.total == 1

        none_running = await ListDownloadJobs(unit_of_work=unit_of_work).execute(
            ListDownloadJobsQuery(status=JobStatus.RUNNING)
        )
        assert none_running.total == 0


class TestCancelDownloadJob:
    async def test_cancels_a_queued_job(
        self,
        media: MediaSummary,
        request_download: RequestDownload,
        unit_of_work: InMemoryUnitOfWorkFactory,
        clock: FrozenClock,
        event_publisher: RecordingEventPublisher,
    ) -> None:
        created = await request_download.execute(RequestDownloadCommand(media_id=media.media_id))
        event_publisher.published.clear()

        cancel = CancelDownloadJob(
            unit_of_work=unit_of_work, clock=clock, event_publisher=event_publisher
        )
        cancelled = await cancel.execute(CancelDownloadJobCommand(job_id=created.job_id))

        assert cancelled.status is JobStatus.CANCELLED
        assert event_publisher.names() == ["DownloadCancelled"]

        # Cancelling frees the item for a new job.
        await request_download.execute(RequestDownloadCommand(media_id=media.media_id))

    async def test_cancelling_twice_is_rejected(
        self,
        media: MediaSummary,
        request_download: RequestDownload,
        unit_of_work: InMemoryUnitOfWorkFactory,
        clock: FrozenClock,
        event_publisher: RecordingEventPublisher,
    ) -> None:
        created = await request_download.execute(RequestDownloadCommand(media_id=media.media_id))
        cancel = CancelDownloadJob(
            unit_of_work=unit_of_work, clock=clock, event_publisher=event_publisher
        )
        await cancel.execute(CancelDownloadJobCommand(job_id=created.job_id))

        with pytest.raises(InvalidJobTransitionError):
            await cancel.execute(CancelDownloadJobCommand(job_id=created.job_id))
