"""One-time logging configuration.

:func:`configure_logging` is called exactly once per process - from the CLI
entry point and from the API lifespan - and is idempotent, so calling it twice
(as tests do) replaces the sinks rather than duplicating them.

Two output shapes are supported:

* **Text** (``logging.json_format = false``): coloured, aligned, meant for a
  terminal during development.
* **JSON** (``logging.json_format = true``): one object per line, produced by
  Loguru's ``serialize=True``, meant for a log collector in production.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.shared.logging.context import UNSET_CORRELATION_ID, get_correlation_id
from mediahub.shared.logging.intercept import configure_stdlib_logging
from mediahub.shared.logging.redaction import redact, scrub_record

if TYPE_CHECKING:  # pragma: no cover - typing only
    from loguru import Message, Record

    from mediahub.shared.config.settings import Settings

TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS!UTC}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[correlation_id]}</cyan> "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
"""Human-oriented format. Timestamps are always rendered in UTC."""


def _patch_record(record: Record) -> None:
    """Prepare every record before any sink formats it.

    Two jobs, in one patcher because Loguru allows exactly one.

    The redaction half is not optional and is not defence in depth: the bot
    token is part of the URL of every Telegram API call, and the HTTP client
    logs the URL it requested. Between the record and the sink is the only
    place that line can be stopped (:mod:`mediahub.shared.logging.redaction`).
    """
    record["extra"].setdefault("correlation_id", get_correlation_id())
    scrub_record(record)


def _redacting_stdout(message: Message) -> None:
    """Write one fully formatted line to stdout, redacted a second time.

    The patcher above scrubs ``message`` and ``extra`` before formatting, but a
    record's *exception* is rendered by Loguru after the patcher has run, and a
    traceback carries the text of every frame - including a request URL with
    the bot token in it. This is the last point where the whole line exists as
    text, JSON or not, so it is the one place a traceback can be caught.
    """
    sys.stdout.write(redact(str(message)))


def configure_logging(settings: Settings) -> None:
    """Install the process-wide logging configuration.

    Removes any existing sink, installs a single stdout sink shaped by
    ``settings.logging``, and redirects the standard logging hierarchy into it.

    Args:
        settings: The active configuration.
    """
    log_settings = settings.logging

    logger.remove()
    logger.configure(
        patcher=_patch_record,
        extra={"correlation_id": UNSET_CORRELATION_ID},
    )
    logger.add(
        _redacting_stdout,
        level=log_settings.level.value,
        format="{message}" if log_settings.json_format else TEXT_FORMAT,
        serialize=log_settings.json_format,
        colorize=not log_settings.json_format,
        backtrace=log_settings.backtrace,
        diagnose=log_settings.diagnose,
        enqueue=False,
        catch=True,
    )

    configure_stdlib_logging(level=log_settings.level.value)

    logger.bind(
        environment=settings.environment.value,
        backend=settings.database.backend.value,
    ).debug("Logging configured")
