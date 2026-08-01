"""Use case: catalogue a new media item."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.media.dto import MediaSummary
from mediahub.domain.media.entities import MediaItem
from mediahub.domain.media.errors import DuplicateMediaError
from mediahub.domain.media.value_objects import MediaId, MediaTitle, SourceUrl

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock, EventPublisher, UuidGenerator
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.media.dto import RegisterMediaCommand


class RegisterMedia:
    """Register a media item so the rest of the system can refer to it.

    The item starts in :attr:`~mediahub.domain.media.enums.MediaStatus.PENDING`:
    registration records *intent*, it does not fetch anything. Acquiring the
    bytes is a separate concern, requested through
    :class:`~mediahub.application.download.use_cases.request_download.RequestDownload`.

    Duplicate detection is by canonical source URL, so the same link submitted
    with a different fragment or letter case is still recognised as a
    duplicate.
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

    async def execute(self, request: RegisterMediaCommand) -> MediaSummary:
        """Validate the request, persist a new item and announce it.

        Args:
            request: The raw registration command.

        Returns:
            A summary of the newly catalogued item.

        Raises:
            InvalidSourceUrlError: If the URL is malformed or unsupported.
            InvalidMediaTitleError: If the title is empty or too long.
            DuplicateMediaError: If the source URL is already catalogued.
        """
        source_url = SourceUrl(request.source_url)
        title = MediaTitle(request.title)

        async with self._unit_of_work() as uow:
            if await uow.media.find_by_source_url(source_url) is not None:
                raise DuplicateMediaError(source_url)

            item = MediaItem.register(
                media_id=MediaId(self._uuid_generator.new_uuid()),
                source_url=source_url,
                title=title,
                media_type=request.media_type,
                now=self._clock.now(),
            )
            await uow.media.add(item)
            await uow.commit()

        await self._event_publisher.publish(item.pull_events())
        logger.bind(media_id=str(item.id), source_host=source_url.host).info(
            "Registered media item"
        )
        return MediaSummary.from_entity(item)
