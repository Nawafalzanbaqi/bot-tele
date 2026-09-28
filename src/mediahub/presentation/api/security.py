"""Shared-secret authentication for the versioned API.

The API listens on the compose bridge, where every other container can reach
it, and is published on the host's loopback. It carries routes that change
state - archiving an item is irreversible, queueing and cancelling jobs is not
free - and until now nothing stood in front of them.

One header, compared in constant time against ``MEDIAHUB_SECURITY__SECRET_KEY``.
That key already had to be set to a non-placeholder value for production to
boot, and nothing used it; this is what it is for. Health stays open on
purpose: the container healthcheck and the host's monitoring call it without
credentials, and it discloses nothing worth protecting.
"""

from __future__ import annotations

import secrets
from typing import Final

from fastapi import HTTPException, Request, status

from mediahub.presentation.api.dependencies import SettingsDep

API_KEY_HEADER: Final[str] = "X-MediaHub-Key"
"""The header a caller presents. Its value is the configured secret key, verbatim."""


async def require_api_key(request: Request, settings: SettingsDep) -> None:
    """Refuse the request unless it carries the configured key.

    Raises:
        HTTPException: 401, with the header name in ``WWW-Authenticate`` so a
            caller learns *how* to authenticate and nothing else.
    """
    expected = settings.security.secret_key.get_secret_value().encode("utf-8")
    provided = request.headers.get(API_KEY_HEADER, "").encode("utf-8")
    # compare_digest runs in time that depends on the lengths, not the content,
    # which is what keeps the key from being guessed one byte at a time.
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )
