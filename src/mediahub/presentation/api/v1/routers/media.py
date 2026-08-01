"""``/api/v1/media`` - the catalogue."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from mediahub.application.media.dto import (
    ArchiveMediaCommand,
    GetMediaQuery,
    ListMediaQuery,
    RegisterMediaCommand,
)
from mediahub.domain.media.enums import MediaStatus, MediaType
from mediahub.presentation.api.dependencies import (
    ArchiveMediaDep,
    GetMediaDep,
    ListMediaDep,
    RegisterMediaDep,
)
from mediahub.presentation.api.v1.schemas.common import PageResponse, PaginationQuery
from mediahub.presentation.api.v1.schemas.media import MediaResponse, RegisterMediaRequest

router = APIRouter(prefix="/media", tags=["media"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Register a media item",
    response_description="The newly catalogued item.",
)
async def register_media(
    payload: RegisterMediaRequest,
    use_case: RegisterMediaDep,
) -> MediaResponse:
    """Catalogue a new item.

    The item is created in ``pending`` state - registration records intent, it
    does not fetch anything. Queue the transfer separately with
    ``POST /api/v1/downloads``.

    Responses:
        201: The item was catalogued.
        409: An item is already registered for this source URL.
        422: The URL or title failed validation.
    """
    summary = await use_case.execute(
        RegisterMediaCommand(
            source_url=payload.source_url,
            title=payload.title,
            media_type=payload.media_type,
        )
    )
    return MediaResponse.from_dto(summary)


@router.get(
    "",
    summary="List media items",
    response_description="A page of catalogued items, newest first.",
)
async def list_media(
    use_case: ListMediaDep,
    pagination: PaginationQuery,
    item_status: Annotated[MediaStatus | None, Query(alias="status")] = None,
    media_type: Annotated[MediaType | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> PageResponse[MediaResponse]:
    """Browse the catalogue, optionally filtered by state, type or title."""
    page = await use_case.execute(
        ListMediaQuery(
            status=item_status,
            media_type=media_type,
            search=search,
            page=pagination,
        )
    )
    return PageResponse.from_page(page, [MediaResponse.from_dto(item) for item in page.items])


@router.get(
    "/{media_id}",
    summary="Get a media item",
    response_description="The requested item.",
)
async def get_media(media_id: UUID, use_case: GetMediaDep) -> MediaResponse:
    """Read a single item.

    Responses:
        200: The item was found.
        404: No item exists with this identifier.
    """
    summary = await use_case.execute(GetMediaQuery(media_id=media_id))
    return MediaResponse.from_dto(summary)


@router.post(
    "/{media_id}/archive",
    summary="Archive a media item",
    response_description="The archived item.",
)
async def archive_media(media_id: UUID, use_case: ArchiveMediaDep) -> MediaResponse:
    """Retire an item from the active library.

    Archiving is terminal and non-destructive: the record stays queryable. It
    is modelled as an explicit action rather than as ``DELETE`` because nothing
    is actually deleted.

    Responses:
        200: The item was archived.
        404: No item exists with this identifier.
        409: The item is already archived.
    """
    summary = await use_case.execute(ArchiveMediaCommand(media_id=media_id))
    return MediaResponse.from_dto(summary)
