"""Audit sink that writes to the structured log.

Implements :class:`~mediahub.application.access.ports.AuditSink`.

Audit entries are kept separate from operational logs by a marker field, so
"who did what" can be extracted without wading through progress lines. A
durable, queryable audit table arrives with the persistence rework; the
contract does not change when it does.

The sink never raises. An audit failure is bad; failing a user's request
*because* the audit failed is worse.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.access.ports import AuditOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.access.ports import AuditEvent

AUDIT_MARKER: Final[str] = "audit"


class LoggingAuditSink:
    """Records audit events as tagged structured log entries."""

    __slots__ = ()

    async def record(self, event: AuditEvent) -> None:
        """Append one event, swallowing any failure to write it."""
        try:
            bound = logger.bind(
                channel=AUDIT_MARKER,
                actor=event.actor,
                action=event.action,
                outcome=event.outcome.value,
                occurred_at=event.occurred_at.isoformat(),
                reason=event.reason,
                **{f"detail_{key}": value for key, value in event.detail.items()},
            )
            if event.outcome is AuditOutcome.DENIED:
                bound.warning("Denied {} for {}", event.action, event.actor)
            else:
                bound.info("Allowed {} for {}", event.action, event.actor)
        except Exception:
            logger.exception("Failed to record an audit event")
