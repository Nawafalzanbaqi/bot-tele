"""Entity and aggregate-root bases.

An *entity* has a stable identity: two instances are equal when their
identifiers match, regardless of their other attributes. An *aggregate root* is
the single entry point to a cluster of objects that change together - it owns
its invariants and is the only thing a repository stores or loads.

Aggregate roots also collect domain events. They are recorded while the
aggregate mutates and drained by the application layer once the surrounding
transaction commits, which keeps side effects out of the domain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.domain.common.events import DomainEvent


class Entity[TIdentifier]:
    """An object distinguished by identity rather than by attribute values.

    Type parameters:
        TIdentifier: The value-object type used as this entity's identifier.
    """

    __slots__ = ("_identifier",)

    def __init__(self, identifier: TIdentifier) -> None:
        """Bind the entity to its immutable identifier."""
        self._identifier = identifier

    @property
    def id(self) -> TIdentifier:
        """Return the entity's identifier. It never changes after creation."""
        return self._identifier

    def __eq__(self, other: object) -> bool:
        """Compare by concrete type and identifier only."""
        if other is self:
            return True
        if not isinstance(other, Entity):
            return NotImplemented
        return type(other) is type(self) and other.id == self._identifier

    def __hash__(self) -> int:
        """Hash by concrete type and identifier, mirroring :meth:`__eq__`."""
        return hash((type(self).__name__, self._identifier))

    def __repr__(self) -> str:
        """Return a short, log-friendly representation."""
        return f"{type(self).__name__}(id={self._identifier!r})"


class AggregateRoot[TIdentifier](Entity[TIdentifier]):
    """An entity that owns a consistency boundary and records domain events.

    Only aggregate roots are persisted through repositories. Everything inside
    the boundary is reached through the root, never referenced directly from
    the outside.
    """

    __slots__ = ("_events",)

    def __init__(self, identifier: TIdentifier) -> None:
        """Initialise the root with an empty event buffer."""
        super().__init__(identifier)
        self._events: list[DomainEvent] = []

    @property
    def events(self) -> Sequence[DomainEvent]:
        """Return the events recorded so far without clearing them."""
        return tuple(self._events)

    def record_event(self, event: DomainEvent) -> None:
        """Append a domain event describing a change that just happened."""
        self._events.append(event)

    def pull_events(self) -> Sequence[DomainEvent]:
        """Return and clear the recorded events.

        Called by the application layer after a successful commit. Draining is
        destructive so that an event can never be published twice.
        """
        drained = tuple(self._events)
        self._events.clear()
        return drained
