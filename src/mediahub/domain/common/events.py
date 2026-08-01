"""Domain events.

A domain event records a fact that has already happened inside an aggregate:
`MediaRegistered`, `DownloadFailed`. Events are immutable, keyword-only frozen
dataclasses, and they never contain infrastructure types.

Timestamps are *passed in* rather than read from the system clock, so aggregates
stay deterministic and trivially testable; the application layer supplies the
current time through the :class:`~mediahub.application.common.ports.Clock` port.

Aggregates record events via
:meth:`~mediahub.domain.common.entity.AggregateRoot.record_event`; the
application layer drains them after a successful commit and hands them to an
:class:`~mediahub.application.common.ports.EventPublisher`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base class for all domain events.

    Attributes:
        occurred_at: Timezone-aware moment the fact became true.
        event_id: Unique identifier, useful for idempotent consumers.
    """

    occurred_at: datetime
    event_id: UUID = field(default_factory=uuid4)

    @property
    def name(self) -> str:
        """Return the event's type name, used as its wire/log identifier."""
        return type(self).__name__
