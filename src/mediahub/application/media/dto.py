"""Commands, queries and result objects for the media use cases.

DTOs are plain frozen dataclasses built from primitives. They are the contract
between the application layer and whatever is driving it - HTTP today, a CLI or
a message consumer tomorrow - which is why they never expose domain entities:
handing a ``MediaItem`` to the presentation layer would let a route mutate an
aggregate without a transaction.

Conversion happens in exactly one direction here: ``from_entity`` maps domain
state outward. Mapping inward (validating raw strings into value objects) is
the use case's job, so validation errors are domain errors with proper codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mediahub.application.common.use_case import Command, Query
from mediahub.domain.common.pagination import PageRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime
    from uuid import UUID

    from mediahub.domain.media.entities import MediaItem
    from mediahub.domain.media.enums import MediaStatus, MediaType


@dataclass(frozen=True, slots=True)
class RegisterMediaCommand(Command):
    """Catalogue a new item.

    Attributes:
        source_url: Raw URL the content comes from; validated by the use case.
        title: Raw human-readable name; normalised by the use case.
        media_type: Broad category to file the item under.
    """

    source_url: str
    title: str
    media_type: MediaType


@dataclass(frozen=True, slots=True)
class GetMediaQuery(Query):
    """Read one item.

    Attributes:
        media_id: Identifier of the item to read.
    """

    media_id: UUID


@dataclass(frozen=True, slots=True)
class ListMediaQuery(Query):
    """Read a page of items.

    Attributes:
        status: Optional lifecycle-state filter.
        media_type: Optional category filter.
        search: Optional case-insensitive substring matched against the title.
        page: The requested window; defaults to the first page.
    """

    status: MediaStatus | None = None
    media_type: MediaType | None = None
    search: str | None = None
    page: PageRequest = field(default_factory=PageRequest)


@dataclass(frozen=True, slots=True)
class ArchiveMediaCommand(Command):
    """Retire an item from the active library.

    Attributes:
        media_id: Identifier of the item to archive.
    """

    media_id: UUID


@dataclass(frozen=True, slots=True)
class MediaSummary:
    """A read-only projection of a :class:`~mediahub.domain.media.entities.MediaItem`.

    Attributes:
        media_id: Identifier of the item.
        source_url: Canonical origin of the content.
        title: Normalised human-readable name.
        media_type: Broad category.
        status: Current lifecycle state.
        storage_key: Location of the bytes, or ``None`` while unavailable.
        size_bytes: Stored size, or ``None`` while unavailable.
        checksum: Integrity hash as ``algorithm:digest``, when known.
        failure_reason: Why the last acquisition failed, if it did.
        created_at: When the item was catalogued (UTC).
        updated_at: When the item last changed (UTC).
    """

    media_id: UUID
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
    def from_entity(cls, item: MediaItem) -> MediaSummary:
        """Project a domain aggregate into a transport-safe summary."""
        return cls(
            media_id=item.id.value,
            source_url=str(item.source_url),
            title=str(item.title),
            media_type=item.media_type,
            status=item.status,
            storage_key=None if item.storage_key is None else str(item.storage_key),
            size_bytes=None if item.size is None else item.size.bytes_,
            checksum=None if item.checksum is None else str(item.checksum),
            failure_reason=item.failure_reason,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
