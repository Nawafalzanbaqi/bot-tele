"""Persistence adapters.

Two interchangeable implementations of the same domain ports:

* :mod:`~mediahub.infrastructure.persistence.sqlalchemy` - PostgreSQL via
  SQLAlchemy 2.0 async. The system of record.
* :mod:`~mediahub.infrastructure.persistence.memory` - in-process dictionaries.
  Used by tests and by ``MEDIAHUB_DATABASE__BACKEND=memory`` for demos.

Both satisfy :class:`~mediahub.domain.media.repository.MediaRepository`,
:class:`~mediahub.domain.download.repository.DownloadJobRepository` and
:class:`~mediahub.application.common.unit_of_work.UnitOfWork`, which is what
lets the entire test suite run without a database while production behaviour
stays identical.

Keeping the in-memory implementation honest - same ordering, same filtering,
same transactional semantics - is a maintenance obligation. When the two
diverge, the tests lie.
"""

from __future__ import annotations
