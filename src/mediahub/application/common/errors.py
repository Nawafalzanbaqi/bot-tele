"""Failures that belong to orchestration rather than to business rules.

Domain errors describe rules being broken ("this transition is illegal").
Application errors describe the *system* being unable to carry out an
otherwise-valid request ("no downloader is configured yet"). Both are mapped to
HTTP responses in :mod:`mediahub.presentation.api.errors`.
"""

from __future__ import annotations

from typing import ClassVar


class ApplicationError(Exception):
    """Base class for failures raised while orchestrating a use case.

    Attributes:
        code: Stable, machine-readable identifier for this failure category.
        message: Human-readable description, safe to show to API clients.
    """

    code: ClassVar[str] = "application_error"

    def __init__(self, message: str) -> None:
        """Initialise the error with a human-readable message."""
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and test failures."""
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class FeatureNotAvailableError(ApplicationError):
    """The request is valid but the capability is not wired up in this build.

    Used for seams that exist by design but have no adapter yet - see
    :class:`~mediahub.application.download.ports.DownloaderPort`. Distinct from
    a bug: the API answers ``501 Not Implemented`` rather than ``500``.
    """

    code: ClassVar[str] = "feature_not_available"


class PermissionDeniedError(ApplicationError):
    """The caller is not allowed to perform this operation.

    Declared now so authorisation has a home the day it is introduced; nothing
    raises it yet.
    """

    code: ClassVar[str] = "permission_denied"
