"""The upload path of the python-telegram-bot client, against a fake library.

The one decision this adapter makes on its own is how a file reaches the Bot
API server: as a stream through this process, or as a *path* the self-hosted
server reads itself. Getting that wrong is invisible until a large file is
an out-of-memory kill, which is why the path is pinned here rather than left
to the network.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mediahub.infrastructure.delivery.telegram import client as client_module
from mediahub.infrastructure.delivery.telegram.client import PythonTelegramBotClient

pytestmark = pytest.mark.unit


class FakeBot:
    """Records how it was built and every send it was asked for."""

    instances: list[FakeBot] = []  # noqa: RUF012 - test-only registry

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.thumbnail_was_open: bool | None = None
        FakeBot.instances.append(self)

    def _message(self, kind: str) -> Any:
        media = SimpleNamespace(file_id=f"{kind}-id", file_unique_id=f"{kind}-uniq")
        photo = [SimpleNamespace(file_id="small"), media] if kind == "photo" else None
        return SimpleNamespace(
            message_id=500,
            chat_id=4242,
            video=media,
            audio=media,
            document=media,
            photo=photo,
        )

    async def send_video(self, **kwargs: Any) -> Any:
        poster = kwargs.get("thumbnail")
        self.thumbnail_was_open = None if poster is None else not poster.closed
        self.sent.append(("video", kwargs))
        return self._message("video")

    async def send_audio(self, **kwargs: Any) -> Any:
        self.sent.append(("audio", kwargs))
        return self._message("audio")

    async def send_photo(self, **kwargs: Any) -> Any:
        self.sent.append(("photo", kwargs))
        return self._message("photo")

    async def send_document(self, **kwargs: Any) -> Any:
        self.sent.append(("document", kwargs))
        return self._message("document")


class FakeRequest:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def fake_library(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeBot.instances.clear()
    monkeypatch.setattr(client_module, "_Bot", FakeBot)
    monkeypatch.setattr(client_module, "_HTTPXRequest", FakeRequest)


class Counted(io.BytesIO):
    """A stream that reports what passed through it, like the provider's wrapper."""

    bytes_read = 4096


class TestConstruction:
    def test_a_self_hosted_server_switches_the_library_to_local_mode(self) -> None:
        PythonTelegramBotClient("123:abc", api_base_url="http://botapi:8081/")

        kwargs = FakeBot.instances[0].kwargs
        assert kwargs["base_url"] == "http://botapi:8081/bot"
        assert kwargs["base_file_url"] == "http://botapi:8081/file/bot"
        assert kwargs["local_mode"] is True
        assert kwargs["token"] == "123:abc"

    def test_the_public_server_is_the_default(self) -> None:
        PythonTelegramBotClient("123:abc")

        kwargs = FakeBot.instances[0].kwargs
        assert "base_url" not in kwargs
        assert "local_mode" not in kwargs


class TestSendMedia:
    async def test_a_self_hosted_server_is_handed_the_path_not_the_bytes(
        self, tmp_path: Path
    ) -> None:
        """This is the 2 GB ceiling: the server reads the file, this process never buffers it."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 64)
        client = PythonTelegramBotClient("123:abc", api_base_url="http://botapi:8081")

        uploaded = await client.send_media(
            chat_id="4242",
            content=Counted(b"x" * 64),
            kind="video",
            caption="A clip",
            filename="clip.mp4",
            duration_seconds=125,
            width=1920,
            height=1080,
            local_path=clip,
        )

        kind, kwargs = FakeBot.instances[0].sent[0]
        assert kind == "video"
        assert kwargs["video"] == clip
        assert kwargs["supports_streaming"] is True
        assert (kwargs["duration"], kwargs["width"], kwargs["height"]) == (125, 1920, 1080)
        assert uploaded.message_id == 500
        assert uploaded.chat_id == "4242"
        assert uploaded.file_id == "video-id"
        assert uploaded.file_unique_id == "video-uniq"

    async def test_the_public_server_gets_the_stream_even_when_a_path_is_offered(
        self, tmp_path: Path
    ) -> None:
        """Without local mode the path would leave this machine as a string and 400."""
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"x" * 64)
        client = PythonTelegramBotClient("123:abc")
        stream = Counted(b"x" * 64)

        uploaded = await client.send_media(
            chat_id="4242", content=stream, kind="video", local_path=clip
        )

        _, kwargs = FakeBot.instances[0].sent[0]
        assert kwargs["video"] is stream
        assert uploaded.bytes_sent == 4096, "what the measuring wrapper counted"

    async def test_the_thumbnail_is_open_during_the_send_and_closed_after(
        self, tmp_path: Path
    ) -> None:
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"\xff\xd8")
        client = PythonTelegramBotClient("123:abc")

        await client.send_media(
            chat_id="4242", content=Counted(b"x"), kind="video", thumbnail=poster
        )

        bot = FakeBot.instances[0]
        assert bot.thumbnail_was_open is True
        handle = bot.sent[0][1]["thumbnail"]
        assert handle.closed, "a leaked descriptor per delivery ends in EMFILE"

    @pytest.mark.parametrize(
        ("kind", "method", "field"),
        [
            ("audio", "audio", "audio"),
            ("photo", "photo", "photo"),
            ("document", "document", "document"),
            ("anything-else", "document", "document"),
        ],
    )
    async def test_each_kind_uses_the_matching_call(
        self, kind: str, method: str, field: str
    ) -> None:
        client = PythonTelegramBotClient("123:abc")

        uploaded = await client.send_media(chat_id="4242", content=Counted(b"x"), kind=kind)

        sent_kind, kwargs = FakeBot.instances[0].sent[0]
        assert sent_kind == method
        assert field in kwargs
        assert uploaded.file_id == f"{method}-id"

    async def test_a_photo_reports_the_largest_size_telegram_made(self) -> None:
        client = PythonTelegramBotClient("123:abc")

        uploaded = await client.send_media(chat_id="4242", content=Counted(b"x"), kind="photo")

        assert uploaded.file_id == "photo-id", "the last entry of message.photo is the largest"


class TestSendByReference:
    async def test_a_reference_is_passed_where_a_file_would_go(self) -> None:
        client = PythonTelegramBotClient("123:abc")

        uploaded = await client.send_by_reference(
            chat_id="4242", reference="FILE-ABC", kind="video", caption="again"
        )

        kind, kwargs = FakeBot.instances[0].sent[0]
        assert kind == "video"
        assert kwargs["video"] == "FILE-ABC"
        assert uploaded.bytes_sent == 0, "nothing was uploaded"
