"""Test doubles for the Telegram surface.

``FakeMessenger`` satisfies the gateway's messenger protocol and
``FakeUploader`` the delivery provider's uploader protocol - the same split the
real client bridges. Neither touches a network, so every gateway test is a unit
test.

The builders produce update dictionaries in Telegram's own shape, because that
is exactly what the parser has to survive.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mediahub.application.credentials.errors import InvalidCookieJarError
from mediahub.application.credentials.ports import CookieSummary
from mediahub.infrastructure.credentials.cookie_jar import decode, parse
from mediahub.infrastructure.delivery.telegram.client import UploadedMedia

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


@dataclass
class SentText:
    """One message the gateway posted."""

    chat_id: str
    text: str
    reply_markup: Mapping[str, Any] | None = None
    reply_to_message_id: int | None = None


@dataclass
class EditedText:
    """One message the gateway edited."""

    chat_id: str
    message_id: int
    text: str
    reply_markup: Mapping[str, Any] | None = None


class FakeMessenger:
    """Records everything the gateway says, and replays queued updates."""

    def __init__(self, *, batches: Sequence[Sequence[Mapping[str, Any]]] = ()) -> None:
        """Create a messenger that will serve ``batches`` in order."""
        self.batches = [list(batch) for batch in batches]
        self.sent: list[SentText] = []
        self.edits: list[EditedText] = []
        self.answers: list[tuple[str, str | None]] = []
        self.poll_calls: list[int | None] = []
        self.next_message_id = 100
        self.edit_error: Exception | None = None
        self.files: dict[str, bytes] = {}
        self.download_error: Exception | None = None
        self.deleted: list[tuple[str, int]] = []
        self.delete_allowed = True

    async def get_updates(
        self, *, offset: int | None = None, timeout: int = 30
    ) -> Sequence[Mapping[str, Any]]:
        """Return the next queued batch, or nothing."""
        del timeout
        self.poll_calls.append(offset)
        return self.batches.pop(0) if self.batches else []

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Record a posted message and return a fresh identifier."""
        self.sent.append(SentText(chat_id, text, reply_markup, reply_to_message_id))
        self.next_message_id += 1
        return self.next_message_id

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> None:
        """Record an edit, or raise if the fake was told to fail."""
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(EditedText(chat_id, message_id, text, reply_markup))

    async def answer_callback(self, *, callback_id: str, text: str | None = None) -> None:
        """Record a callback acknowledgement."""
        self.answers.append((callback_id, text))

    async def download_file(self, *, file_id: str, max_bytes: int) -> bytes:
        """Return a queued file, enforcing the ceiling the real client does."""
        if self.download_error is not None:
            raise self.download_error
        payload = self.files.get(file_id, b"")
        if len(payload) > max_bytes:
            message = f"that file is {len(payload)} bytes; the limit is {max_bytes}"
            raise ValueError(message)
        return payload

    async def delete_message(self, *, chat_id: str, message_id: int) -> bool:
        """Record a deletion, or report that Telegram refused one."""
        if not self.delete_allowed:
            return False
        self.deleted.append((chat_id, message_id))
        return True

    # -- Assertions helpers --------------------------------------------------

    @property
    def last_text(self) -> str:
        """Return the most recent thing said, posted or edited."""
        if self.edits:
            return self.edits[-1].text
        return self.sent[-1].text if self.sent else ""

    def texts(self) -> list[str]:
        """Return every posted and edited message body, in order of capture."""
        return [item.text for item in self.sent] + [item.text for item in self.edits]


@dataclass
class FakeUploader:
    """Records uploads and returns a plausible Telegram answer.

    It genuinely **reads** the stream it is handed, which matters: the provider
    wraps the artifact in a measuring reader, and progress and the receipt's
    checksum only exist because something consumes it. A fake that skipped the
    read would let a broken provider pass.
    """

    error: Exception | None = None
    uploads: list[dict[str, Any]] = field(default_factory=list)
    resends: list[dict[str, Any]] = field(default_factory=list)
    message_id: int = 500
    read_size: int = 8192

    async def send_media(  # noqa: PLR0913 - mirrors the protocol it stands in for
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
        """Drain the stream, record the upload, and answer as Telegram would."""
        if self.error is not None:
            raise self.error

        size = 0
        read = getattr(content, "read", None)
        if callable(read):
            while chunk := read(self.read_size):
                size += len(chunk)

        self.uploads.append(
            {
                "chat_id": chat_id,
                "kind": kind,
                "caption": caption,
                "filename": filename,
                "duration_seconds": duration_seconds,
                "width": width,
                "height": height,
                "thumbnail": thumbnail,
                "size": size,
            }
        )
        return UploadedMedia(
            message_id=self.message_id,
            chat_id=chat_id,
            file_id="FILE-ABC",
            file_unique_id="UNIQ-ABC",
            bytes_sent=size,
        )

    async def send_by_reference(
        self,
        *,
        chat_id: str,
        reference: str,
        kind: str,
        caption: str | None = None,
    ) -> UploadedMedia:
        """Record a re-send and answer without moving any bytes."""
        if self.error is not None:
            raise self.error
        self.resends.append(
            {"chat_id": chat_id, "reference": reference, "kind": kind, "caption": caption}
        )
        return UploadedMedia(
            message_id=self.message_id + 1,
            chat_id=chat_id,
            file_id=reference,
            file_unique_id="UNIQ-ABC",
            bytes_sent=0,
        )


# --------------------------------------------------------------------------- #
# Update builders                                                              #
# --------------------------------------------------------------------------- #


def message_update(
    text: str,
    *,
    update_id: int = 1,
    user_id: int = 4242,
    chat_id: int = 4242,
    message_id: int = 7,
    first_name: str = "Ada",
    username: str | None = "ada",
    is_bot: bool = False,
) -> dict[str, Any]:
    """Build a plain message update."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "text": text,
            "from": {
                "id": user_id,
                "is_bot": is_bot,
                "first_name": first_name,
                "username": username,
            },
            "chat": {"id": chat_id, "type": "private"},
        },
    }


def callback_update(
    data: str,
    *,
    update_id: int = 2,
    user_id: int = 4242,
    chat_id: int = 4242,
    message_id: int = 101,
    callback_id: str = "cb-1",
) -> dict[str, Any]:
    """Build a button-press update."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "data": data,
            "from": {"id": user_id, "is_bot": False, "first_name": "Ada"},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
        },
    }


class FakeCookieStore:
    """An in-memory cookie jar, validated the way the real store validates.

    Shares the real parser deliberately: a fake that accepted anything would
    let the handler tests pass while the only interesting behaviour - refusing
    a file that is not a jar - went untested.
    """

    def __init__(self) -> None:
        """Start with nothing stored."""
        self.content: bytes | None = None
        self.installs = 0

    async def install(self, content: bytes) -> CookieSummary:
        """Validate and keep the jar in memory."""
        try:
            parsed = parse(decode(content))
        except ValueError as exc:
            raise InvalidCookieJarError(str(exc)) from exc
        if parsed.cookie_count == 0:
            message = "no cookies found in that file"
            raise InvalidCookieJarError(message)

        self.content = content
        self.installs += 1
        return CookieSummary(
            cookie_count=parsed.cookie_count,
            domains=parsed.domains,
            earliest_expiry=parsed.earliest_expiry,
            installed_at=datetime.now(UTC),
            size_bytes=len(content),
        )

    async def describe(self) -> CookieSummary | None:
        """Return what is held, or nothing."""
        if self.content is None:
            return None
        return await self.install(self.content)

    async def discard(self) -> bool:
        """Forget the jar, reporting whether there was one."""
        had = self.content is not None
        self.content = None
        return had


def document_update(
    *,
    file_id: str = "FILE-1",
    file_name: str = "cookies.txt",
    file_size: int = 512,
    update_id: int = 3,
    user_id: int = 4242,
    chat_id: int = 4242,
    message_id: int = 9,
) -> dict[str, Any]:
    """Build an attached-file update."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "document": {
                "file_id": file_id,
                "file_name": file_name,
                "file_size": file_size,
                "mime_type": "text/plain",
            },
            "from": {"id": user_id, "is_bot": False, "first_name": "Ada", "username": "ada"},
            "chat": {"id": chat_id, "type": "private"},
        },
    }
