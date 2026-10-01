"""A post with several items is delivered whole: as one album where the destination groups."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mediahub.application.delivery.ports import DeliveryKind, DeliveryTarget, TargetAddress
from mediahub.application.download.dto import AcquireMediaCommand
from mediahub.application.download.use_cases.acquire_media import AcquireMedia, _kind_of_file
from mediahub.infrastructure.delivery.registry import (
    DeliveryProviderRegistry,
    ProviderRegistration,
)
from mediahub.infrastructure.persistence.memory.journal import InMemoryAcquisitionJournal
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from tests.support.delivery_fakes import FAKE_PROVIDER, FakeDeliveryProvider
from tests.support.download_fakes import FakeDownloader

if TYPE_CHECKING:
    from pathlib import Path

URL = "https://example.com/post/1"
NOW = datetime(2026, 10, 1, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


def target() -> DeliveryTarget:
    return DeliveryTarget(
        provider=FAKE_PROVIDER, address=TargetAddress(provider=FAKE_PROVIDER, opaque={"chat": "1"})
    )


def use_case(
    tmp_path: Path, delivery: FakeDeliveryProvider, companions: tuple[str, ...]
) -> AcquireMedia:
    registry = DeliveryProviderRegistry(
        registrations=[ProviderRegistration(delivery)], default_provider=delivery.name
    )
    return AcquireMedia(
        downloader=FakeDownloader(companions=companions),
        delivery=registry,
        workspace=FilesystemWorkspace(tmp_path / "ws"),
        journal=InMemoryAcquisitionJournal(),
        clock=FrozenClock(),
        max_item_bytes=100 * 1024 * 1024,
    )


async def run(case: AcquireMedia) -> object:
    return await case.execute(
        AcquireMediaCommand(url=URL, quality_key="auto", target=target(), requested_by="telegram:1")
    )


class TestAlbums:
    async def test_photos_and_videos_of_one_post_go_as_one_album(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        case = use_case(tmp_path, delivery, ("002.jpg", "003.mp4", "track.m4a"))

        summary = await run(case)

        assert len(delivery.albums) == 1
        assert [item.kind for item in delivery.albums[0]] == [
            DeliveryKind.VIDEO,
            DeliveryKind.PHOTO,
            DeliveryKind.VIDEO,
        ]
        assert delivery.albums[0][0].caption is not None, "the caption rides on the first item"
        assert [item.kind for item in delivery.delivered] == [DeliveryKind.AUDIO], (
            "the track follows on its own"
        )
        assert summary.items_delivered == 4  # type: ignore[attr-defined]
        assert summary.local_copy_released  # type: ignore[attr-defined]

    async def test_without_album_support_items_go_one_by_one_by_kind(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider(supports_albums=False)
        case = use_case(tmp_path, delivery, ("002.jpg", "003.mp4", "track.m4a"))

        summary = await run(case)

        assert delivery.albums == []
        assert [item.kind for item in delivery.delivered] == [
            DeliveryKind.VIDEO,
            DeliveryKind.PHOTO,
            DeliveryKind.VIDEO,
            DeliveryKind.AUDIO,
        ]
        assert summary.items_delivered == 4  # type: ignore[attr-defined]

    async def test_a_single_item_is_not_an_album(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()

        await run(use_case(tmp_path, delivery, ()))

        assert delivery.albums == []
        assert len(delivery.delivered) == 1


class TestCompanionKinds:
    def test_kinds_come_from_the_suffix(self) -> None:
        assert _kind_of_file("002.JPG") is DeliveryKind.PHOTO
        assert _kind_of_file("clip.mp4") is DeliveryKind.VIDEO
        assert _kind_of_file("sound.m4a") is DeliveryKind.AUDIO
        assert _kind_of_file("notes.pdf") is DeliveryKind.DOCUMENT
