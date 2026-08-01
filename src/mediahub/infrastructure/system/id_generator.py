"""Identifier generation adapter.

Implements :class:`~mediahub.application.common.ports.UuidGenerator`.

UUID4 is chosen for its independence: any process can mint an identifier
without coordinating with the database, which keeps aggregate creation a pure
in-memory operation. If index locality ever becomes a measured problem, UUIDv7
can replace it here without touching a single use case.
"""

from __future__ import annotations

from uuid import UUID, uuid4


class Uuid4Generator:
    """Produces random (version 4) UUIDs."""

    def new_uuid(self) -> UUID:
        """Return a fresh, unique identifier."""
        return uuid4()
