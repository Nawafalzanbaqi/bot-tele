"""Closed value sets used by the media aggregate.

``StrEnum`` members serialise as their own string value, so the same constant
is safe in a database column, a JSON payload and a log line. Adding a member is
backwards compatible; renaming one is a breaking change and requires a
migration.
"""

from __future__ import annotations

from enum import StrEnum


class MediaType(StrEnum):
    """The broad category of a catalogued item.

    Kept intentionally coarse: fine-grained container/codec details belong to
    a future technical-metadata value object, not to this classification.
    """

    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    DOCUMENT = "document"
    OTHER = "other"


class MediaStatus(StrEnum):
    """Lifecycle state of a media item.

    Attributes:
        PENDING: Catalogued, bytes not available locally yet.
        AVAILABLE: Bytes are stored and verified.
        FAILED: Acquisition failed; ``failure_reason`` explains why.
        ARCHIVED: Retired from the active library. Terminal.
    """

    PENDING = "pending"
    AVAILABLE = "available"
    FAILED = "failed"
    ARCHIVED = "archived"

    @property
    def is_terminal(self) -> bool:
        """Return whether no further transition is allowed from this state."""
        return self is MediaStatus.ARCHIVED
