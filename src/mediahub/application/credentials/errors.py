"""Failures the credential store reports."""

from __future__ import annotations

from typing import ClassVar

from mediahub.application.common.errors import ApplicationError


class InvalidCookieJarError(ApplicationError):
    """The supplied content is not a usable cookie jar.

    Refusing is deliberate. Storing something unusable would leave the engine
    presenting an empty jar and failing with the platform's own message - "no
    video could be found in this post" - which points at the source rather than
    at the export that went wrong.
    """

    code: ClassVar[str] = "invalid_cookie_jar"


class CookieStoreUnavailableError(ApplicationError):
    """No cookie jar location is configured, or it cannot be written.

    Distinct from a bad jar: the content may be perfect and there is simply
    nowhere to put it, which is an operator's problem and needs a different
    answer.
    """

    code: ClassVar[str] = "cookie_store_unavailable"
