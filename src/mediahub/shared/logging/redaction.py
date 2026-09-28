"""Secret redaction, applied to every record before it reaches a sink.

**This is not defence in depth; it is the only defence.** MediaHub itself is
careful - the bot token is a ``SecretStr``, it is never interpolated into a
message, and it is never placed in a subprocess environment. None of that
helps, because the credential is *part of the URL* of every Telegram API call
and the HTTP client library logs the URL it requested:

    HTTP Request: POST https://api.telegram.org/bot<id>:<secret>/getMe "200 OK"

That line is written by ``httpx``, at INFO, on a logger this application does
not own. The only place it can be stopped is between the record and the sink,
which is what this module is.

The patterns are deliberately shaped around *where secrets appear*, not around
"anything that looks random". A redactor that guesses mangles ordinary logs and
teaches operators to distrust the redaction; one that names its cases can be
read, tested and extended.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from loguru import Record

PLACEHOLDER: Final[str] = "***REDACTED***"

_TELEGRAM_TOKEN = re.compile(r"(\d{6,12})(:|%3A|%3a)([A-Za-z0-9_-]{30,})")
"""A bot token, wherever it appears - in a URL, a message or a traceback.

Note the **absence of a leading** ``\\b``. The token's usual habitat is
``.../bot8653097410:<secret>/getMe``, where the digits are preceded by the ``t``
of "bot" - two word characters, so there is no word boundary there and a
``\\b`` would make this pattern silently never fire in exactly the place the
credential actually leaks.

The separator alternation matters for the same reason. A token that has been
through URL encoding carries ``%3A`` rather than ``:``, and that form appears
in precisely the paths a *self-hosted* Bot API server hands back - so matching
only the literal colon leaks the whole credential on a deployment that had
taken the trouble to run its own server. Found in production logs, not in
review.

The bot id before the colon is deliberately *kept*: it is not the secret half,
it is what makes a line attributable to one bot, and losing it would make a
multi-bot deployment impossible to debug.
"""

_URL_CREDENTIALS = re.compile(r"(://[^\s/:@]+):([^\s/@]+)@")
"""A password inside a connection URL, e.g. ``postgresql://user:pw@host``."""

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)"
    # The key, including any prefix: the value to protect is just as often
    # behind `MEDIAHUB_SECURITY__SECRET_KEY` as behind a bare `secret`, and an
    # underscore is a word character, so a `\b` here fails for the same reason
    # as above.
    r"([\w.\-\[\]]*(?:token|secret|password|passwd|api[_-]?hash|api[_-]?key|api[_-]?id"
    # Named key material, not a bare "key": WIREGUARD_PRIVATE_KEY and friends
    # reach the log through the environment dump of a crashing process, while
    # "key" alone would redact dictionary keys and cache keys all over the logs.
    r"|(?:private|preshared|signing|access|session)[_-]?key|authorization)"
    r"[\w.\-\[\]]*)"
    # The separator: `=` or `:`, optionally quoted on either side so JSON is
    # covered, and optionally followed by an auth scheme whose argument is the
    # part that matters.
    r"(['\"]?\s*[=:]\s*(?:bearer\s+|basic\s+)?['\"]?)"
    r"([^\s,;'\"}\)]+)"
)
"""``key=value`` for a key whose name says the value is a credential."""

_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "token",
        "bot_token",
        "secret",
        "secret_key",
        "password",
        "api_hash",
        "api_id",
        "api_key",
        "authorization",
        "private_key",
        "preshared_key",
    }
)
"""Structured ``extra`` fields replaced wholesale rather than pattern-matched."""


def redact(text: str) -> str:
    """Return ``text`` with any recognised credential replaced.

    Args:
        text: Arbitrary log text, from this application or from a dependency.

    Returns:
        The same text with secrets replaced by :data:`PLACEHOLDER`.
    """
    if not text:
        return text
    result = _TELEGRAM_TOKEN.sub(rf"\1\2{PLACEHOLDER}", text)
    result = _URL_CREDENTIALS.sub(rf"\1:{PLACEHOLDER}@", result)
    return _SENSITIVE_ASSIGNMENT.sub(rf"\1\2{PLACEHOLDER}", result)


def scrub_record(record: Record) -> None:
    """Redact one record in place, before any sink formats it.

    Installed as a Loguru patcher. Both halves matter: ``message`` carries the
    text a dependency logged, and ``extra`` carries the structured fields this
    application binds - a token reaching either one is equally published.
    """
    message = record.get("message")
    if isinstance(message, str):
        record["message"] = redact(message)

    extra = record.get("extra")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if key.lower() in _SENSITIVE_KEYS:
                extra[key] = PLACEHOLDER
            elif isinstance(value, str):
                extra[key] = redact(value)


def contains_secret(text: str, *, secrets: Iterable[str]) -> bool:
    """Return whether any of ``secrets`` survives in ``text``.

    The assertion the tests are actually interested in: not "does the redactor
    match its own pattern", but "is the real credential absent from the output".
    """
    return any(secret and secret in text for secret in secrets)
