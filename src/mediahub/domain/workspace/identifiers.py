"""The identity of a workspace lease.

A lease id is **generated**, never derived from anything a provider, a user or a
filename supplied. That matters more here than in most contexts because the id
becomes a directory name: an identifier with a restricted alphabet cannot carry
a traversal sequence, a shell metacharacter or a reserved device name into the
filesystem, whatever else goes wrong upstream
(``docs/architecture/14-security-architecture.md`` §14.4).

The domain validates the *shape* only. Producing the random value is an
infrastructure concern, exactly as computing a hash is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from mediahub.domain.common.errors import InvariantViolationError
from mediahub.domain.common.value_object import ValueObject

LEASE_ID_LENGTH: Final[int] = 32
"""Characters in a lease id - the hexadecimal form of a 128-bit value."""

_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class InvalidLeaseIdError(InvariantViolationError):
    """A lease identifier is not the generated, fixed-width value it must be."""

    code: ClassVar[str] = "invalid_lease_id"


@dataclass(frozen=True, slots=True)
class LeaseId(ValueObject):
    """A lease's identifier, as lower-case hexadecimal.

    Attributes:
        value: Exactly :data:`LEASE_ID_LENGTH` hexadecimal characters.
    """

    value: str

    def __post_init__(self) -> None:
        """Normalise to lower case and refuse anything that is not hex."""
        normalised = (self.value or "").strip().lower()
        if len(normalised) != LEASE_ID_LENGTH:
            message = f"A lease id must be {LEASE_ID_LENGTH} characters, got {len(normalised)}."
            raise InvalidLeaseIdError(message)
        if set(normalised) - _HEX_DIGITS:
            message = "A lease id must be hexadecimal."
            raise InvalidLeaseIdError(message)
        object.__setattr__(self, "value", normalised)

    def __str__(self) -> str:
        """Return the identifier as text, which is also its directory name."""
        return self.value
