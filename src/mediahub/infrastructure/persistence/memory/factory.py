"""Factory that produces in-memory units of work.

Satisfies :class:`~mediahub.application.common.unit_of_work.UnitOfWorkFactory`.
All units of work created by one factory share a single
:class:`~mediahub.infrastructure.persistence.memory.database.InMemoryDatabase`,
which is what makes data written by one use case visible to the next.
"""

from __future__ import annotations

from mediahub.infrastructure.persistence.memory.database import InMemoryDatabase
from mediahub.infrastructure.persistence.memory.unit_of_work import InMemoryUnitOfWork


class InMemoryUnitOfWorkFactory:
    """Creates snapshot-isolated units of work over one shared store."""

    __slots__ = ("_database",)

    def __init__(self, database: InMemoryDatabase | None = None) -> None:
        """Use ``database``, or create a fresh empty store."""
        self._database = database or InMemoryDatabase()

    @property
    def database(self) -> InMemoryDatabase:
        """Return the shared store, so tests can seed or clear it."""
        return self._database

    def __call__(self) -> InMemoryUnitOfWork:
        """Return a new, unopened unit of work."""
        return InMemoryUnitOfWork(self._database)
