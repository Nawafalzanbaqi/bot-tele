"""The domain error hierarchy.

Every failure the domain can express derives from :class:`DomainError`. Errors
carry a stable, machine-readable ``code`` so that outer layers can translate
them without pattern-matching on exception types or message strings; the
presentation layer maps codes to HTTP statuses in
:mod:`mediahub.presentation.api.errors`.

Design rules:

* Messages are built *inside* the exception class, never at the raise site.
  Call sites therefore read ``raise DuplicateMediaError(url)``.
* Errors are part of the public contract of the domain. Renaming a ``code`` is
  a breaking change; adding a subclass is not.
"""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    """Base class for all errors raised by the domain layer.

    Attributes:
        code: Stable, machine-readable identifier for this failure category.
        message: Human-readable description, safe to show to API clients.
    """

    code: ClassVar[str] = "domain_error"

    def __init__(self, message: str) -> None:
        """Initialise the error with a human-readable message."""
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and test failures."""
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class InvariantViolationError(DomainError):
    """A value or entity was asked to enter a state its rules forbid.

    Raised by value-object constructors and entity guards. Signals a caller
    mistake (bad input), not an infrastructure problem.
    """

    code: ClassVar[str] = "invariant_violation"


class EntityNotFoundError(DomainError):
    """A referenced entity does not exist.

    Attributes:
        entity_type: Human-readable name of the missing aggregate.
        identifier: The identifier that was looked up.
    """

    code: ClassVar[str] = "entity_not_found"

    def __init__(self, entity_type: str, identifier: object) -> None:
        """Initialise the error from the aggregate name and its identifier."""
        super().__init__(f"{entity_type} '{identifier}' does not exist.")
        self.entity_type = entity_type
        self.identifier = identifier


class ConflictError(DomainError):
    """The requested change collides with the current state of the system.

    Typical causes are uniqueness violations and concurrent modifications.
    """

    code: ClassVar[str] = "conflict"


class InvalidStateTransitionError(DomainError):
    """An aggregate was asked to move between two incompatible states.

    Attributes:
        entity_type: Human-readable name of the aggregate.
        current: The state the aggregate is in.
        target: The state that was requested.
    """

    code: ClassVar[str] = "invalid_state_transition"

    def __init__(self, entity_type: str, current: object, target: object) -> None:
        """Initialise the error from the aggregate name and both states."""
        super().__init__(f"{entity_type} cannot move from '{current}' to '{target}'.")
        self.entity_type = entity_type
        self.current = current
        self.target = target
