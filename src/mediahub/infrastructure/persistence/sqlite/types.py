"""Column types that behave the same on SQLite as on PostgreSQL.

SQLite has no native timezone-aware timestamp. SQLAlchemy's
``DateTime(timezone=True)`` therefore stores whatever it is given and hands back
a **naive** ``datetime`` on load. The mapper passes that to an aggregate
constructor, which calls ``ensure_utc`` - and raises ``InvariantViolationError``
on every single row, at load time, far from the cause.

The domain rule is right; the adapter was lying to it. :class:`UtcDateTime`
fixes it in the one place that can: values go in as ISO-8601 UTC text and come
back with ``UTC`` re-attached, so a round trip is lossless and the aggregate
sees exactly what it stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.types import TEXT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.engine.interfaces import Dialect


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware and always UTC.

    On PostgreSQL this delegates to ``TIMESTAMP WITH TIME ZONE`` and costs
    nothing. On SQLite it stores ISO-8601 text, which sorts correctly as a
    string precisely because the format is fixed-width UTC - so ``ORDER BY
    created_at`` still means what it says.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        """Return the underlying type this dialect should use."""
        if dialect.name == "sqlite":
            return dialect.type_descriptor(TEXT())
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        """Normalise a value on its way into the database.

        Raises:
            ValueError: If a naive datetime is stored. Guessing a zone here is
                how "off by three hours" bugs are born; the caller must say.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            message = "refusing to store a naive datetime; attach a timezone first"
            raise ValueError(message)
        moment = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return moment.isoformat()
        return moment

    def process_result_value(self, value: Any, _dialect: Dialect) -> datetime | None:
        """Re-attach UTC on the way out, whatever the driver returned.

        Deliberately dialect-independent: PostgreSQL hands back an aware
        ``datetime``, SQLite a string, and a future driver might do either. All
        three are normalised to the same thing rather than branching on a name.
        """
        if value is None:
            return None
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        message = f"cannot interpret {type(value).__name__} as a timestamp"  # pragma: no cover
        raise TypeError(message)  # pragma: no cover
