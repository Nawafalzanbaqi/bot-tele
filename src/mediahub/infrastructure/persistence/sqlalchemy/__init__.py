"""SQLAlchemy 2.0 (async) persistence adapters for PostgreSQL.

Layout:

* :mod:`~mediahub.infrastructure.persistence.sqlalchemy.base` - declarative
  base and the index/constraint naming convention.
* :mod:`~mediahub.infrastructure.persistence.sqlalchemy.models` - the physical
  schema. ORM models are *not* domain entities.
* :mod:`~mediahub.infrastructure.persistence.sqlalchemy.mappers` - explicit
  translation between rows and aggregates.
* :mod:`~mediahub.infrastructure.persistence.sqlalchemy.engine` - engine and
  session factory lifecycle.
* :mod:`~mediahub.infrastructure.persistence.sqlalchemy.unit_of_work` - the
  transaction boundary.
* :mod:`~mediahub.infrastructure.persistence.sqlalchemy.repositories` - the
  repository implementations.

**Why separate models from entities.** Letting the ORM map straight onto
aggregates is quicker to write and expensive forever: entities gain nullable
columns to satisfy the mapper, lazy loading fires queries from inside domain
logic, and a schema migration becomes a domain change. Explicit mappers cost a
few dozen lines and keep the schema and the model free to evolve apart.
"""

from __future__ import annotations
