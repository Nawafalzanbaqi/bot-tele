"""The slice of the Telegram Bot API that MediaHub uses.

Two protocols are declared here, one per consumer, and one concrete client that
satisfies both:

* :class:`TelegramUploader` - what the delivery provider needs (send a file).
* the messenger protocol declared separately in
  :mod:`mediahub.presentation.telegram.api` - what the gateway needs (poll,
  post, edit, answer).

They are deliberately *not* one interface. The gateway must not be able to
upload media and the provider must not be able to read updates, and neither
package imports the other - the composition root hands the same object to both.
That is interface segregation doing real architectural work: it keeps a
presentation adapter and an infrastructure adapter from acquiring a dependency
on each other.

The real implementation wraps ``python-telegram-bot`` and is imported lazily,
exactly as the download engine wraps yt-dlp: one file wide, so the library can
be replaced without touching anything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import io

# `Mapping` is imported at runtime because `_to_markup` annotates a parameter
# that is evaluated when the module is loaded by the type checker.
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Sequence

# The library is imported into deliberately untyped holders. It is an optional
# install, so the module must import without it; and everything it returns is
# re-typed at this boundary anyway, which is the point of the wrapper.
_Bot: Any = None
_HTTPXRequest: Any = None
_InlineKeyboardMarkup: Any = None
_InlineKeyboardButton: Any = None

try:  # pragma: no cover - exercised by the presence or absence of the package
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.request import HTTPXRequest

    _Bot = Bot
    _HTTPXRequest = HTTPXRequest
    _InlineKeyboardMarkup = InlineKeyboardMarkup
    _InlineKeyboardButton = InlineKeyboardButton
except ImportError:  # pragma: no cover - the library is an optional install
    pass


def _read_if_present(path: Path) -> bytes | None:
    """Return a file's bytes, or ``None`` if it is not there to read."""
    if not path.is_file():
        return None
    return path.read_bytes()


def _to_markup(raw: Mapping[str, Any] | None) -> Any:  # pragma: no cover - network
    """Convert MediaHub's plain keyboard dictionary into library objects.

    The gateway speaks dictionaries so that it never imports the client library;
    this is where that choice is paid for, in one small function.
    """
    if raw is None:
        return None
    rows = [
        [
            _InlineKeyboardButton(text=button["text"], callback_data=button["callback_data"])
            for button in row
        ]
        for row in raw.get("inline_keyboard", [])
    ]
    return _InlineKeyboardMarkup(rows)


@dataclass(frozen=True, slots=True)
class UploadedMedia:
    """What Telegram returned after accepting a file.

    Attributes:
        message_id: The message the file was posted as.
        chat_id: Where it was posted.
        file_id: Reference usable to re-send the file **by this bot only**.
        file_unique_id: Stable identifier that survives a token change but
            cannot be used to fetch.
        bytes_sent: How much was uploaded.
    """

    message_id: int
    chat_id: str
    file_id: str | None
    file_unique_id: str | None
    bytes_sent: int


class TelegramUploader(Protocol):
    """Sends media to a chat. Everything the delivery provider needs.

    ``content`` is a readable binary stream rather than a path so the provider
    can wrap it and measure what actually passes - progress and the receipt's
    checksum both come from that wrapper, in one pass over the bytes.
    """

    async def send_media(  # noqa: PLR0913 - mirrors Telegram's own parameter list
        self,
        *,
        chat_id: str,
        content: io.IOBase,
        kind: str,
        caption: str | None = None,
        filename: str | None = None,
        duration_seconds: int | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail: Path | None = None,
    ) -> UploadedMedia:
        """Upload media and return what Telegram said about it."""
        ...

    async def send_by_reference(
        self,
        *,
        chat_id: str,
        reference: str,
        kind: str,
        caption: str | None = None,
    ) -> UploadedMedia:
        """Send something Telegram already holds, by its own reference.

        One request, no upload: the destination already has the bytes.
        """
        ...


class PythonTelegramBotClient:
    """Concrete client over ``python-telegram-bot``.

    Satisfies both the uploader protocol here and the messenger protocol the
    gateway declares. It contains no policy: size limits, retries, throttling
    and error classification all live with the components that own them.
    """

    __slots__ = ("_bot",)

    def __init__(self, token: str, *, api_base_url: str | None = None) -> None:
        """Build a bot client.

        Args:
            token: The bot token. Held only in memory and never logged.
            api_base_url: Base URL of a local Bot API server, if one is run.
                Using one raises the upload ceiling from 50 MB to about 2 GB,
                which is why the capability is discovered from configuration
                rather than assumed.

        Raises:
            RuntimeError: If ``python-telegram-bot`` is not installed.
        """
        if _Bot is None or _HTTPXRequest is None:  # pragma: no cover - environment
            message = "python-telegram-bot is not installed; the gateway cannot run"
            raise RuntimeError(message)
        request = _HTTPXRequest(connection_pool_size=8, read_timeout=60, write_timeout=600)
        kwargs: dict[str, Any] = {"token": token, "request": request}
        if api_base_url:
            base = api_base_url.rstrip("/")
            kwargs["base_url"] = f"{base}/bot"
            kwargs["base_file_url"] = f"{base}/file/bot"
            # Without this the library treats a self-hosted server exactly like
            # the public one: it prepends the public file URL to the *absolute
            # local path* the server returns, producing
            # `api.telegram.org/file/bot<token>//var/lib/...` and a 404 on every
            # download. `local_mode` is the library's own switch for the case
            # and leaves the path alone, which is what makes it readable here.
            kwargs["local_mode"] = True
        self._bot: Any = _Bot(**kwargs)

    async def start(self) -> None:  # pragma: no cover - requires the network
        """Initialise the underlying client."""
        await self._bot.initialize()

    async def close(self) -> None:  # pragma: no cover - requires the network
        """Release the underlying client."""
        await self._bot.shutdown()

    # -- Messenger surface (used by the gateway) ----------------------------

    async def get_updates(
        self, *, offset: int | None = None, timeout: int = 30
    ) -> Sequence[Mapping[str, Any]]:  # pragma: no cover - requires the network
        """Long-poll for updates and return them as plain dictionaries.

        Returning dictionaries rather than library objects is what keeps
        ``python-telegram-bot`` out of the presentation layer entirely.
        """
        updates = await self._bot.get_updates(
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )
        return [update.to_dict() for update in updates]

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:  # pragma: no cover - requires the network
        """Post a message and return its identifier."""
        message = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=_to_markup(reply_markup),
            reply_to_message_id=reply_to_message_id,
            disable_web_page_preview=True,
        )
        return int(message.message_id)

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> None:  # pragma: no cover - requires the network
        """Replace the text of a message already posted."""
        await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=_to_markup(reply_markup),
            disable_web_page_preview=True,
        )

    async def answer_callback(
        self, *, callback_id: str, text: str | None = None
    ) -> None:  # pragma: no cover - requires the network
        """Acknowledge a button press so the client stops spinning."""
        await self._bot.answer_callback_query(callback_query_id=callback_id, text=text)

    async def download_file(
        self, *, file_id: str, max_bytes: int
    ) -> bytes:  # pragma: no cover - requires the network
        """Fetch a small uploaded file into memory.

        The size is checked against what Telegram reports **before** any bytes
        are transferred, so an oversized upload costs one metadata call rather
        than a download. The check is repeated afterwards because the declared
        size comes from the other side and a declared size can lie.
        """
        handle = await self._bot.get_file(file_id)
        declared = handle.file_size or 0
        if declared > max_bytes:
            message = f"that file is {declared} bytes; the limit is {max_bytes}"
            raise ValueError(message)

        payload = await self._read(handle)
        if len(payload) > max_bytes:
            message = f"that file is {len(payload)} bytes; the limit is {max_bytes}"
            raise ValueError(message)
        return payload

    @staticmethod
    async def _read(handle: Any) -> bytes:  # pragma: no cover - requires the network
        """Return an uploaded file's bytes, from wherever this server keeps them.

        The two Bot API servers answer ``getFile`` differently, and the
        difference is easy to miss because only one of them is exercised in
        development:

        * The public server returns a *relative* path, and the client fetches
          it over HTTPS.
        * A **self-hosted** server in local mode has already written the file
          to its own disk and returns an *absolute* path to it. Handing that to
          the downloader produces a request for
          ``api.telegram.org/file/bot<token>//var/lib/...``, which is a 404 -
          and the upload fails on exactly the deployments that went to the
          trouble of running their own server.

        The absolute path is readable here because the server's data volume is
        mounted into this container.
        """
        path = getattr(handle, "file_path", None)
        if isinstance(path, str) and path.startswith("/"):
            # Off the event loop: this runs on the gateway's poll loop, and a
            # blocking read there stalls every other conversation. The file is
            # small, but "small" is not a property this code can rely on.
            payload = await asyncio.to_thread(_read_if_present, Path(path))
            if payload is not None:
                return payload
            logger.bind(path=path).warning(
                "The Bot API server reported a local file this process cannot see; "
                "its data volume is probably not mounted here"
            )
        return bytes(await handle.download_as_bytearray())

    async def delete_message(
        self, *, chat_id: str, message_id: int
    ) -> bool:  # pragma: no cover - requires the network
        """Remove a message, reporting whether Telegram allowed it."""
        try:
            return bool(await self._bot.delete_message(chat_id=chat_id, message_id=message_id))
        except Exception:
            # Telegram limits how long a bot may delete a message, and refuses
            # outright in some chats. The caller has a fallback: telling the
            # user to delete it themselves.
            logger.opt(exception=True).debug("Could not delete a message")
            return False

    # -- Uploader surface (used by the delivery provider) -------------------

    async def send_media(  # noqa: PLR0913 - Telegram's own parameter list
        self,
        *,
        chat_id: str,
        content: io.IOBase,
        kind: str,
        caption: str | None = None,
        filename: str | None = None,
        duration_seconds: int | None = None,
        width: int | None = None,
        height: int | None = None,
        thumbnail: Path | None = None,
    ) -> UploadedMedia:  # pragma: no cover - requires the network
        """Upload a stream, presenting it according to ``kind``."""
        common: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption,
            "filename": filename,
        }
        # The poster image is opened inside a context manager rather than inline
        # in the call. An inline ``open`` leaks one descriptor per delivery, and
        # a process that delivers for months hits the descriptor ceiling and
        # then fails *everything*, including the writes that would explain why.
        with _opened(thumbnail) as poster:
            if kind == "video":
                message = await self._bot.send_video(
                    video=content,
                    duration=duration_seconds,
                    width=width,
                    height=height,
                    thumbnail=poster,
                    supports_streaming=True,
                    **common,
                )
                media = message.video
            elif kind == "audio":
                message = await self._bot.send_audio(
                    audio=content, duration=duration_seconds, **common
                )
                media = message.audio
            elif kind == "photo":
                message = await self._bot.send_photo(photo=content, **common)
                media = message.photo[-1] if message.photo else None
            else:
                message = await self._bot.send_document(document=content, **common)
                media = message.document

        return _uploaded(message, media, bytes_sent=_stream_size(content))

    async def send_by_reference(
        self,
        *,
        chat_id: str,
        reference: str,
        kind: str,
        caption: str | None = None,
    ) -> UploadedMedia:  # pragma: no cover - requires the network
        """Send something Telegram already holds, by its own reference.

        Passing the reference where a file would go is Telegram's own idiom for
        this, and it is what turns a re-send into a single request with no
        upload at all.
        """
        common: dict[str, Any] = {"chat_id": chat_id, "caption": caption}
        if kind == "video":
            message = await self._bot.send_video(video=reference, **common)
            media = message.video
        elif kind == "audio":
            message = await self._bot.send_audio(audio=reference, **common)
            media = message.audio
        elif kind == "photo":
            message = await self._bot.send_photo(photo=reference, **common)
            media = message.photo[-1] if message.photo else None
        else:
            message = await self._bot.send_document(document=reference, **common)
            media = message.document

        return _uploaded(message, media, bytes_sent=0)


@contextlib.contextmanager
def _opened(path: Path | None) -> Iterator[io.BufferedReader | None]:
    """Yield an open handle on ``path``, closing it however the block ends.

    A thumbnail is optional, so ``None`` yields ``None`` rather than forcing
    every call site to branch. The point of the wrapper is the ``finally``: the
    client library keeps no reference we could close later, so the only place
    the descriptor can be released is here.
    """
    if path is None:
        yield None
        return
    handle = path.open("rb")
    try:
        yield handle
    finally:
        handle.close()


def _uploaded(
    message: Any, media: Any, *, bytes_sent: int
) -> UploadedMedia:  # pragma: no cover - requires the network
    """Build the adapter's own result from the library's message object."""
    return UploadedMedia(
        message_id=int(message.message_id),
        chat_id=str(message.chat_id),
        file_id=getattr(media, "file_id", None),
        file_unique_id=getattr(media, "file_unique_id", None),
        bytes_sent=bytes_sent,
    )


def _stream_size(content: io.IOBase) -> int:  # pragma: no cover - requires the network
    """Return how much a measured stream reported, when it can say.

    The provider hands in a wrapper that counts; anything else reports zero and
    the provider falls back to the artifact's known size.
    """
    counted = getattr(content, "bytes_read", 0)
    return int(counted) if isinstance(counted, int) else 0
