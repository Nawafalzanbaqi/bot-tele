"""Ports and DTOs for the access context.

Refusals raise the shared
:class:`~mediahub.application.common.errors.PermissionDeniedError` rather than
a new hierarchy: the HTTP layer already maps it to ``403``, and a parallel set
of access errors would have to be mapped again in every interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from datetime import datetime

    from mediahub.domain.access.enums import Role


class AuditOutcome(StrEnum):
    """Whether an audited attempt succeeded.

    Attributes:
        ALLOWED: The action proceeded.
        DENIED: The action was refused by policy.
    """

    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller, as the rest of the application sees it.

    Attributes:
        identity: How the caller is known to the system that authenticated it.
        role: What the caller is permitted to be.
        display_name: A human label for messages and logs. Never trusted for
            anything but display - it is supplied by the outside world.
    """

    identity: str
    role: Role
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One security-relevant attempt, recorded whatever the outcome.

    Attributes:
        occurred_at: When the attempt happened (UTC).
        actor: The identity that attempted it, as ``scheme:id``.
        action: What was attempted.
        outcome: Whether it was allowed.
        reason: Why it was refused, when it was.
        detail: Extra context. **Must never contain secrets, file paths or
            stack traces** - the audit log is read by people and kept for a
            year.
    """

    occurred_at: datetime
    actor: str
    action: str
    outcome: AuditOutcome
    reason: str | None = None
    detail: Mapping[str, str] = field(default_factory=dict)


class AuditSink(Protocol):
    """Append-only destination for audit events.

    Implementations must not raise: an audit failure is bad, but failing the
    user's request because the audit failed is worse. They log and continue.
    """

    async def record(self, event: AuditEvent) -> None:
        """Append one event."""
        ...
