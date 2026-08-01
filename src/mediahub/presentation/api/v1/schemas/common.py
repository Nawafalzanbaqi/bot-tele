"""Schemas shared by every version 1 resource."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from mediahub.domain.common.pagination import PageRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.domain.common.pagination import Page

DEFAULT_PAGE_LIMIT = 50


class PageResponse[TItem](BaseModel):
    """One page of results and the numbers needed to request the next.

    Attributes:
        items: The results in this window.
        total: Total number of matches, ignoring the window.
        limit: The limit that produced this page.
        offset: The offset that produced this page.
        has_next: Whether more results exist after this window.
    """

    model_config = ConfigDict(frozen=True)

    items: list[TItem]
    total: int = Field(description="Total matches, ignoring pagination.")
    limit: int
    offset: int
    has_next: bool

    @classmethod
    def from_page[TDto](cls, page: Page[TDto], items: list[TItem]) -> PageResponse[TItem]:
        """Build a response from a domain page and its converted items."""
        return cls(
            items=items,
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_next=page.has_next,
        )


def pagination_params(
    limit: Annotated[
        int,
        Query(ge=1, le=PageRequest.MAX_LIMIT, description="Maximum items to return."),
    ] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, description="Items to skip.")] = 0,
) -> PageRequest:
    """Turn ``limit``/``offset`` query parameters into the domain's window type.

    Bounds are enforced twice on purpose: here, so a bad request gets a clear
    ``422`` before any work starts, and again in
    :class:`~mediahub.domain.common.pagination.PageRequest`, which is the rule
    every caller obeys - HTTP or not.
    """
    return PageRequest(limit=limit, offset=offset)


PaginationQuery = Annotated[PageRequest, Depends(pagination_params)]
"""Dependency alias exposing ``limit`` and ``offset`` as query parameters."""
