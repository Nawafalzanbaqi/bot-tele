"""Event publisher that writes each event to the structured log.

Implements :class:`~mediahub.application.common.ports.EventPublisher`.

Events are published *after* the transaction commits, so every line here
describes something that is already durable. Publishing never raises: a
consumer failing must not undo work that succeeded, so failures are logged and
swallowed. Once a real broker is introduced, at-least-once delivery should be
handled with a transactional outbox rather than by making this call fallible.
"""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.domain.common.events import DomainEvent


class LoggingEventPublisher:
    """Records domain events as structured log entries."""

    async def publish(self, events: Sequence[DomainEvent]) -> None:
        """Log every event, isolating failures to a single event.

        Args:
            events: The events drained from an aggregate after commit.
        """
        for event in events:
            try:
                logger.bind(
                    event_name=event.name,
                    event_id=str(event.event_id),
                    occurred_at=event.occurred_at.isoformat(),
                    payload=_payload_of(event),
                ).info("Domain event published")
            except Exception:
                # A failing consumer must never undo work that already committed.
                logger.exception("Failed to publish domain event {}", event.name)


_ENVELOPE_FIELDS = frozenset({"occurred_at", "event_id"})


def _payload_of(event: DomainEvent) -> dict[str, Any]:
    """Return the event's own fields, minus the envelope, as plain data."""
    return {
        field.name: _as_plain_value(getattr(event, field.name))
        for field in fields(event)
        if field.name not in _ENVELOPE_FIELDS
    }


def _as_plain_value(value: object) -> Any:
    """Render a field so a JSON sink can serialise it without custom encoders."""
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)
