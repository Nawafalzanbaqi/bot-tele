"""The engine fallback: video first, pictures only when the video engine found none.

What matters here is the *narrowness* of the handover. Two refusals mean "ask
the other engine"; every other failure is about the source or the network and
would fail identically the second time, slower and with a vaguer message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    ConnectionBlockedError,
    ContentRemovedError,
    NoPlayableMediaError,
    UnsupportedProviderError,
)
from mediahub.application.download.ports import DownloadRequest
from mediahub.domain.media.enums import MediaType
from mediahub.infrastructure.download.composite import CompositeDownloader
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from tests.support.download_fakes import FakeDownloader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit

URL = "https://example.com/p/abc123"


@dataclass
class Declining(FakeDownloader):
    """An image engine that claims no URL at all."""

    def supports(self, url: str) -> bool:
        del url
        return False


def video_engine(**overrides: object) -> FakeDownloader:
    return FakeDownloader(engine_name="video", provider="videosite", **overrides)  # type: ignore[arg-type]


def image_engine(**overrides: object) -> FakeDownloader:
    return FakeDownloader(
        engine_name="images",
        provider="imagesite",
        kind=MediaType.IMAGE,
        offers_video=False,
        offers_audio=False,
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="composite") as leased:
        yield leased


class TestSurface:
    def test_names_both_engines_because_either_may_do_the_work(self) -> None:
        assert CompositeDownloader(video_engine(), image_engine()).name == "video+images"

    def test_capabilities_are_the_video_engines_not_a_union(self) -> None:
        """A union would promise a rendition ladder for a photograph."""
        composite = CompositeDownloader(video_engine(), image_engine())

        assert composite.capabilities().engine == "video"

    def test_supports_whatever_either_engine_claims(self) -> None:
        assert CompositeDownloader(Declining(), image_engine()).supports(URL) is True
        assert CompositeDownloader(Declining(), Declining()).supports(URL) is False


class TestProbeHandsOverNarrowly:
    @pytest.mark.parametrize(
        "refusal",
        [NoPlayableMediaError("no stream here"), UnsupportedProviderError("no extractor")],
    )
    async def test_no_stream_or_no_extractor_asks_the_image_engine(
        self, refusal: Exception
    ) -> None:
        video = video_engine(probe_error=refusal)
        images = image_engine()

        metadata = await CompositeDownloader(video, images).probe(URL)

        assert metadata.kind is MediaType.IMAGE
        assert metadata.provider == "imagesite"
        assert (video.probe_calls, images.probe_calls) == (1, 1)

    @pytest.mark.parametrize(
        "failure",
        [
            AuthenticationRequiredError("log in first"),
            ContentRemovedError("gone"),
            ConnectionBlockedError("reset"),
        ],
    )
    async def test_every_other_failure_is_final_and_reported_as_is(
        self, failure: Exception
    ) -> None:
        """A private post fails identically in the image engine, only slower and vaguer."""
        video = video_engine(probe_error=failure)
        images = image_engine()

        with pytest.raises(type(failure)):
            await CompositeDownloader(video, images).probe(URL)

        assert images.probe_calls == 0

    async def test_a_url_the_image_engine_does_not_claim_keeps_the_original_refusal(
        self,
    ) -> None:
        video = video_engine(probe_error=NoPlayableMediaError("no stream here"))
        images = Declining(engine_name="images")

        with pytest.raises(NoPlayableMediaError):
            await CompositeDownloader(video, images).probe(URL)

        assert images.probe_calls == 0

    async def test_a_video_is_never_sent_to_the_image_engine(self) -> None:
        """Asking the image engine first would strip the audio from every video."""
        video = video_engine()
        images = image_engine()

        metadata = await CompositeDownloader(video, images).probe(URL)

        assert metadata.kind is MediaType.VIDEO
        assert images.probe_calls == 0


class TestFetchHandsOverTheSameWay:
    async def test_the_image_engine_fetches_what_the_video_engine_declined(
        self, scope: WorkspaceScope
    ) -> None:
        video = video_engine(fetch_error=NoPlayableMediaError("no stream here"))
        images = image_engine(size_bytes=2048)

        result = await CompositeDownloader(video, images).fetch(DownloadRequest(url=URL), scope)

        assert result.provider == "imagesite"
        assert result.primary.size_bytes == 2048
        assert (video.fetch_calls, images.fetch_calls) == (1, 1)

    async def test_a_final_failure_is_not_retried_in_the_image_engine(
        self, scope: WorkspaceScope
    ) -> None:
        video = video_engine(fetch_error=ContentRemovedError("gone"))
        images = image_engine()

        with pytest.raises(ContentRemovedError):
            await CompositeDownloader(video, images).fetch(DownloadRequest(url=URL), scope)

        assert images.fetch_calls == 0

    async def test_progress_and_cancellation_reach_the_engine_that_does_the_work(
        self, scope: WorkspaceScope
    ) -> None:
        seen: list[object] = []
        video = video_engine(fetch_error=NoPlayableMediaError("no stream here"))
        images = image_engine(chunks=(1024, 2048))

        await CompositeDownloader(video, images).fetch(
            DownloadRequest(url=URL), scope, on_progress=seen.append
        )

        assert seen, "the image engine's progress must not be swallowed by the wrapper"
