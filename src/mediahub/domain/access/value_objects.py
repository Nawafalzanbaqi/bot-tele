"""Immutable values identifying a principal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from mediahub.domain.common.errors import InvariantViolationError
from mediahub.domain.common.value_object import ValueObject


class InvalidIdentityError(InvariantViolationError):
    """An external identity is malformed."""

    code: ClassVar[str] = "invalid_identity"


@dataclass(frozen=True, slots=True)
class ExternalIdentity(ValueObject):
    """How a principal is known to one outside system.

    Keeping the scheme alongside the identifier is what stops a Telegram user
    id from ever being confused with an API key or a future OIDC subject - they
    live in different namespaces and must not collide.

    Attributes:
        scheme: The issuing system, e.g. ``telegram``.
        external_id: The identifier that system uses, as text.
    """

    scheme: str
    external_id: str

    MAX_LENGTH: ClassVar[int] = 64

    def __post_init__(self) -> None:
        """Normalise and validate both halves."""
        scheme = (self.scheme or "").strip().lower()
        external_id = (self.external_id or "").strip()
        if not scheme or not external_id:
            message = "an external identity needs both a scheme and an identifier"
            raise InvalidIdentityError(message)
        if len(scheme) > self.MAX_LENGTH or len(external_id) > self.MAX_LENGTH:
            message = f"identity parts must be at most {self.MAX_LENGTH} characters"
            raise InvalidIdentityError(message)
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "external_id", external_id)

    def __str__(self) -> str:
        """Return the ``scheme:id`` form used in logs and audit entries."""
        return f"{self.scheme}:{self.external_id}"
