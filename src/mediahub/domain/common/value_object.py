"""Base class for value objects.

A value object is defined by its attributes rather than by an identity: two
instances holding the same data are the same value. In MediaHub every value
object is a frozen, slotted dataclass that validates itself on construction,
which means an invalid value can never exist in memory - if you hold a
:class:`~mediahub.domain.media.value_objects.SourceUrl`, it *is* a usable URL.

Subclasses should:

* declare ``@dataclass(frozen=True, slots=True)``;
* validate in ``__post_init__`` and raise an
  :class:`~mediahub.domain.common.errors.InvariantViolationError` subclass;
* expose derived data as properties rather than as stored fields.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValueObject:
    """Marker base for immutable, self-validating domain values.

    It deliberately carries no fields and no behaviour: its purpose is to make
    intent explicit in type signatures and to give the architecture tests a
    single symbol to reason about.
    """
