"""Use case: read a single media item."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.application.media.dto import MediaSummary
from mediahub.domain.media.errors import MediaNotFoundError
from mediahub.domain.media.value_objects import MediaId

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.media.dto import GetMediaQuery


class GetMedia:
    """Fetch one catalogued item by identifier.

    A missing item raises rather than returning ``None``: "not found" is an
    outcome the caller must handle explicitly, and the presentation layer turns
    the error into ``404`` without any branching of its own.
    """

    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work = unit_of_work

    async def execute(self, request: GetMediaQuery) -> MediaSummary:
        """Return the requested item.

        Args:
            request: The query naming the item.

        Returns:
            A summary of the item.

        Raises:
            MediaNotFoundError: If no item exists with that identifier.
        """
        media_id = MediaId(request.media_id)

        async with self._unit_of_work() as uow:
            item = await uow.media.get(media_id)

        if item is None:
            raise MediaNotFoundError(media_id)
        return MediaSummary.from_entity(item)
