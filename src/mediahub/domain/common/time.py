"""Time helpers for the domain.

The domain never reads the system clock - the current time always arrives as an
argument from the application layer (see
:class:`~mediahub.application.common.ports.Clock`). What the domain *does*
enforce is that every timestamp it stores is timezone-aware and expressed in
UTC, so ordering and comparisons are unambiguous forever.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mediahub.domain.common.errors import InvariantViolationError


def ensure_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    """Return ``value`` converted to UTC, rejecting naive datetimes.

    Args:
        value: The timestamp to validate.
        field_name: Name used in the error message to aid debugging.

    Returns:
        The same instant expressed in UTC.

    Raises:
        InvariantViolationError: If ``value`` carries no timezone information.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        message = f"{field_name} must be timezone-aware; received a naive datetime."
        raise InvariantViolationError(message)
    return value.astimezone(UTC)
