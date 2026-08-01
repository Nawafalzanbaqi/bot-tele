"""ASGI middleware.

Two concerns, both cross-cutting and both cheap:

* :mod:`~mediahub.presentation.api.middleware.correlation` gives every request
  an id and puts it in the logging context.
* :mod:`~mediahub.presentation.api.middleware.access_log` records one
  structured line per request.

Order matters. Correlation is installed *outermost* so that the access log -
and every handler below it - already has an id to attach.
"""

from __future__ import annotations
