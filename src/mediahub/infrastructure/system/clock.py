"""System clock adapter.

Implements :class:`~mediahub.application.common.ports.Clock`. Always returns an
aware UTC datetime - the domain rejects naive timestamps, so a local-time clock
would fail fast rather than silently store ambiguous data.
"""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Reads the real wall clock in UTC."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        return datetime.now(UTC)
