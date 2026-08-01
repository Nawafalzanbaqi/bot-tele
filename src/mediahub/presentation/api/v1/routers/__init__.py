"""Route handlers for version 1 of the API.

Each handler is four lines of work: build a command or query, await the use
case, convert the DTO to a response schema, return it. Errors are not caught
here - the handlers registered in :mod:`mediahub.presentation.api.errors` turn
every domain and application error into the right status code, which keeps
``try``/``except`` out of every route.
"""

from __future__ import annotations
