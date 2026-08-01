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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from loguru import Record

    from mediahub.shared.config.settings import Settings

TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS!UTC}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[correlation_id]}</cyan> "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
"""Human-oriented format. Timestamps are always rendered in UTC."""


def _inject_correlation_id(record: Record) -> None:
    """Add the current correlation id to every record before it is formatted.

    Loguru calls this patcher for each record, which is why no call site ever
    has to pass the id explicitly.
    """
    record["extra"].setdefault("correlation_id", get_correlation_id())


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
        patcher=_inject_correlation_id,
        extra={"correlation_id": UNSET_CORRELATION_ID},
    )
    logger.add(
        sys.stdout,
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
