"""Pagination primitives shared by every repository port.

These types live in the domain because repository *interfaces* are declared
here; keeping them framework-free means the same contract works for SQL,
in-memory and any future adapter, and the presentation layer only has to map
them to its own request/response schemas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from mediahub.domain.common.errors import InvariantViolationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class PageRequest:
    """A bounded window over an ordered result set.

    Attributes:
        limit: Maximum number of items to return.
        offset: Number of items to skip.
    """

    limit: int = 50
    offset: int = 0

    MAX_LIMIT: ClassVar[int] = 200
    """Upper bound enforced for every caller, protecting adapters from
    unbounded scans."""

    def __post_init__(self) -> None:
        """Validate the window, rejecting values an adapter could not honour."""
        if self.limit < 1 or self.limit > self.MAX_LIMIT:
            message = f"Page limit must be between 1 and {self.MAX_LIMIT}, got {self.limit}."
            raise InvariantViolationError(message)
        if self.offset < 0:
            message = f"Page offset must be >= 0, got {self.offset}."
            raise InvariantViolationError(message)


@dataclass(frozen=True, slots=True)
class Page[TItem]:
    """One page of results plus the information needed to request the next.

    Type parameters:
        TItem: The element type carried by this page.

    Attributes:
        items: The items in this window, in repository order.
        total: Total number of items matching the query, ignoring the window.
        limit: The limit that produced this page.
        offset: The offset that produced this page.
    """

    items: Sequence[TItem]
    total: int
    limit: int
    offset: int

    @classmethod
    def empty(cls, request: PageRequest) -> Page[TItem]:
        """Build an empty page that echoes the requested window."""
        return cls(items=(), total=0, limit=request.limit, offset=request.offset)

    @property
    def has_next(self) -> bool:
        """Return whether more items exist after this window."""
        return self.offset + len(self.items) < self.total

    @property
    def count(self) -> int:
        """Return the number of items in this window."""
        return len(self.items)
