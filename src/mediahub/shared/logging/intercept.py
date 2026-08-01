"""Bridge from the standard :mod:`logging` module to Loguru.

Third-party libraries - uvicorn, SQLAlchemy, asyncio - log through the standard
library. Without this bridge their records would bypass our sink entirely and
appear in a different format, or not at all. :class:`InterceptHandler` is
installed on the root logger so every record ends up in the same stream, with
the same fields and the same correlation id.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Final

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

MANAGED_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
    "fastapi",
    "sqlalchemy",
    "sqlalchemy.engine",
    "alembic",
    "asyncio",
)
"""Loggers whose own handlers are removed so records propagate to the root."""


class InterceptHandler(logging.Handler):
    """A :mod:`logging` handler that forwards every record to Loguru.

    The original module, function and line number are preserved by walking the
    stack until the frame that actually issued the call is found - otherwise
    every intercepted line would appear to originate from this file.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one standard-library record to the Loguru sink."""
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = sys._getframe(6), 6  # noqa: SLF001 - the documented Loguru recipe
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_stdlib_logging(
    *, level: str, managed_loggers: Iterable[str] = MANAGED_LOGGERS
) -> None:
    """Route the standard logging hierarchy into Loguru.

    Args:
        level: Minimum severity accepted by the root handler. Loguru applies
            its own per-sink threshold afterwards.
        managed_loggers: Loggers whose handlers are cleared so that their
            records propagate to the intercepted root logger.
    """
    logging.basicConfig(handlers=[InterceptHandler()], level=level, force=True)

    for name in managed_loggers:
        managed = logging.getLogger(name)
        managed.handlers = [InterceptHandler()]
        managed.propagate = False
