"""Who is allowed in, and what they may do once inside.

Two policies, both pure:

* :class:`AllowListPolicy` answers "is this identity known?" - a closed list,
  because MediaHub is a household appliance and everyone else is an attacker.
* :class:`AuthorizationPolicy` answers "may this role do this?" - a table, so
  adding an action is a decision made in one place rather than an ``if`` in a
  handler.

Deny is the default in both. An identity that is not listed gets nothing, and a
role/action pair that is not listed is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from mediahub.domain.access.enums import Action, Role

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from mediahub.domain.access.value_objects import ExternalIdentity

_PERMISSIONS: Final[Mapping[Role, frozenset[Action]]] = {
    Role.OWNER: frozenset(Action),
    Role.MEMBER: frozenset(
        {
            Action.SUBMIT_SOURCE,
            Action.CANCEL_ACQUISITION,
            Action.VIEW_HISTORY,
            Action.VIEW_SETTINGS,
        }
    ),
    Role.READONLY: frozenset({Action.VIEW_HISTORY, Action.VIEW_SETTINGS}),
}
"""The complete permission table. Adding an action means editing one line."""


@dataclass(frozen=True, slots=True)
class AllowListPolicy:
    """Maps known external identities to the role they carry.

    Attributes:
        roles: Identity string (``scheme:id``) to role. Anything absent is
            refused; there is no wildcard and no "default role".
    """

    roles: Mapping[str, Role] = field(default_factory=dict)

    @classmethod
    def from_ids(
        cls,
        *,
        scheme: str,
        owner_ids: tuple[str, ...] = (),
        member_ids: tuple[str, ...] = (),
        readonly_ids: tuple[str, ...] = (),
    ) -> AllowListPolicy:
        """Build a policy from the id lists an operator configures.

        Later entries do not override earlier ones: an id listed as both owner
        and member stays an owner, because the more permissive intent was
        clearly deliberate and silently downgrading it would be surprising.
        """
        roles: dict[str, Role] = {}
        for ids, role in (
            (owner_ids, Role.OWNER),
            (member_ids, Role.MEMBER),
            (readonly_ids, Role.READONLY),
        ):
            for raw in ids:
                key = f"{scheme.strip().lower()}:{raw.strip()}"
                roles.setdefault(key, role)
        return cls(roles=roles)

    @property
    def is_empty(self) -> bool:
        """Return whether nobody is allowed in.

        An empty allow-list is a valid, if useless, configuration. It is worth
        asking about because it is also what a typo produces.
        """
        return not self.roles

    def role_for(self, identity: ExternalIdentity) -> Role | None:
        """Return the role this identity carries, or ``None`` if unknown."""
        return self.roles.get(str(identity))


@dataclass(frozen=True, slots=True)
class AuthorizationPolicy:
    """Decides whether a role may perform an action."""

    def permits(self, role: Role, action: Action) -> bool:
        """Return whether ``role`` may perform ``action``."""
        return action in _PERMISSIONS.get(role, frozenset())
