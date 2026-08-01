"""Infrastructure layer - adapters for everything outside the process.

Every module here implements a port declared by an inner layer. Nothing in
:mod:`mediahub.domain` or :mod:`mediahub.application` imports this package;
the connection is made once, in the composition root
(:mod:`mediahub.infrastructure.di.container`).

Sub-packages:

* :mod:`~mediahub.infrastructure.persistence` - SQLAlchemy and in-memory
  repository/unit-of-work implementations.
* :mod:`~mediahub.infrastructure.system` - clock and identifier generation.
* :mod:`~mediahub.infrastructure.messaging` - domain event publishing.
* :mod:`~mediahub.infrastructure.downloader` - the (not yet implemented)
  download engine.
* :mod:`~mediahub.infrastructure.di` - the composition root.

Replacing an adapter must never require a change to an inner layer. If it does,
the port is leaking implementation detail and should be redesigned.
"""

from __future__ import annotations
