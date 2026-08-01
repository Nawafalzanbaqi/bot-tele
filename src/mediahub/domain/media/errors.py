"""Failures the media aggregate can express.

Each error derives from a category in
:mod:`mediahub.domain.common.errors`, so outer layers can handle whole
categories (``InvariantViolationError`` -> 422, ``ConflictError`` -> 409)
without knowing every concrete type.
"""

from __future__ import annotations

from typing import ClassVar

from mediahub.domain.common.errors import (
    ConflictError,
    EntityNotFoundError,
    InvalidStateTransitionError,
    InvariantViolationError,
)


class InvalidMediaIdError(InvariantViolationError):
    """The supplied string is not a valid media identifier."""

    code: ClassVar[str] = "invalid_media_id"

    def __init__(self, raw: str) -> None:
        """Initialise the error from the rejected raw value."""
        super().__init__(f"'{raw}' is not a valid media identifier (expected a UUID).")
        self.raw = raw


class InvalidMediaTitleError(InvariantViolationError):
    """The title is empty or longer than the aggregate allows."""

    code: ClassVar[str] = "invalid_media_title"


class InvalidSourceUrlError(InvariantViolationError):
    """The source URL is malformed or uses an unsupported scheme."""

    code: ClassVar[str] = "invalid_source_url"

    def __init__(self, raw: str, reason: str) -> None:
        """Initialise the error from the rejected URL and the reason."""
        super().__init__(f"'{raw}' is not a usable source URL: {reason}")
        self.raw = raw
        self.reason = reason


class InvalidStorageKeyError(InvariantViolationError):
    """The storage key is absolute, empty or attempts path traversal."""

    code: ClassVar[str] = "invalid_storage_key"

    def __init__(self, raw: str, reason: str) -> None:
        """Initialise the error from the rejected key and the reason."""
        super().__init__(f"'{raw}' is not a valid storage key: {reason}")
        self.raw = raw
        self.reason = reason


class InvalidFileSizeError(InvariantViolationError):
    """A byte count was negative."""

    code: ClassVar[str] = "invalid_file_size"


class MediaNotFoundError(EntityNotFoundError):
    """No media item exists for the requested identifier."""

    code: ClassVar[str] = "media_not_found"

    def __init__(self, identifier: object) -> None:
        """Initialise the error from the identifier that was looked up."""
        super().__init__("Media item", identifier)


class DuplicateMediaError(ConflictError):
    """A media item is already catalogued for this source URL."""

    code: ClassVar[str] = "duplicate_media"

    def __init__(self, source_url: object) -> None:
        """Initialise the error from the conflicting source URL."""
        super().__init__(f"A media item is already registered for '{source_url}'.")
        self.source_url = source_url


class InvalidMediaTransitionError(InvalidStateTransitionError):
    """The requested lifecycle change is not allowed from the current state."""

    code: ClassVar[str] = "invalid_media_transition"

    def __init__(self, current: object, target: object) -> None:
        """Initialise the error from the current and requested states."""
        super().__init__("Media item", current, target)
