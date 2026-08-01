"""Domain layer - the enterprise rules of MediaHub.

This is the innermost layer. It contains entities, value objects, domain
events, domain errors and repository *ports* (interfaces). It knows nothing
about FastAPI, SQLAlchemy, Loguru, HTTP, or configuration: the only imports
allowed here are the Python standard library and other domain modules.

Sub-packages:

* :mod:`mediahub.domain.common` - shared building blocks (entity/value-object
  bases, domain events, error hierarchy, pagination primitives).
* :mod:`mediahub.domain.media` - the media library aggregate.
* :mod:`mediahub.domain.download` - the download job aggregate.

Rule of thumb: if a change here is caused by a framework, a database or a
transport protocol, it belongs in another layer.
"""

from __future__ import annotations
