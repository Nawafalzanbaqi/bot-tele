"""Use case: retire a media item from the active library."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.media.dto import MediaSummary
from mediahub.domain.media.errors import MediaNotFoundError
from mediahub.domain.media.value_objects import MediaId

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock, EventPublisher
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.media.dto import ArchiveMediaCommand


class ArchiveMedia:
    """Archive an item instead of deleting it.

    Archiving is terminal but non-destructive: the record and its history stay
    queryable, which is what an operator needs when auditing what the library
    once contained. Reclaiming disk space is a separate, explicit operation.
    """

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        event_publisher: EventPublisher,
    ) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._event_publisher = event_publisher

    async def execute(self, request: ArchiveMediaCommand) -> MediaSummary:
        """Archive the item and announce the change.

        Args:
            request: The command naming the item.

        Returns:
            A summary of the archived item.

        Raises:
            MediaNotFoundError: If no item exists with that identifier.
            InvalidMediaTransitionError: If the item is already archived.
        """
        media_id = MediaId(request.media_id)

        async with self._unit_of_work() as uow:
            item = await uow.media.get(media_id)
            if item is None:
                raise MediaNotFoundError(media_id)

            item.archive(now=self._clock.now())
            await uow.media.save(item)
            await uow.commit()

        await self._event_publisher.publish(item.pull_events())
        logger.bind(media_id=str(media_id)).info("Archived media item")
        return MediaSummary.from_entity(item)
