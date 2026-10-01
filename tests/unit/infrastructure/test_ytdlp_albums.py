"""A collection whose entries are one post's items is an album: fetched whole, in order."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.download.ports import DownloadRequest
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.media.enums import MediaType
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.download.ytdlp.mapping import (
    album_entries,
    to_metadata,
    to_selected_format,
)
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.ytdlp_fakes import FakeYoutubeDL, factory_for, playlist_info, writes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

URL = "https://example.com/post/abc"
NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_engine_registry() -> Iterator[None]:
    FakeYoutubeDL.instances.clear()
    yield
    FakeYoutubeDL.instances.clear()


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    with FilesystemWorkspace(tmp_path / "ws").lease(label="album") as leased:
        yield leased


def image(index: int, *, taken: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": f"abc_{index}",
        "title": f"item {index}",
        "url": f"https://cdn/{index}.jpg",
        "ext": "jpg",
        "vcodec": "none",
        "acodec": "none",
        "width": 720,
        "height": 900,
        "webpage_url": URL,
    }
    if taken:
        entry["requested_downloads"] = [{"filepath": f"/lease/{taken}", "ext": "jpg"}]
    return entry


def video(index: int, *, taken: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": f"abc_{index}",
        "title": f"item {index}",
        "formats": [
            {
                "format_id": "v101",
                "url": f"https://cdn/{index}.mp4",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 1280,
                "width": 720,
            }
        ],
        "webpage_url": URL,
    }
    if taken:
        entry["requested_downloads"] = [
            {
                "filepath": f"/lease/{taken}",
                "format_id": "v101",
                "ext": "mp4",
                "vcodec": "avc1",
                "width": 720,
                "height": 1280,
            }
        ]
    return entry


def album(*entries: dict[str, Any]) -> dict[str, Any]:
    return playlist_info(len(entries), webpage_url=URL, entries=list(entries))


def engine() -> YtDlpDownloader:
    return YtDlpDownloader(
        DownloadSettings(enabled=True, probe_attempts=1, progress_interval_seconds=0.0),
        url_policy=UrlPolicy(),
        address_guard=None,
    )


class TestRecognition:
    def test_inline_entries_on_the_same_page_are_an_album(self) -> None:
        assert len(album_entries(album(video(1), image(2)))) == 2

    def test_a_list_of_links_is_a_playlist_not_an_album(self) -> None:
        linked = playlist_info(
            2,
            webpage_url=URL,
            entries=[
                {"_type": "url", "url": "https://x/1"},
                {"_type": "url", "url": "https://x/2"},
            ],
        )

        assert album_entries(linked) == ()

    def test_entries_from_other_pages_are_not_an_album(self) -> None:
        elsewhere = {**video(1), "webpage_url": "https://example.com/other"}

        assert album_entries(album(video(1), elsewhere)) == ()

    def test_metadata_describes_the_album_by_its_first_item(self) -> None:
        metadata = to_metadata(album(image(1), video(2)), url=URL, probed_at=NOW)

        assert metadata.is_album
        assert metadata.is_playlist
        assert metadata.entry_count == 2
        assert metadata.kind is MediaType.IMAGE, "the first item is a picture"

    def test_a_video_first_album_offers_the_video_ladder(self) -> None:
        metadata = to_metadata(album(video(1), image(2)), url=URL, probed_at=NOW)

        assert metadata.kind is MediaType.VIDEO
        assert [f.format_id for f in metadata.video_formats] == ["v101"]

    def test_the_selected_format_of_an_album_is_its_first_entry(self) -> None:
        info = album(video(1, taken="1.mp4"), image(2, taken="2.jpg"))

        assert to_selected_format(info).format_id == "v101"


class TestFetching:
    async def test_an_album_probe_is_not_resolved_to_its_first_entry(self) -> None:
        downloader = YtDlpDownloader(
            DownloadSettings(enabled=True, probe_attempts=1, progress_interval_seconds=0.0),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory_for(info=album(video(1), image(2))),
        )

        metadata = await downloader.probe(URL)

        assert metadata.is_album
        assert metadata.from_playlist is False
        assert metadata.entry_count == 2

    async def test_every_item_lands_as_primary_then_companions_in_order(
        self, scope: WorkspaceScope
    ) -> None:
        info = album(video(1, taken="1.mp4"), image(2, taken="2.jpg"), image(3, taken="3.jpg"))
        script = [*writes("3.jpg", 256), *writes("1.mp4", 2048), *writes("2.jpg", 512)]
        downloader = YtDlpDownloader(
            DownloadSettings(
                enabled=True, probe_attempts=1, progress_interval_seconds=0.0, verify_streams=False
            ),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory_for(info=info, script=script),
        )

        result = await downloader.fetch(DownloadRequest(url=URL, allow_playlist=True), scope)

        assert [(a.name, a.role) for a in result.artifacts] == [
            ("1.mp4", ArtifactRole.PRIMARY),
            ("2.jpg", ArtifactRole.COMPANION),
            ("3.jpg", ArtifactRole.COMPANION),
        ]
        assert result.metadata.is_album
        assert result.selected_format.format_id == "v101"

    async def test_an_album_needs_no_explicit_playlist_permission(
        self, scope: WorkspaceScope
    ) -> None:
        """The use case asks for the whole album; the policy must not call it a stray playlist."""
        info = album(image(1, taken="1.jpg"), image(2, taken="2.jpg"))
        downloader = YtDlpDownloader(
            DownloadSettings(
                enabled=True, probe_attempts=1, progress_interval_seconds=0.0, verify_streams=False
            ),
            url_policy=UrlPolicy(),
            address_guard=None,
            youtube_dl_factory=factory_for(
                info=info, script=[*writes("1.jpg", 10), *writes("2.jpg", 10)]
            ),
        )

        result = await downloader.fetch(DownloadRequest(url=URL), scope)

        assert len(result.artifacts) == 2
