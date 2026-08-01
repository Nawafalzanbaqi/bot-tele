"""The ``MediaItem`` aggregate root.

``MediaItem`` is a state machine over :class:`~mediahub.domain.media.enums.MediaStatus`.
Every mutation goes through a named intention (``mark_available``, ``archive``)
rather than through attribute assignment, which means:

* illegal transitions raise instead of silently corrupting state;
* each change stamps ``updated_at`` and records a domain event;
* the rules are readable in one place and testable without any I/O.

Allowed transitions::

    PENDING ──> AVAILABLE ──> ARCHIVED
       │  │         │
       │  └──> FAILED ──> PENDING (retry)
       │           │
       └───────────┴──> ARCHIVED (terminal)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mediahub.domain.common.entity import AggregateRoot
from mediahub.domain.common.time import ensure_utc
from mediahub.domain.media.enums import MediaStatus, MediaType
from mediahub.domain.media.errors import InvalidMediaTransitionError
from mediahub.domain.media.events import (
    MediaArchived,
    MediaBecameAvailable,
    MediaFailed,
    MediaRegistered,
    MediaRenamed,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from datetime import datetime

    from mediahub.domain.common.fingerprint import Fingerprint
    from mediahub.domain.media.value_objects import (
        FileSize,
        MediaId,
        MediaTitle,
        SourceUrl,
        StorageKey,
    )

ALLOWED_TRANSITIONS: Final[Mapping[MediaStatus, frozenset[MediaStatus]]] = {
    MediaStatus.PENDING: frozenset(
        {MediaStatus.AVAILABLE, MediaStatus.FAILED, MediaStatus.ARCHIVED}
    ),
    MediaStatus.AVAILABLE: frozenset({MediaStatus.FAILED, MediaStatus.ARCHIVED}),
    MediaStatus.FAILED: frozenset({MediaStatus.PENDING, MediaStatus.ARCHIVED}),
    MediaStatus.ARCHIVED: frozenset(),
}
"""The complete lifecycle contract. Adding an edge here is a domain decision;
adapters and API handlers must never work around it."""

MAX_FAILURE_REASON_LENGTH: Final[int] = 1000


class MediaItem(AggregateRoot["MediaId"]):
    """One catalogued piece of content and everything MediaHub knows about it."""

    __slots__ = (
        "_checksum",
        "_created_at",
        "_failure_reason",
        "_media_type",
        "_size",
        "_source_url",
        "_status",
        "_storage_key",
        "_title",
        "_updated_at",
    )

    def __init__(  # noqa: PLR0913 - an aggregate's full state is rehydrated at once
        self,
        *,
        media_id: MediaId,
        source_url: SourceUrl,
        title: MediaTitle,
        media_type: MediaType,
        status: MediaStatus,
        created_at: datetime,
        updated_at: datetime,
        storage_key: StorageKey | None = None,
        size: FileSize | None = None,
        checksum: Fingerprint | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Rehydrate an item from stored state.

        Repositories use this constructor to rebuild an aggregate; application
        code should call :meth:`register` instead, which enforces the rules
        that apply when an item first enters the library.
        """
        super().__init__(media_id)
        self._source_url = source_url
        self._title = title
        self._media_type = media_type
        self._status = status
        self._created_at = ensure_utc(created_at, field_name="created_at")
        self._updated_at = ensure_utc(updated_at, field_name="updated_at")
        self._storage_key = storage_key
        self._size = size
        self._checksum = checksum
        self._failure_reason = failure_reason

    # -- Construction -------------------------------------------------------

    @classmethod
    def register(
        cls,
        *,
        media_id: MediaId,
        source_url: SourceUrl,
        title: MediaTitle,
        media_type: MediaType,
        now: datetime,
    ) -> MediaItem:
        """Catalogue a new item in :attr:`MediaStatus.PENDING`.

        Args:
            media_id: Identifier produced by the application layer.
            source_url: Where the content originates from.
            title: Human-readable name.
            media_type: Broad category of the content.
            now: Current time, supplied by the caller's clock.

        Returns:
            A new aggregate carrying a
            :class:`~mediahub.domain.media.events.MediaRegistered` event.
        """
        stamped = ensure_utc(now, field_name="now")
        item = cls(
            media_id=media_id,
            source_url=source_url,
            title=title,
            media_type=media_type,
            status=MediaStatus.PENDING,
            created_at=stamped,
            updated_at=stamped,
        )
        item.record_event(
            MediaRegistered(
                occurred_at=stamped,
                media_id=media_id.value,
                source_url=str(source_url),
                media_type=media_type.value,
            )
        )
        return item

    # -- State ---------------------------------------------------------------

    @property
    def source_url(self) -> SourceUrl:
        """Return the canonical origin of the content."""
        return self._source_url

    @property
    def title(self) -> MediaTitle:
        """Return the current title."""
        return self._title

    @property
    def media_type(self) -> MediaType:
        """Return the broad category of the content."""
        return self._media_type

    @property
    def status(self) -> MediaStatus:
        """Return the current lifecycle state."""
        return self._status

    @property
    def storage_key(self) -> StorageKey | None:
        """Return where the bytes live, or ``None`` while unavailable."""
        return self._storage_key

    @property
    def size(self) -> FileSize | None:
        """Return the stored size, or ``None`` while unavailable."""
        return self._size

    @property
    def checksum(self) -> Fingerprint | None:
        """Return the integrity hash, or ``None`` while unavailable."""
        return self._checksum

    @property
    def failure_reason(self) -> str | None:
        """Return why the last acquisition failed, if it did."""
        return self._failure_reason

    @property
    def created_at(self) -> datetime:
        """Return when the item was catalogued (UTC)."""
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        """Return when the item last changed (UTC)."""
        return self._updated_at

    @property
    def is_available(self) -> bool:
        """Return whether the bytes are present and verified."""
        return self._status is MediaStatus.AVAILABLE

    # -- Behaviour -----------------------------------------------------------

    def rename(self, *, title: MediaTitle, now: datetime) -> None:
        """Change the human-readable title.

        Renaming an archived item is refused: archived state is immutable.

        Args:
            title: The new title.
            now: Current time.

        Raises:
            InvalidMediaTransitionError: If the item is archived.
        """
        if self._status is MediaStatus.ARCHIVED:
            raise InvalidMediaTransitionError(self._status, self._status)
        if title == self._title:
            return
        self._title = title
        self._touch(now)
        self.record_event(
            MediaRenamed(occurred_at=self._updated_at, media_id=self.id.value, title=str(title))
        )

    def mark_available(
        self,
        *,
        storage_key: StorageKey,
        size: FileSize,
        checksum: Fingerprint | None = None,
        now: datetime,
    ) -> None:
        """Record that the bytes are stored locally.

        Args:
            storage_key: Location relative to the library root.
            size: Size of the stored artefact.
            checksum: Optional integrity hash.
            now: Current time.

        Raises:
            InvalidMediaTransitionError: If the item cannot become available.
        """
        self._transition_to(MediaStatus.AVAILABLE, now=now)
        self._storage_key = storage_key
        self._size = size
        self._checksum = checksum
        self._failure_reason = None
        self.record_event(
            MediaBecameAvailable(
                occurred_at=self._updated_at,
                media_id=self.id.value,
                storage_key=str(storage_key),
                size_bytes=size.bytes_,
            )
        )

    def mark_failed(self, *, reason: str, now: datetime) -> None:
        """Record that acquiring the content failed.

        Args:
            reason: Operator-facing explanation; truncated to a sane length so
                a provider's stack trace can never bloat a row.
            now: Current time.

        Raises:
            InvalidMediaTransitionError: If the item cannot fail from its
                current state.
        """
        self._transition_to(MediaStatus.FAILED, now=now)
        self._failure_reason = reason.strip()[:MAX_FAILURE_REASON_LENGTH] or "unknown error"
        self.record_event(
            MediaFailed(
                occurred_at=self._updated_at,
                media_id=self.id.value,
                reason=self._failure_reason,
            )
        )

    def reset_to_pending(self, *, now: datetime) -> None:
        """Return a failed item to :attr:`MediaStatus.PENDING` for another try.

        Args:
            now: Current time.

        Raises:
            InvalidMediaTransitionError: If the item is not in ``FAILED``.
        """
        self._transition_to(MediaStatus.PENDING, now=now)
        self._failure_reason = None

    def archive(self, *, now: datetime) -> None:
        """Retire the item from the active library. This is terminal.

        Args:
            now: Current time.

        Raises:
            InvalidMediaTransitionError: If the item is already archived.
        """
        self._transition_to(MediaStatus.ARCHIVED, now=now)
        self.record_event(MediaArchived(occurred_at=self._updated_at, media_id=self.id.value))

    # -- Internals -----------------------------------------------------------

    def _transition_to(self, target: MediaStatus, *, now: datetime) -> None:
        """Move to ``target`` if the lifecycle contract allows it."""
        if target not in ALLOWED_TRANSITIONS[self._status]:
            raise InvalidMediaTransitionError(self._status, target)
        self._status = target
        self._touch(now)

    def _touch(self, now: datetime) -> None:
        """Stamp the aggregate as modified at ``now``."""
        self._updated_at = ensure_utc(now, field_name="now")
