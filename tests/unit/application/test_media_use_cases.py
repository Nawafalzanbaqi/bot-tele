"""Media use cases orchestrate the domain and publish what happened."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from mediahub.application.media.dto import (
    ArchiveMediaCommand,
    GetMediaQuery,
    ListMediaQuery,
    RegisterMediaCommand,
)
from mediahub.application.media.use_cases.archive_media import ArchiveMedia
from mediahub.application.media.use_cases.get_media import GetMedia
from mediahub.application.media.use_cases.list_media import ListMedia
from mediahub.application.media.use_cases.register_media import RegisterMedia
from mediahub.domain.common.pagination import PageRequest
from mediahub.domain.media.enums import MediaStatus, MediaType
from mediahub.domain.media.errors import (
    DuplicateMediaError,
    InvalidSourceUrlError,
    MediaNotFoundError,
)

if TYPE_CHECKING:
    from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
    from tests.conftest import (
        FrozenClock,
        RecordingEventPublisher,
        SequentialUuidGenerator,
    )

pytestmark = pytest.mark.unit


@pytest.fixture
def register(
    unit_of_work: InMemoryUnitOfWorkFactory,
    clock: FrozenClock,
    uuid_generator: SequentialUuidGenerator,
    event_publisher: RecordingEventPublisher,
) -> RegisterMedia:
    return RegisterMedia(
        unit_of_work=unit_of_work,
        clock=clock,
        uuid_generator=uuid_generator,
        event_publisher=event_publisher,
    )


class TestRegisterMedia:
    async def test_persists_and_publishes(
        self,
        register: RegisterMedia,
        event_publisher: RecordingEventPublisher,
        clock: FrozenClock,
    ) -> None:
        summary = await register.execute(
            RegisterMediaCommand(
                source_url="HTTPS://Example.com/a.mp4#top",
                title="  A   talk  ",
                media_type=MediaType.VIDEO,
            )
        )

        assert summary.media_id == UUID(int=1)
        assert summary.source_url == "https://example.com/a.mp4"
        assert summary.title == "A talk"
        assert summary.status is MediaStatus.PENDING
        assert summary.created_at == clock.now()
        assert event_publisher.names() == ["MediaRegistered"]

    async def test_rejects_duplicate_source_url(self, register: RegisterMedia) -> None:
        command = RegisterMediaCommand(
            source_url="https://example.com/a.mp4",
            title="A",
            media_type=MediaType.VIDEO,
        )
        await register.execute(command)

        with pytest.raises(DuplicateMediaError):
            await register.execute(command)

    async def test_rejects_invalid_url_before_touching_storage(
        self, register: RegisterMedia, event_publisher: RecordingEventPublisher
    ) -> None:
        with pytest.raises(InvalidSourceUrlError):
            await register.execute(
                RegisterMediaCommand(
                    source_url="ftp://example.com/a.mp4",
                    title="A",
                    media_type=MediaType.VIDEO,
                )
            )

        assert event_publisher.published == []


class TestReadUseCases:
    async def test_get_returns_the_registered_item(
        self, register: RegisterMedia, unit_of_work: InMemoryUnitOfWorkFactory
    ) -> None:
        created = await register.execute(
            RegisterMediaCommand(
                source_url="https://example.com/a.mp4", title="A", media_type=MediaType.VIDEO
            )
        )

        summary = await GetMedia(unit_of_work=unit_of_work).execute(
            GetMediaQuery(media_id=created.media_id)
        )
        assert summary.media_id == created.media_id

    async def test_get_raises_for_unknown_id(self, unit_of_work: InMemoryUnitOfWorkFactory) -> None:
        with pytest.raises(MediaNotFoundError):
            await GetMedia(unit_of_work=unit_of_work).execute(GetMediaQuery(media_id=UUID(int=99)))

    async def test_list_paginates_and_filters(
        self,
        register: RegisterMedia,
        unit_of_work: InMemoryUnitOfWorkFactory,
        clock: FrozenClock,
    ) -> None:
        for index in range(5):
            clock.advance(60)
            await register.execute(
                RegisterMediaCommand(
                    source_url=f"https://example.com/{index}.mp4",
                    title=f"Item {index}",
                    media_type=MediaType.VIDEO if index % 2 == 0 else MediaType.AUDIO,
                )
            )

        list_media = ListMedia(unit_of_work=unit_of_work)

        first_page = await list_media.execute(ListMediaQuery(page=PageRequest(limit=2)))
        assert first_page.total == 5
        assert first_page.count == 2
        assert first_page.has_next
        # Newest first.
        assert first_page.items[0].title == "Item 4"

        audio_only = await list_media.execute(ListMediaQuery(media_type=MediaType.AUDIO))
        assert audio_only.total == 2

        searched = await list_media.execute(ListMediaQuery(search="item 3"))
        assert searched.total == 1


class TestArchiveMedia:
    async def test_archives_and_publishes(
        self,
        register: RegisterMedia,
        unit_of_work: InMemoryUnitOfWorkFactory,
        clock: FrozenClock,
        event_publisher: RecordingEventPublisher,
    ) -> None:
        created = await register.execute(
            RegisterMediaCommand(
                source_url="https://example.com/a.mp4", title="A", media_type=MediaType.VIDEO
            )
        )
        event_publisher.published.clear()

        archived = await ArchiveMedia(
            unit_of_work=unit_of_work, clock=clock, event_publisher=event_publisher
        ).execute(ArchiveMediaCommand(media_id=created.media_id))

        assert archived.status is MediaStatus.ARCHIVED
        assert event_publisher.names() == ["MediaArchived"]

    async def test_unknown_item_raises(
        self,
        unit_of_work: InMemoryUnitOfWorkFactory,
        clock: FrozenClock,
        event_publisher: RecordingEventPublisher,
    ) -> None:
        with pytest.raises(MediaNotFoundError):
            await ArchiveMedia(
                unit_of_work=unit_of_work, clock=clock, event_publisher=event_publisher
            ).execute(ArchiveMediaCommand(media_id=UUID(int=99)))
