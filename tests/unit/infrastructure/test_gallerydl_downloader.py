"""The still-image engine adapter, against a scripted stand-in for gallery-dl.

Confinement, the bounded lock, honest metadata without a second enumeration,
and failures that carry names - each is a thing that went wrong in production
before it was pinned here.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    DownloadFailedError,
    DownloadTimeoutError,
    NoPlayableMediaError,
    SizeLimitExceededError,
    UnsupportedProviderError,
)
from mediahub.application.download.ports import DownloadRequest
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.media.enums import MediaType
from mediahub.domain.sources.errors import BlockedAddressError
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.download.gallerydl import downloader as module
from mediahub.infrastructure.download.gallerydl.downloader import (
    MAX_ITEMS,
    GalleryDlDownloader,
)
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.gallerydl_fakes import FakeGalleryDl, item

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.domain.sources.value_objects import ValidatedUrl

pytestmark = pytest.mark.unit

URL = "https://example.com/p/abc123"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGalleryDl:
    stand_in = FakeGalleryDl()
    monkeypatch.setattr(module, "gallery_dl", stand_in)
    monkeypatch.setattr(module, "_gallery_version", stand_in.version)
    return stand_in


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="images") as leased:
        yield leased


def adapter(**overrides: object) -> GalleryDlDownloader:
    settings = DownloadSettings(enabled=True, socket_timeout_seconds=17.0, **overrides)  # type: ignore[arg-type]
    return GalleryDlDownloader(settings, url_policy=UrlPolicy())


class TestSurface:
    def test_reports_the_library_version(self, fake: FakeGalleryDl) -> None:
        assert adapter().capabilities().version == "fake-1.0"
        assert adapter().is_available

    def test_supports_asks_the_extractor_registry(self, fake: FakeGalleryDl) -> None:
        fake.unsupported.add("https://nowhere.example/x")

        assert adapter().supports(URL) is True
        assert adapter().supports("https://nowhere.example/x") is False
        assert adapter().supports("not a url") is False


class TestProbe:
    async def test_enumerates_without_downloading(self, fake: FakeGalleryDl) -> None:
        fake.items[URL] = [item(1), item(2), item(3)]

        metadata = await adapter().probe(URL)

        assert metadata.kind is MediaType.IMAGE
        assert metadata.provider == "fakegram"
        assert metadata.title == "A carousel of three"
        assert metadata.uploader == "someone"
        assert metadata.entry_count == 3
        assert not any(hasattr(job, "written") for job in fake.jobs), "a probe must not download"

    async def test_an_empty_post_is_no_playable_media(self, fake: FakeGalleryDl) -> None:
        """Read fine, holds nothing: the one refusal that means 'ask nobody else'."""
        fake.items[URL] = []

        with pytest.raises(NoPlayableMediaError):
            await adapter().probe(URL)

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (16, AuthenticationRequiredError),
            (32, UnsupportedProviderError),
            (4, DownloadFailedError),
            (128, DownloadFailedError),
        ],
    )
    async def test_status_bits_name_the_failure(
        self, fake: FakeGalleryDl, status: int, expected: type[Exception]
    ) -> None:
        """The runner swallows its exceptions; the bits are the only record of why."""
        fake.status[URL] = status

        with pytest.raises(expected):
            await adapter().probe(URL)

    async def test_no_extractor_at_construction_is_unsupported(self, fake: FakeGalleryDl) -> None:
        """The constructor raises, not run(); it used to escape unclassified."""
        fake.unsupported.add(URL)

        with pytest.raises(UnsupportedProviderError):
            await adapter().probe(URL)

    async def test_an_escaping_exception_is_classified(self, fake: FakeGalleryDl) -> None:
        class AuthenticationError(Exception):
            pass

        fake.raises[URL] = AuthenticationError("login required")

        with pytest.raises(AuthenticationRequiredError):
            await adapter().probe(URL)

    async def test_the_item_ceiling_applies_to_what_is_reported(self, fake: FakeGalleryDl) -> None:
        fake.items[URL] = [item(n) for n in range(1, MAX_ITEMS + 15)]

        metadata = await adapter().probe(URL)

        assert metadata.entry_count == MAX_ITEMS


class TestFetch:
    async def test_writes_inside_the_lease_and_ranks_the_largest_first(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        fake.items[URL] = [item(1, size=500), item(2, size=9000), item(3, size=700)]

        result = await adapter().fetch(DownloadRequest(url=URL), scope)

        assert result.primary.size_bytes == 9000
        companions = [a for a in result.artifacts if a.role is ArtifactRole.COMPANION]
        assert sorted(a.size_bytes for a in companions) == [500, 700]
        assert set(scope.names()) == {"001.jpg", "002.jpg", "003.jpg"}
        assert result.total_bytes == 10200
        assert result.metadata.kind is MediaType.IMAGE

    async def test_the_result_is_described_without_a_second_enumeration(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        fake.items[URL] = [item(1), item(2)]

        result = await adapter().fetch(DownloadRequest(url=URL), scope)

        assert result.metadata.title == "A carousel of three"
        assert result.metadata.entry_count == 2
        assert len(fake.jobs) == 1, "one download job, no enumeration before or after"

    async def test_configuration_is_pinned_to_the_lease(
        self, fake: FakeGalleryDl, scope: WorkspaceScope, tmp_path: Path
    ) -> None:
        fake.items[URL] = [item(1)]
        engine = adapter(
            cookies_file=tmp_path / "cookies.txt", proxy="http://vpn:8888", user_agent="UA/1"
        )

        await engine.fetch(DownloadRequest(url=URL), scope)

        values = fake.config.values
        assert values["base-directory"] == str(scope.directory())
        assert values["directory"] == []
        assert values["filename"] == "{num:>03}.{extension}"
        assert values["range"] == f"1-{MAX_ITEMS}"
        assert values["postprocessors"] == []
        assert values["timeout"] == 17.0
        assert values["cookies"] == str(tmp_path / "cookies.txt")
        assert values["proxy"] == "http://vpn:8888"
        assert values["user-agent"] == "UA/1"
        assert fake.config.clears >= 1, "settings from a previous run must not leak"

    async def test_a_post_over_the_ceiling_is_refused(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        fake.items[URL] = [item(1, size=6000), item(2, size=6000)]

        with pytest.raises(SizeLimitExceededError):
            await adapter().fetch(DownloadRequest(url=URL, max_bytes=10_000), scope)

    async def test_nothing_produced_is_named_from_the_status(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        fake.status[URL] = 16

        with pytest.raises(AuthenticationRequiredError):
            await adapter().fetch(DownloadRequest(url=URL), scope)


class TestTheLockIsBounded:
    async def test_a_busy_engine_is_a_timeout_not_a_hang(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        """A caller waits for the lock only within its own budget, then says so."""
        fake.items[URL] = [item(1)]
        assert module._CONFIG_LOCK.acquire(timeout=1)
        try:
            with pytest.raises(DownloadTimeoutError):
                await asyncio.wait_for(
                    adapter().fetch(DownloadRequest(url=URL, timeout_seconds=0.5), scope),
                    timeout=3,
                )
        finally:
            module._CONFIG_LOCK.release()

        assert scope.names() == ()

    async def test_the_lock_is_released_after_a_failure(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        fake.status[URL] = 16
        with pytest.raises(AuthenticationRequiredError):
            await adapter().fetch(DownloadRequest(url=URL), scope)

        assert module._CONFIG_LOCK.acquire(blocking=False)
        module._CONFIG_LOCK.release()

    async def test_a_download_that_outlives_its_budget_stops_at_the_next_item(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        """The item in flight finishes; the rest are skipped, not fetched into a dead lease."""
        fake.items[URL] = [item(n) for n in range(1, 7)]
        fake.download_delay = 0.3

        with pytest.raises(DownloadTimeoutError):
            await adapter().fetch(DownloadRequest(url=URL, timeout_seconds=0.5), scope)

        # Let the abandoned thread run to its end, then look at what it did.
        for _ in range(40):
            if not module._CONFIG_LOCK.locked():
                break
            await asyncio.sleep(0.1)
        written = next(job for job in fake.jobs if hasattr(job, "written")).written
        assert 1 <= len(written) <= 3, f"the job should have stopped early, wrote {len(written)}"


class FakeGuard:
    def __init__(self) -> None:
        self.checked: list[str] = []

    def check(self, validated: ValidatedUrl) -> None:
        self.checked.append(validated.value)
        raise BlockedAddressError(validated.value, "127.0.0.1", "resolves to a private address")


class TestTheAddressGuardIsConsulted:
    async def test_a_blocked_address_is_refused_before_any_engine_work(
        self, fake: FakeGalleryDl, scope: WorkspaceScope
    ) -> None:
        fake.items[URL] = [item(1)]
        guard = FakeGuard()
        engine = GalleryDlDownloader(
            DownloadSettings(enabled=True),
            url_policy=UrlPolicy(),
            address_guard=guard,  # type: ignore[arg-type]
        )

        with pytest.raises(BlockedAddressError):
            await engine.probe(URL)
        with pytest.raises(BlockedAddressError):
            await engine.fetch(DownloadRequest(url=URL), scope)

        assert guard.checked == [URL, URL]
        assert fake.jobs == []


def test_the_lock_is_a_plain_lock_shared_by_every_instance() -> None:
    """Two adapters over one process-global configuration must share one lock."""
    assert isinstance(module._CONFIG_LOCK, type(threading.Lock()))
