"""Request and response schemas for the media resource."""

from __future__ import annotations

# `datetime` and `UUID` are imported at runtime, not under TYPE_CHECKING: Pydantic
# resolves field annotations when it builds the model.
from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mediahub.domain.media.enums import MediaStatus, MediaType
from mediahub.domain.media.value_objects import MediaTitle, SourceUrl

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.media.dto import MediaSummary


class RegisterMediaRequest(BaseModel):
    """Body of ``POST /api/v1/media``.

    Attributes:
        source_url: Where the content originates. ``http``/``https`` only.
        title: Human-readable name for the library.
        media_type: Broad category to file the item under.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "source_url": "https://example.com/talks/clean-architecture.mp4",
                    "title": "Clean Architecture, revisited",
                    "media_type": "video",
                }
            ]
        },
    )

    source_url: Annotated[str, Field(min_length=1, max_length=SourceUrl.MAX_LENGTH)]
    title: Annotated[str, Field(min_length=1, max_length=MediaTitle.MAX_LENGTH)]
    media_type: MediaType = MediaType.OTHER


class MediaResponse(BaseModel):
    """A catalogued media item as returned by the API.

    Attributes:
        id: Identifier of the item.
        source_url: Canonical origin of the content.
        title: Normalised human-readable name.
        media_type: Broad category.
        status: Current lifecycle state.
        storage_key: Location of the bytes, or ``null`` while unavailable.
        size_bytes: Stored size, or ``null`` while unavailable.
        checksum: Integrity hash as ``algorithm:digest``, when known.
        failure_reason: Why the last acquisition failed, if it did.
        created_at: When the item was catalogued (UTC).
        updated_at: When the item last changed (UTC).
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    source_url: str
    title: str
    media_type: MediaType
    status: MediaStatus
    storage_key: str | None
    size_bytes: int | None
    checksum: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_dto(cls, summary: MediaSummary) -> MediaResponse:
        """Build the response from an application DTO."""
        return cls(
            id=summary.media_id,
            source_url=summary.source_url,
            title=summary.title,
            media_type=summary.media_type,
            status=summary.status,
            storage_key=summary.storage_key,
            size_bytes=summary.size_bytes,
            checksum=summary.checksum,
            failure_reason=summary.failure_reason,
            created_at=summary.created_at,
            updated_at=summary.updated_at,
        )
