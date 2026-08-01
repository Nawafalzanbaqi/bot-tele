"""The HTTP API.

Structure:

* :mod:`~mediahub.presentation.api.app` - the application factory.
* :mod:`~mediahub.presentation.api.lifespan` - startup and shutdown.
* :mod:`~mediahub.presentation.api.dependencies` - container and use case
  injection.
* :mod:`~mediahub.presentation.api.errors` - RFC 9457 problem responses.
* :mod:`~mediahub.presentation.api.middleware` - correlation ids, access logs.
* :mod:`~mediahub.presentation.api.routers` - unversioned operational routes.
* :mod:`~mediahub.presentation.api.v1` - the versioned public API.

**Versioning.** Every business route lives under ``/api/v1``. When a breaking
change is needed, a ``v2`` package is added beside ``v1`` and both are served
until clients migrate; ``v1`` schemas are never edited in place. Health
endpoints stay unversioned because orchestrators depend on their path.
"""

from __future__ import annotations
