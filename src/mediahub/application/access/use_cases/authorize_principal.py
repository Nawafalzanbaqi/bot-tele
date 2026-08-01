"""Use case: decide whether a caller may perform an action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.access.ports import AuditEvent, AuditOutcome, Principal
from mediahub.application.common.errors import PermissionDeniedError
from mediahub.application.common.use_case import Query
from mediahub.domain.access.value_objects import ExternalIdentity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.access.ports import AuditSink
    from mediahub.application.common.ports import Clock
    from mediahub.domain.access.enums import Action
    from mediahub.domain.access.policies import AllowListPolicy, AuthorizationPolicy


@dataclass(frozen=True, slots=True)
class AuthorizeQuery(Query):
    """Ask whether an external caller may perform an action.

    Attributes:
        scheme: The system that authenticated the caller, e.g. ``telegram``.
        external_id: That system's identifier for the caller.
        action: What the caller is attempting.
        display_name: Optional human label, for messages and audit entries.
    """

    scheme: str
    external_id: str
    action: Action
    display_name: str | None = None


class AuthorizePrincipal:
    """Resolves an external identity to a principal and checks its permission.

    Every attempt is audited - allowed as well as denied. Recording only
    failures produces an audit log that cannot answer "who asked for this?",
    which is the question that actually gets asked.
    """

    def __init__(
        self,
        *,
        allow_list: AllowListPolicy,
        authorization: AuthorizationPolicy,
        audit: AuditSink,
        clock: Clock,
    ) -> None:
        """Wire the use case to its policies and ports."""
        self._allow_list = allow_list
        self._authorization = authorization
        self._audit = audit
        self._clock = clock

    async def execute(self, request: AuthorizeQuery) -> Principal:
        """Return the authorised principal, or refuse.

        Args:
            request: Who is asking, and for what.

        Returns:
            The principal, carrying the role the allow-list assigned.

        Raises:
            PermissionDeniedError: If the identity is unknown to the allow-list
                or its role does not permit the action. The two cases produce
                the same message on purpose: telling an unknown caller that
                they are unknown - rather than unauthorised - is free
                reconnaissance.
        """
        identity = ExternalIdentity(scheme=request.scheme, external_id=request.external_id)
        role = self._allow_list.role_for(identity)

        if role is None:
            await self._deny(identity, request.action, "identity is not on the allow list")
        elif not self._authorization.permits(role, request.action):
            await self._deny(identity, request.action, f"role '{role.value}' may not do this")
        else:
            await self._audit.record(
                AuditEvent(
                    occurred_at=self._clock.now(),
                    actor=str(identity),
                    action=request.action.value,
                    outcome=AuditOutcome.ALLOWED,
                )
            )
            return Principal(identity=str(identity), role=role, display_name=request.display_name)

        raise AssertionError  # pragma: no cover - _deny always raises

    async def _deny(self, identity: ExternalIdentity, action: Action, reason: str) -> None:
        """Audit and refuse, giving the caller no detail about why."""
        await self._audit.record(
            AuditEvent(
                occurred_at=self._clock.now(),
                actor=str(identity),
                action=action.value,
                outcome=AuditOutcome.DENIED,
                reason=reason,
            )
        )
        logger.bind(actor=str(identity), action=action.value).warning("Access denied: {}", reason)
        message = "You are not authorised to use this service."
        raise PermissionDeniedError(message)
