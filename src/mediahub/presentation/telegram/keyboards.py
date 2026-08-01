"""Inline keyboards, and the payloads their buttons carry.

Telegram allows **64 bytes** of ``callback_data`` per button, and that data is
returned to us verbatim by a client we do not control. Two rules follow:

* nothing meaningful travels in it - no URL, no format expression, no user
  input. A button carries an action, a short session token and a short choice
  key, and the session holds the rest;
* everything coming back is parsed defensively and validated against the
  session before it is acted on.

A button tapped an hour after it was posted therefore fails cleanly, rather
than acquiring whatever the stale payload happened to say.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.download.dto import QualityOption

SEPARATOR: Final[str] = "|"
MAX_CALLBACK_BYTES: Final[int] = 64
CANCEL_KEY: Final[str] = "x"


class CallbackAction(StrEnum):
    """What a button press means.

    Values are single characters because every one of them competes for the
    same 64 bytes.
    """

    CHOOSE_QUALITY = "q"
    DISMISS = "d"
    ABORT = "a"


@dataclass(frozen=True, slots=True)
class CallbackPayload:
    """The decoded contents of a button press.

    Attributes:
        action: What the button means.
        token: Session token identifying the conversation this belongs to.
        choice: The option key, for :attr:`CallbackAction.CHOOSE_QUALITY`.
    """

    action: CallbackAction
    token: str
    choice: str | None = None

    def encode(self) -> str:
        """Return the wire form, refusing anything that will not fit.

        Raises:
            ValueError: If the payload exceeds Telegram's limit. Raising here
                is deliberate: a silently truncated payload becomes an
                unparsable one at the worst possible moment.
        """
        parts = [self.action.value, self.token, self.choice or ""]
        encoded = SEPARATOR.join(parts)
        if len(encoded.encode("utf-8")) > MAX_CALLBACK_BYTES:
            message = f"callback payload is {len(encoded)} bytes; the limit is 64"
            raise ValueError(message)
        return encoded


def decode_callback(raw: str | None) -> CallbackPayload | None:
    """Return the payload a button carried, or ``None`` if it is unusable.

    Total by design: a malformed payload is something an attacker can send, and
    the correct response is to ignore it, not to raise.
    """
    if not raw:
        return None
    parts = raw.split(SEPARATOR)
    expected_parts = 3
    if len(parts) != expected_parts:
        return None

    action_value, token, choice = parts
    try:
        action = CallbackAction(action_value)
    except ValueError:
        return None
    if not token or not token.isalnum():
        return None
    if choice and not _is_safe_choice(choice):
        return None
    return CallbackPayload(action=action, token=token, choice=choice or None)


def quality_keyboard(
    options: Sequence[QualityOption], *, token: str, columns: int = 2
) -> dict[str, Any]:
    """Build the keyboard offering the qualities a source supports.

    Args:
        options: What the application decided may be offered. The gateway never
            invents a choice of its own - it renders what it was given.
        token: Session token to embed in every button.
        columns: Buttons per row.

    Returns:
        An ``inline_keyboard`` markup dictionary.
    """
    buttons = [
        {
            "text": _button_label(option),
            "callback_data": CallbackPayload(
                action=CallbackAction.CHOOSE_QUALITY, token=token, choice=option.key
            ).encode(),
        }
        for option in options
    ]

    rows = [buttons[index : index + columns] for index in range(0, len(buttons), columns)]
    rows.append(
        [
            {
                "text": "Cancel",
                "callback_data": CallbackPayload(
                    action=CallbackAction.DISMISS, token=token, choice=CANCEL_KEY
                ).encode(),
            }
        ]
    )
    return {"inline_keyboard": rows}


def abort_keyboard(token: str) -> dict[str, Any]:
    """Build the single-button keyboard shown while a download is running."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Stop",
                    "callback_data": CallbackPayload(
                        action=CallbackAction.ABORT, token=token
                    ).encode(),
                }
            ]
        ]
    }


def _button_label(option: QualityOption) -> str:
    """Render a quality option as button text, with a size hint when useful."""
    if option.approx_bytes is None:
        return option.label
    return f"{option.label} · {_megabytes(option.approx_bytes)}"


def _megabytes(size: int) -> str:
    """Render a byte count compactly enough for a button."""
    megabytes = size / (1024 * 1024)
    if megabytes >= 1000:  # noqa: PLR2004 - readability threshold, not a rule
        return f"{megabytes / 1024:.1f} GB"
    return f"{megabytes:.0f} MB"


def _is_safe_choice(choice: str) -> bool:
    """Return whether an option key looks like one we could have issued."""
    max_choice_length = 12
    return len(choice) <= max_choice_length and choice.replace("_", "").isalnum()
