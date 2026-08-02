"""Turns raw Telegram update dictionaries into typed intents.

Everything arriving here is attacker-controlled: a message may be missing any
field, carry the wrong type, or contain megabytes of text. Parsing is therefore
total - it returns ``None`` for anything it does not understand rather than
raising - and every string is length-capped before it goes any further.

Pure and dictionary-based, so the whole surface is testable with literals and
no client library is involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

MAX_TEXT_LENGTH: Final[int] = 4096
MAX_NAME_LENGTH: Final[int] = 64
MAX_CALLBACK_LENGTH: Final[int] = 64
MAX_FILE_ID_LENGTH: Final[int] = 256


class IntentKind(StrEnum):
    """What an update is asking for.

    Attributes:
        COMMAND: A slash command such as ``/help``.
        TEXT: Free text, which the gateway treats as a candidate source.
        CALLBACK: An inline button press.
        DOCUMENT: An attached file. The only file the gateway accepts is a
            cookie jar, and only from an owner.
    """

    COMMAND = "command"
    TEXT = "text"
    CALLBACK = "callback"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class Sender:
    """Who sent an update.

    Attributes:
        user_id: Telegram's identifier for the sender, as text.
        display_name: Their visible name. Untrusted; display only.
    """

    user_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class Intent:
    """One understood request from Telegram.

    Attributes:
        kind: What sort of request it is.
        sender: Who sent it.
        chat_id: Where to reply.
        message_id: The message that carried it.
        command: The command word, without the slash, for ``COMMAND``.
        argument: Anything after the command word.
        text: The full text, for ``TEXT``.
        callback_id: Identifier to acknowledge, for ``CALLBACK``.
        callback_data: The button's payload, for ``CALLBACK``.
        update_id: Telegram's sequence number, used for de-duplication.
        file_id: Telegram's handle for an attachment, for ``DOCUMENT``.
        file_name: The attachment's claimed name. Attacker-controlled; shown
            back to the sender and never used as a path.
        file_size: The attachment's declared size. Also attacker-controlled, so
            it is worth a cheap rejection but never a guarantee.
    """

    kind: IntentKind
    sender: Sender
    chat_id: str
    update_id: int
    message_id: int | None = None
    command: str | None = None
    argument: str | None = None
    text: str | None = None
    callback_id: str | None = None
    callback_data: str | None = None
    file_id: str | None = None
    file_name: str | None = None
    file_size: int | None = None


def parse_update(update: Mapping[str, Any]) -> Intent | None:
    """Return the intent an update carries, or ``None`` if it carries none.

    Unrecognised updates - edited messages, channel posts, polls, joins - are
    silently ignored. A gateway that raised on them would stop polling the
    first time someone edited a message.
    """
    update_id = _int(update.get("update_id"))
    if update_id is None:
        return None

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return _parse_callback(callback, update_id)

    message = update.get("message")
    if isinstance(message, dict):
        return _parse_message(message, update_id)

    return None


def _parse_callback(callback: Mapping[str, Any], update_id: int) -> Intent | None:
    """Parse a button press."""
    sender = _sender(callback.get("from"))
    message = callback.get("message")
    if sender is None or not isinstance(message, dict):
        return None

    chat_id = _chat_id(message.get("chat"))
    callback_id = _text(callback.get("id"), MAX_NAME_LENGTH)
    if chat_id is None or callback_id is None:
        return None

    return Intent(
        kind=IntentKind.CALLBACK,
        sender=sender,
        chat_id=chat_id,
        update_id=update_id,
        message_id=_int(message.get("message_id")),
        callback_id=callback_id,
        callback_data=_text(callback.get("data"), MAX_CALLBACK_LENGTH),
    )


def _parse_message(message: Mapping[str, Any], update_id: int) -> Intent | None:
    """Parse a text message, a slash command, or an attached file."""
    sender = _sender(message.get("from"))
    chat_id = _chat_id(message.get("chat"))
    if sender is None or chat_id is None:
        return None

    message_id = _int(message.get("message_id"))

    document = message.get("document")
    if isinstance(document, dict):
        return _parse_document(document, sender, chat_id, update_id, message_id)

    text = _text(message.get("text"), MAX_TEXT_LENGTH)
    if not text:
        return None

    if text.startswith("/"):
        head, _, tail = text.partition(" ")
        # "/help@SomeBot" in a group carries the bot's username; drop it.
        command = head[1:].split("@", maxsplit=1)[0].lower()
        return Intent(
            kind=IntentKind.COMMAND,
            sender=sender,
            chat_id=chat_id,
            update_id=update_id,
            message_id=message_id,
            command=command or None,
            argument=tail.strip() or None,
            text=text,
        )

    return Intent(
        kind=IntentKind.TEXT,
        sender=sender,
        chat_id=chat_id,
        update_id=update_id,
        message_id=message_id,
        text=text,
    )


def _parse_document(
    document: Mapping[str, Any],
    sender: Sender,
    chat_id: str,
    update_id: int,
    message_id: int | None,
) -> Intent | None:
    """Parse an attached file.

    The name is carried only so it can be echoed back to whoever sent it; it is
    never used to build a path. What the file *is* gets decided by reading it,
    not by trusting an extension somebody else chose.
    """
    file_id = _text(document.get("file_id"), MAX_FILE_ID_LENGTH)
    if file_id is None:
        return None
    return Intent(
        kind=IntentKind.DOCUMENT,
        sender=sender,
        chat_id=chat_id,
        update_id=update_id,
        message_id=message_id,
        file_id=file_id,
        file_name=_text(document.get("file_name"), MAX_NAME_LENGTH),
        file_size=_int(document.get("file_size")),
    )


def _sender(raw: object) -> Sender | None:
    """Extract the sender, refusing bots and anything malformed."""
    if not isinstance(raw, dict):
        return None
    user_id = _int(raw.get("id"))
    if user_id is None:
        return None
    if raw.get("is_bot") is True:
        return None
    first = _text(raw.get("first_name"), MAX_NAME_LENGTH) or ""
    username = _text(raw.get("username"), MAX_NAME_LENGTH)
    return Sender(user_id=str(user_id), display_name=username or first or None)


def _chat_id(raw: object) -> str | None:
    """Extract the conversation identifier."""
    if not isinstance(raw, dict):
        return None
    chat_id = _int(raw.get("id"))
    return None if chat_id is None else str(chat_id)


def _int(value: object) -> int | None:
    """Coerce a field to an int, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object, limit: int) -> str | None:
    """Coerce a field to a bounded, stripped string, or ``None``."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()[:limit]
    return trimmed or None
