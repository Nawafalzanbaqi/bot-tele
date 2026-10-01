"""The Telegram provider posts an album as media groups of up to ten, by path."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.application.delivery.ports import (
    DeliveryKind,
    DeliveryRequest,
    DeliveryTarget,
    TargetAddress,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.infrastructure.delivery.telegram.provider import TelegramDeliveryProvider
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from tests.support.telegram_fakes import FakeUploader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="album") as leased:
        yield leased


def a_target() -> DeliveryTarget:
    return DeliveryTarget(
        provider="telegram", address=TargetAddress(provider="telegram", opaque={"chat": "4242"})
    )


def item(
    scope: WorkspaceScope, name: str, kind: DeliveryKind, caption: str | None = None
) -> DeliveryRequest:
    (scope.directory() / name).write_bytes(b"x" * 10)
    role = ArtifactRole.PRIMARY if caption else ArtifactRole.COMPANION
    return DeliveryRequest(
        target=a_target(), artifact=scope.artifact(name, role=role), kind=kind, caption=caption
    )


class TestAlbums:
    def test_the_provider_declares_albums(self) -> None:
        provider = TelegramDeliveryProvider(FakeUploader(), bot_principal="1")

        assert provider.capabilities().supports_albums

    async def test_items_go_as_one_group_and_the_receipt_is_the_first(
        self, scope: WorkspaceScope
    ) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="1", local_api_server=True)
        requests = [
            item(scope, "a.jpg", DeliveryKind.PHOTO, caption="first"),
            item(scope, "b.mp4", DeliveryKind.VIDEO),
            item(scope, "c.jpg", DeliveryKind.PHOTO),
        ]

        receipt = await provider.deliver_album(requests, scope)

        assert uploader.groups == [
            [("photo", "a.jpg", "first"), ("video", "b.mp4", None), ("photo", "c.jpg", None)]
        ]
        assert receipt.message is not None
        assert receipt.message.message_id == "500"
        assert receipt.reference.remote_id == "FILE-a.jpg"
        assert receipt.size_bytes == 30
        assert receipt.can_serve_back

    async def test_more_than_ten_go_as_consecutive_groups(self, scope: WorkspaceScope) -> None:
        uploader = FakeUploader()
        provider = TelegramDeliveryProvider(uploader, bot_principal="1", local_api_server=True)
        requests = [
            item(scope, f"{index:02d}.jpg", DeliveryKind.PHOTO, caption="x" if index == 0 else None)
            for index in range(12)
        ]

        receipt = await provider.deliver_album(requests, scope)

        assert [len(group) for group in uploader.groups] == [10, 2]
        assert receipt.reference.remote_id == "FILE-00.jpg"
        assert receipt.size_bytes == 120

    async def test_progress_runs_from_nothing_to_everything(self, scope: WorkspaceScope) -> None:
        provider = TelegramDeliveryProvider(
            FakeUploader(), bot_principal="1", local_api_server=True
        )
        seen: list[tuple[int, int | None]] = []
        requests = [
            item(scope, "a.jpg", DeliveryKind.PHOTO, caption="c"),
            item(scope, "b.jpg", DeliveryKind.PHOTO),
        ]

        await provider.deliver_album(
            requests, scope, on_progress=lambda p: seen.append((p.sent_bytes, p.total_bytes))
        )

        assert seen[0][0] == 0
        assert seen[-1] == (20, 20)
