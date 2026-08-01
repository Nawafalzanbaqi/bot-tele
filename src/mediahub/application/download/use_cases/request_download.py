"""Use case: queue an acquisition for a catalogued media item."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.dto import DownloadJobSummary
from mediahub.domain.download.entities import DownloadJob
from mediahub.domain.download.errors import DuplicateActiveJobError
from mediahub.domain.download.value_objects import JobId, RetryPolicy
from mediahub.domain.media.errors import MediaNotFoundError
from mediahub.domain.media.value_objects import MediaId

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock, EventPublisher, UuidGenerator
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.download.dto import RequestDownloadCommand


class RequestDownload:
    """Accept a download request and persist it as a queued job.

    This use case is **complete**: it validates the request, enforces that only
    one job is in flight per media item, and durably records the intent. What
    it deliberately does not do is start transferring - no
    :class:`~mediahub.application.download.ports.DownloaderPort` is invoked
    here. A queued job is a promise the system will keep once an engine and a
    worker exist; until then jobs accumulate safely and can be listed and
    cancelled.

    Refusing a second active job for the same item is a real safety rule, not a
    convenience: two engines writing one storage key would corrupt the artefact.
    """

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        uuid_generator: UuidGenerator,
        event_publisher: EventPublisher,
    ) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._uuid_generator = uuid_generator
        self._event_publisher = event_publisher

    async def execute(self, request: RequestDownloadCommand) -> DownloadJobSummary:
        """Queue a job for the requested media item.

        Args:
            request: The command naming the item and its scheduling options.

        Returns:
            A summary of the queued job.

        Raises:
            MediaNotFoundError: If the media item does not exist.
            DuplicateActiveJobError: If a job for it is already queued/running.
            InvalidRetryPolicyError: If ``max_attempts`` is out of bounds.
        """
        media_id = MediaId(request.media_id)
        retry_policy = (
            RetryPolicy.default()
            if request.max_attempts is None
            else RetryPolicy(max_attempts=request.max_attempts)
        )

        async with self._unit_of_work() as uow:
            item = await uow.media.get(media_id)
            if item is None:
                raise MediaNotFoundError(media_id)

            if await uow.download_jobs.find_active_for_media(media_id) is not None:
                raise DuplicateActiveJobError(media_id)

            job = DownloadJob.request(
                job_id=JobId(self._uuid_generator.new_uuid()),
                media_id=media_id,
                source_url=item.source_url,
                priority=request.priority,
                retry_policy=retry_policy,
                now=self._clock.now(),
            )
            await uow.download_jobs.add(job)
            await uow.commit()

        await self._event_publisher.publish(job.pull_events())
        logger.bind(
            job_id=str(job.id),
            media_id=str(media_id),
            priority=job.priority.value,
        ).info("Queued download job")
        return DownloadJobSummary.from_entity(job)
