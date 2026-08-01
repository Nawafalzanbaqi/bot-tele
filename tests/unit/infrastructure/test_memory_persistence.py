"""The in-memory unit of work really is transactional.

If these semantics drift from the SQL adapter's, every test that relies on them
starts lying - which is why they are pinned here explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from mediahub.domain.common.pagination import PageRequest
from mediahub.domain.media.entities import MediaItem
from mediahub.domain.media.enums import MediaStatus, MediaType
from mediahub.domain.media.repository import MediaFilter
from mediahub.domain.media.value_objects import MediaId, MediaTitle, SourceUrl
from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
from mediahub.infrastructure.persistence.memory.unit_of_work import NotStartedError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_item(index: int) -> MediaItem:
    return MediaItem.register(
        media_id=MediaId(UUID(int=index)),
        source_url=SourceUrl(f"https://example.com/{index}.mp4"),
        title=MediaTitle(f"Item {index}"),
        media_type=MediaType.VIDEO,
        now=NOW,
    )


async def test_commit_makes_writes_visible_to_later_transactions() -> None:
    factory = InMemoryUnitOfWorkFactory()

    async with factory() as uow:
        await uow.media.add(make_item(1))
        await uow.commit()

    async with factory() as uow:
        assert await uow.media.get(MediaId(UUID(int=1))) is not None


async def test_uncommitted_writes_are_discarded() -> None:
    factory = InMemoryUnitOfWorkFactory()

    async with factory() as uow:
        await uow.media.add(make_item(1))
        # No commit.

    async with factory() as uow:
        assert await uow.media.get(MediaId(UUID(int=1))) is None


async def add_then_fail(factory: InMemoryUnitOfWorkFactory) -> None:
    """Stage a write, then blow up before committing."""
    async with factory() as uow:
        await uow.media.add(make_item(1))
        message = "boom"
        raise RuntimeError(message)


async def test_an_exception_rolls_everything_back() -> None:
    factory = InMemoryUnitOfWorkFactory()

    with pytest.raises(RuntimeError):
        await add_then_fail(factory)

    async with factory() as uow:
        assert await uow.media.get(MediaId(UUID(int=1))) is None


async def test_repositories_require_entering_the_context() -> None:
    uow = InMemoryUnitOfWorkFactory()()

    with pytest.raises(NotStartedError):
        _ = uow.media


async def test_listing_is_newest_first_and_windowed() -> None:
    factory = InMemoryUnitOfWorkFactory()

    async with factory() as uow:
        for index in range(1, 4):
            item = MediaItem.register(
                media_id=MediaId(UUID(int=index)),
                source_url=SourceUrl(f"https://example.com/{index}.mp4"),
                title=MediaTitle(f"Item {index}"),
                media_type=MediaType.VIDEO,
                now=NOW.replace(minute=index),
            )
            await uow.media.add(item)
        await uow.commit()

    async with factory() as uow:
        page = await uow.media.find_many(
            filters=MediaFilter(status=MediaStatus.PENDING), page=PageRequest(limit=2)
        )

    assert page.total == 3
    assert page.has_next
    assert [str(item.title) for item in page.items] == ["Item 3", "Item 2"]
