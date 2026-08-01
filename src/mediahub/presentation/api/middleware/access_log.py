"""Access log middleware.

Emits exactly one structured line per request, which is what makes logs
greppable and cheap to index. Uvicorn's own access log is disabled in
:mod:`mediahub.__main__` so that this is the single source of truth.

Only the path is logged, never the query string: query parameters routinely
carry tokens and personal data, and an access log is one of the easiest places
to leak them.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

SERVER_ERROR_THRESHOLD: Final[int] = 500
CLIENT_ERROR_THRESHOLD: Final[int] = 400
MILLISECONDS: Final[int] = 1000


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Logs one line per request, with method, path, status and duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Time the request and log its outcome, including failures."""
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * MILLISECONDS
            logger.bind(
                method=request.method,
                path=request.url.path,
                duration_ms=round(elapsed_ms, 2),
            ).error("{} {} failed", request.method, request.url.path)
            raise

        elapsed_ms = (time.perf_counter() - started) * MILLISECONDS
        bound = logger.bind(
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(elapsed_ms, 2),
        )
        message = "{} {} -> {}"
        args = (request.method, request.url.path, response.status_code)

        if response.status_code >= SERVER_ERROR_THRESHOLD:
            bound.error(message, *args)
        elif response.status_code >= CLIENT_ERROR_THRESHOLD:
            bound.warning(message, *args)
        else:
            bound.info(message, *args)

        return response
