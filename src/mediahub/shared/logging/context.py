"""Request-scoped logging context.

A correlation id is stored in a :class:`~contextvars.ContextVar`, which asyncio
propagates into every task spawned from the current one. That lets any layer
log with the right id without threading a parameter through every function
signature - the presentation layer sets it once per request and the Loguru
patcher reads it for every record.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import uuid4

UNSET_CORRELATION_ID = "-"
"""Value used for records emitted outside any request (startup, shutdown)."""

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default=UNSET_CORRELATION_ID)


def get_correlation_id() -> str:
    """Return the correlation id of the current context."""
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token[str]:
    """Bind ``value`` to the current context.

    Args:
        value: The identifier to bind; usually an inbound ``X-Request-ID``.

    Returns:
        A token that :func:`reset_correlation_id` can use to restore the
        previous value.
    """
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str]) -> None:
    """Restore the correlation id that was bound before ``token`` was issued."""
    _correlation_id.reset(token)


def new_correlation_id() -> str:
    """Return a fresh correlation id for a request that arrived without one."""
    return uuid4().hex
