"""The messenger contract the gateway needs.

Declared in the presentation layer, on purpose. The gateway must not import an
adapter, and the delivery provider - which lives in infrastructure and declares
its own, different contract - must not import the gateway. Both are satisfied
by one concrete client that the composition root builds and hands to each.

The protocol is the *minimum*: poll, post, edit, acknowledge. Notably absent is
anything that uploads media. The gateway cannot send a file even by accident;
that is the delivery provider's job, reached through an application command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence


class TelegramMessenger(Protocol):
    """Reads updates and posts text. Everything the gateway needs."""

    async def get_updates(
        self, *, offset: int | None = None, timeout: int = 30
    ) -> Sequence[Mapping[str, Any]]:
        """Long-poll for updates, as plain dictionaries.

        Dictionaries rather than library objects: it is what keeps the client
        library out of this layer entirely.
        """
        ...

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Post a message and return its identifier."""
        ...

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> None:
        """Replace the text of a message already posted."""
        ...

    async def answer_callback(self, *, callback_id: str, text: str | None = None) -> None:
        """Acknowledge a button press so the client stops spinning."""
        ...

    async def download_file(self, *, file_id: str, max_bytes: int) -> bytes:
        """Fetch a small file the user uploaded, into memory.

        Deliberately narrow, and deliberately *not* symmetrical with the
        delivery provider's upload: this exists to receive a cookie jar, which
        is a few kilobytes of text. ``max_bytes`` is a required argument rather
        than a default so no caller can forget it - the gateway must never be
        the thing that reads an arbitrary-sized file into a 4 GB device.
        """
        ...

    async def delete_message(self, *, chat_id: str, message_id: int) -> bool:
        """Remove a message, returning whether it went.

        Used to get an uploaded credential out of the conversation once it has
        been stored. Best effort by nature: Telegram only lets a bot delete
        messages within a limited window, so the caller must treat ``False`` as
        "tell the user to delete it themselves", not as an error.
        """
        ...
