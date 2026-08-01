"""Facts published by the media aggregate.

Payloads deliberately use primitives (``UUID``, ``str``, ``int``) instead of
value objects: events cross process boundaries - a log sink today, a message
broker tomorrow - and must stay trivially serialisable and stable over time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mediahub.domain.common.events import DomainEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaRegistered(DomainEvent):
    """A new media item was catalogued.

    Attributes:
        media_id: Identifier of the new item.
        source_url: Canonical URL the item originates from.
        media_type: Broad category assigned at registration.
    """

    media_id: UUID
    source_url: str
    media_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaRenamed(DomainEvent):
    """A media item's title changed.

    Attributes:
        media_id: Identifier of the item.
        title: The new, normalised title.
    """

    media_id: UUID
    title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaBecameAvailable(DomainEvent):
    """A media item's bytes are stored locally and verified.

    Attributes:
        media_id: Identifier of the item.
        storage_key: Location of the bytes, relative to the library root.
        size_bytes: Size of the stored artefact.
    """

    media_id: UUID
    storage_key: str
    size_bytes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaFailed(DomainEvent):
    """Acquiring a media item did not succeed.

    Attributes:
        media_id: Identifier of the item.
        reason: Operator-facing explanation of the failure.
    """

    media_id: UUID
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaArchived(DomainEvent):
    """A media item was retired from the active library.

    Attributes:
        media_id: Identifier of the item.
    """

    media_id: UUID
