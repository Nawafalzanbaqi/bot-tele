"""Correlation id middleware.

Accepts an inbound ``X-Request-ID`` (so a reverse proxy or calling service can
propagate its own trace) or mints one, binds it to the logging context for the
lifetime of the request, and echoes it on the response.

The value is length-capped and sanitised: it ends up in log lines and in a
response header, and a client-supplied string must never be able to inject
newlines into either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.middleware.base import BaseHTTPMiddleware

from mediahub.shared.logging.context import (
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

REQUEST_ID_HEADER: Final[str] = "X-Request-ID"
MAX_ID_LENGTH: Final[int] = 128
_ALLOWED_EXTRA_CHARS: Final[frozenset[str]] = frozenset("-_.")


def _sanitise(raw: str) -> str:
    """Return a log-safe correlation id, or an empty string if unusable."""
    cleaned = "".join(
        char for char in raw.strip() if char.isalnum() or char in _ALLOWED_EXTRA_CHARS
    )
    return cleaned[:MAX_ID_LENGTH]


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Binds a correlation id to every request and its log records."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Bind an id for the duration of the request and echo it back."""
        inbound = _sanitise(request.headers.get(REQUEST_ID_HEADER, ""))
        correlation_id = inbound or new_correlation_id()

        token = set_correlation_id(correlation_id)
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            reset_correlation_id(token)

        response.headers[REQUEST_ID_HEADER] = correlation_id
        return response
