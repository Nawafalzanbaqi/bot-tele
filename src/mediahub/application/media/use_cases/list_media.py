"""Use case: read a page of media items."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediahub.application.media.dto import MediaSummary
from mediahub.domain.common.pagination import Page
from mediahub.domain.media.repository import MediaFilter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.unit_of_work import UnitOfWorkFactory
    from mediahub.application.media.dto import ListMediaQuery


class ListMedia:
    """Browse the catalogue with optional filters.

    The window is always bounded - :class:`~mediahub.domain.common.pagination.PageRequest`
    caps the limit - so no caller can ask an adapter for an unbounded scan.
    """

    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        """Wire the use case to its ports."""
        self._unit_of_work = unit_of_work

    async def execute(self, request: ListMediaQuery) -> Page[MediaSummary]:
        """Return one page of matching items, newest first.

        Args:
            request: Filters plus the requested window.

        Returns:
            A page of summaries, carrying the total match count so the caller
            can render pagination controls.
        """
        filters = MediaFilter(
            status=request.status,
            media_type=request.media_type,
            search=request.search,
        )

        async with self._unit_of_work() as uow:
            page = await uow.media.find_many(filters=filters, page=request.page)

        return Page(
            items=[MediaSummary.from_entity(item) for item in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )
