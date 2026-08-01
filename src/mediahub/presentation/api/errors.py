"""Error translation for the HTTP surface.

Errors are mapped from *category*, never from concrete type. A new domain error
that derives from :class:`~mediahub.domain.common.errors.ConflictError`
automatically answers ``409`` - nobody has to remember to update a table here.

Responses follow RFC 9457 (Problem Details for HTTP APIs), so a client gets a
predictable body for every failure::

    {
      "type": "about:blank",
      "title": "Conflict",
      "status": 409,
      "detail": "A media item is already registered for 'https://example.com/a'.",
      "code": "duplicate_media",
      "instance": "/api/v1/media",
      "correlation_id": "9f1c..."
    }

``code`` is the stable, machine-readable field: clients branch on it, not on
``detail``, which is prose and may change.

Unexpected exceptions are logged in full and answered with a generic ``500``.
Internal details are only echoed to the client when ``debug`` is enabled and
the environment is not production.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from fastapi import status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from mediahub.application.common.errors import (
    ApplicationError,
    FeatureNotAvailableError,
    PermissionDeniedError,
)
from mediahub.domain.common.errors import (
    ConflictError,
    DomainError,
    EntityNotFoundError,
    InvalidStateTransitionError,
    InvariantViolationError,
)
from mediahub.shared.logging.context import get_correlation_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI, Request

PROBLEM_CONTENT_TYPE: Final[str] = "application/problem+json"

UNPROCESSABLE: Final[int] = HTTPStatus.UNPROCESSABLE_ENTITY.value
"""422, taken from the standard library: Starlette renamed its own constant."""

_ERROR_STATUS_MAP: Final[tuple[tuple[type[Exception], int], ...]] = (
    # Order matters: the first matching entry wins, so subclasses come first.
    (EntityNotFoundError, status.HTTP_404_NOT_FOUND),
    (InvalidStateTransitionError, status.HTTP_409_CONFLICT),
    (ConflictError, status.HTTP_409_CONFLICT),
    (InvariantViolationError, UNPROCESSABLE),
    (DomainError, status.HTTP_400_BAD_REQUEST),
    (FeatureNotAvailableError, status.HTTP_501_NOT_IMPLEMENTED),
    (PermissionDeniedError, status.HTTP_403_FORBIDDEN),
    (ApplicationError, status.HTTP_400_BAD_REQUEST),
)


class ProblemDetail(BaseModel):
    """RFC 9457 problem document returned for every failed request.

    Attributes:
        type: URI identifying the problem type; ``about:blank`` when the status
            code alone describes it.
        title: Short, human-readable summary of the problem type.
        status: The HTTP status code, repeated in the body for convenience.
        detail: Explanation specific to this occurrence.
        code: Stable, machine-readable error identifier. Branch on this.
        instance: The path that produced the problem.
        correlation_id: Ties the response to the server-side log entries.
    """

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    instance: str | None = None
    correlation_id: str = Field(default_factory=get_correlation_id)


def problem_response(
    *,
    request: Request,
    status_code: int,
    detail: str,
    code: str,
) -> JSONResponse:
    """Build a problem-details response for one failure."""
    problem = ProblemDetail(
        title=HTTPStatus(status_code).phrase,
        status=status_code,
        detail=detail,
        code=code,
        instance=request.url.path,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(mode="json"),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def status_for(error: Exception) -> int:
    """Return the HTTP status that matches an error's category."""
    for error_type, status_code in _ERROR_STATUS_MAP:
        if isinstance(error, error_type):
            return status_code
    return status.HTTP_500_INTERNAL_SERVER_ERROR


async def handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    """Translate a domain or application error into a problem response."""
    status_code = status_for(exc)
    code = getattr(exc, "code", "error")
    detail = getattr(exc, "message", str(exc))

    logger.bind(error_code=code, status_code=status_code, path=request.url.path).info(
        "Request rejected: {}", detail
    )
    return problem_response(request=request, status_code=status_code, detail=detail, code=code)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Translate a request-schema violation into a ``422`` problem response."""
    detail = "The request body or parameters did not pass validation."
    if isinstance(exc, RequestValidationError):
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )

    logger.bind(path=request.url.path).info("Request validation failed: {}", detail)
    return problem_response(
        request=request,
        status_code=UNPROCESSABLE,
        detail=detail,
        code="request_validation_failed",
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Render Starlette's own ``HTTPException`` in problem-details form."""
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - defensive
        return await handle_unexpected_error(request, exc)

    return problem_response(
        request=request,
        status_code=exc.status_code,
        detail=str(exc.detail),
        code=HTTPStatus(exc.status_code).name.lower(),
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Log an unhandled exception in full and answer with a generic ``500``.

    The stack trace goes to the log, never to the client: an error body is an
    excellent place to leak file paths, queries and secrets.
    """
    logger.opt(exception=exc).error("Unhandled error while serving {}", request.url.path)

    debug = bool(getattr(request.app.state, "debug", False))
    detail = f"{type(exc).__name__}: {exc}" if debug else "An unexpected error occurred."
    return problem_response(
        request=request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=detail,
        code="internal_error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler above to the application.

    Args:
        app: The FastAPI application being assembled.
    """
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(ApplicationError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected_error)
