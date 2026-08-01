"""Ambient capabilities a use case may depend on.

These are the small, non-domain services that would otherwise be called as
global functions - the clock, the id generator, the event bus. Declaring them
as ports has two payoffs:

* tests inject a frozen clock and a deterministic id sequence, so assertions
  are exact instead of approximate;
* production can swap an implementation (a broker-backed publisher, say)
  without touching a single use case.

Adapters live in :mod:`mediahub.infrastructure`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from mediahub.domain.common.events import DomainEvent


class Clock(Protocol):
    """Source of the current time.

    The domain never reads the clock itself; use cases call this port and pass
    the result into aggregates as an argument.
    """

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class UuidGenerator(Protocol):
    """Source of new identifiers.

    Injected rather than called directly so that tests can produce a
    predictable sequence of ids.
    """

    def new_uuid(self) -> UUID:
        """Return a fresh, unique identifier."""
        ...


class EventPublisher(Protocol):
    """Sink for domain events drained after a successful commit.

    Publishing happens *after* the transaction commits: an event must only
    describe a fact that is durable. Implementations must not raise for a
    single bad consumer - a failed subscriber cannot be allowed to undo work
    that already succeeded.
    """

    async def publish(self, events: Sequence[DomainEvent]) -> None:
        """Deliver ``events`` to every interested consumer."""
        ...
