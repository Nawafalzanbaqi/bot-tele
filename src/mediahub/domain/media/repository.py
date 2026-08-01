"""Persistence port for the media aggregate.

The interface lives in the domain and its implementations live in
:mod:`mediahub.infrastructure.persistence` - this is the dependency inversion
that keeps the core independent of SQLAlchemy, Postgres, or any future store.

It is declared as a :class:`typing.Protocol` rather than an abstract base class
so adapters do not have to inherit from a domain symbol; conformance is checked
structurally by mypy wherever an adapter is injected.

Contract notes:

* ``get``/``find_by_source_url`` return ``None`` when nothing matches. Raising
  :class:`~mediahub.domain.media.errors.MediaNotFoundError` is the use case's
  decision, not the adapter's.
* Writes are staged; they only become durable when the surrounding
  :class:`~mediahub.application.common.unit_of_work.UnitOfWork` commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.domain.common.pagination import Page, PageRequest
    from mediahub.domain.media.entities import MediaItem
    from mediahub.domain.media.enums import MediaStatus, MediaType
    from mediahub.domain.media.value_objects import MediaId, SourceUrl


@dataclass(frozen=True, slots=True)
class MediaFilter:
    """Criteria for narrowing a media listing.

    Every field is optional; ``None`` means "do not filter on this". Fields are
    combined with AND.

    Attributes:
        status: Restrict to a single lifecycle state.
        media_type: Restrict to a single category.
        search: Case-insensitive substring matched against the title.
    """

    status: MediaStatus | None = None
    media_type: MediaType | None = None
    search: str | None = None


class MediaRepository(Protocol):
    """Collection-like access to :class:`~mediahub.domain.media.entities.MediaItem`."""

    async def add(self, media: MediaItem) -> None:
        """Stage a newly registered item for insertion."""
        ...

    async def save(self, media: MediaItem) -> None:
        """Stage the current state of an already-known item."""
        ...

    async def get(self, media_id: MediaId) -> MediaItem | None:
        """Return the item with this identifier, or ``None``."""
        ...

    async def find_by_source_url(self, source_url: SourceUrl) -> MediaItem | None:
        """Return the item catalogued for this URL, or ``None``."""
        ...

    async def find_many(self, *, filters: MediaFilter, page: PageRequest) -> Page[MediaItem]:
        """Return one page of items matching ``filters``, newest first."""
        ...

    async def delete(self, media_id: MediaId) -> None:
        """Stage removal of an item. Deleting an unknown id is a no-op."""
        ...
