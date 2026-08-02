"""No credential reaches a log sink.

This suite exists because of a real leak, observed in production logs on the
target device: the Telegram gateway started, and the very first line written by
the HTTP client carried the whole bot token, because the token is part of the
URL of every Bot API call.

The assertions are therefore written the way the incident was found - take the
literal string a sink received, and look for the literal secret in it. Asserting
that a regular expression matches would prove the redactor recognises its own
pattern, which is not the property anyone cares about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from loguru import logger

from mediahub.shared.config.settings import (
    Environment,
    LoggingSettings,
    SecuritySettings,
    Settings,
    TelegramSettings,
)
from mediahub.shared.logging.redaction import PLACEHOLDER, contains_secret, redact
from mediahub.shared.logging.setup import configure_logging

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

pytestmark = pytest.mark.security

BOT_ID = "8653097410"
BOT_SECRET = "AAH0xCp2jD-nfN3DAMlx7nSCU-e9Bf4qSPc"
BOT_TOKEN = f"{BOT_ID}:{BOT_SECRET}"


@pytest.fixture
def sink() -> Iterator[list[str]]:
    """Capture what a sink actually receives, after every patcher has run."""
    written: list[str] = []
    configure_logging(
        Settings(
            _env_file=None,
            environment=Environment.TESTING,
            logging=LoggingSettings(),
            security=SecuritySettings(),
            telegram=TelegramSettings(),
        )
    )
    handler = logger.add(written.append, format="{message} {extra}", level="DEBUG")
    yield written
    logger.remove(handler)


# -- The observed leak -------------------------------------------------------


def test_the_line_that_leaked_no_longer_leaks(sink: list[str]) -> None:
    """The exact shape written by httpx when the gateway starts."""
    logger.info(f'HTTP Request: POST https://api.telegram.org/bot{BOT_TOKEN}/getMe "200 OK"')

    (line,) = sink
    assert not contains_secret(line, secrets=[BOT_SECRET])
    assert PLACEHOLDER in line


def test_the_bot_id_is_kept_so_a_line_stays_attributable(sink: list[str]) -> None:
    """The id before the colon is not the secret half.

    Redacting it too would make a multi-bot deployment impossible to debug,
    which is how a redactor ends up switched off.
    """
    logger.info(f"calling https://api.telegram.org/bot{BOT_TOKEN}/sendVideo")

    (line,) = sink
    assert BOT_ID in line
    assert BOT_SECRET not in line


def test_a_token_in_a_bound_field_is_redacted(sink: list[str]) -> None:
    """`extra` is published exactly as much as the message is."""
    logger.bind(bot_token=BOT_TOKEN).info("gateway ready")

    (line,) = sink
    assert not contains_secret(line, secrets=[BOT_SECRET, BOT_TOKEN])


def test_a_token_inside_a_traceback_is_redacted(sink: list[str]) -> None:
    """A failed API call raises with the URL in the message."""
    logger.error(f"ConnectError: POST https://api.telegram.org/bot{BOT_TOKEN}/getUpdates failed")

    (line,) = sink
    assert BOT_SECRET not in line


# -- The other credentials this process holds --------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("postgresql+asyncpg://mediahub:hunter2@db:5432/mediahub", "hunter2"),
        ("MEDIAHUB_SECURITY__SECRET_KEY=9f1c2e0a5b7d4f31a0c6e8b2d4f60193", "9f1c2e0a5b7d"),
        ("TELEGRAM_API_HASH: 0123456789abcdef0123456789abcdef", "0123456789abcdef"),
        ('{"password": "s3cr3t-value"}', "s3cr3t-value"),
        ("Authorization=Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
    ],
    ids=["dsn-password", "secret-key", "api-hash", "json-password", "bearer"],
)
def test_other_credentials_are_redacted(text: str, secret: str) -> None:
    assert secret not in redact(text)


# -- What must NOT be mangled ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Download complete in 12.4s",
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        "job 6ff25c04-4ece-44ca-9926-c819d84df68a moved to running",
        "delivered 28523658 bytes to chat 367516248",
        "PRAGMA journal_mode=wal",
    ],
    ids=["timing", "source-url", "job-id", "delivery", "pragma"],
)
def test_ordinary_lines_survive_untouched(text: str) -> None:
    """A redactor that guesses mangles real logs and gets switched off.

    Note the source URL in particular: a submitted link is the single most
    useful thing in a support conversation, and a redactor that ate it would
    make the logs worthless for the one job they have.
    """
    assert redact(text) == text


def test_redaction_survives_the_json_sink(sink: list[str]) -> None:
    """Production serialises records; the patcher must run before that."""
    logger.bind(url=f"https://api.telegram.org/bot{BOT_TOKEN}/getMe").info("request")

    (line,) = sink
    assert BOT_SECRET not in line


class TestUrlEncodedTokens:
    """A token that has been through URL encoding is still a token.

    Found in production logs, not in review: a self-hosted Bot API server
    hands back file paths containing the token with the colon percent-encoded,
    so a pattern matching only the literal colon published the whole
    credential on exactly the deployments that had taken the trouble to run
    their own server.
    """

    def test_a_percent_encoded_token_is_redacted(self, sink: list[str]) -> None:
        logger.info(
            f"GET https://api.telegram.org/file/bot{BOT_ID}%3A{BOT_SECRET}"
            f"/var/lib/telegram-bot-api/documents/file_0.txt"
        )

        (line,) = sink
        assert not contains_secret(line, secrets=[BOT_SECRET])
        assert PLACEHOLDER in line

    def test_the_lowercase_encoding_is_redacted_too(self) -> None:
        assert BOT_SECRET not in redact(f"bot{BOT_ID}%3a{BOT_SECRET}/getFile")

    def test_the_bot_id_still_survives_encoding(self) -> None:
        """Attributability must not depend on which encoding was used."""
        assert BOT_ID in redact(f"bot{BOT_ID}%3A{BOT_SECRET}/getMe")
