"""Pagination primitives bound every query."""

from __future__ import annotations

import pytest

from mediahub.domain.common.errors import InvariantViolationError
from mediahub.domain.common.pagination import Page, PageRequest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("limit", [0, -1, PageRequest.MAX_LIMIT + 1])
def test_limit_is_bounded(limit: int) -> None:
    with pytest.raises(InvariantViolationError):
        PageRequest(limit=limit)


def test_offset_cannot_be_negative() -> None:
    with pytest.raises(InvariantViolationError):
        PageRequest(offset=-1)


def test_has_next_reflects_remaining_items() -> None:
    page: Page[int] = Page(items=[1, 2], total=5, limit=2, offset=0)

    assert page.has_next
    assert page.count == 2


def test_last_page_has_no_next() -> None:
    page: Page[int] = Page(items=[5], total=5, limit=2, offset=4)

    assert not page.has_next


def test_empty_page_echoes_the_request() -> None:
    page: Page[int] = Page.empty(PageRequest(limit=10, offset=20))

    assert page.total == 0
    assert page.limit == 10
    assert page.offset == 20
    assert not page.has_next
