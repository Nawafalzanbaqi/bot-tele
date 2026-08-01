# 0003. Explicit ORM mapping instead of active record

- Status: Accepted
- Date: 2026-08-01

## Context

SQLAlchemy can map the ORM directly onto domain classes, and many projects do:
one class is both the aggregate and the row. It removes a mapper module and
feels economical.

The costs show up later, and all of them land on the domain:

- Entities acquire nullable columns and default values that exist only to
  satisfy the mapper, weakening the invariants the type was supposed to
  guarantee.
- Lazy loading fires database queries from inside domain logic - including,
  eventually, from inside an async context where it raises instead.
- The identity map means a "domain object" is quietly attached to a session,
  so its behaviour depends on transaction state.
- Every schema migration becomes a domain change, and vice versa.

SQLAlchemy's imperative mapping is a third option, but it hides the translation
in configuration rather than making it readable.

## Decision

Keep two separate representations:

- ORM models in `infrastructure/persistence/sqlalchemy/models.py` describe
  storage only - no behaviour, no relationships that let callers wander the
  object graph.
- Domain aggregates describe behaviour and know nothing about the database.

Translate explicitly in `mappers.py`, one function per direction per aggregate.
Aggregates are rebuilt through their full constructor, so a row that violates
an invariant fails loudly at load time.

## Consequences

- The schema and the model evolve independently. Denormalising
  `priority_weight` for an index required no domain change.
- Domain objects are always detached plain Python; no query can fire from
  inside a business rule.
- Loading is eager and predictable - no accidental N+1 from attribute access.
- Cost: roughly 150 lines of mapper, and every new field must be added in two
  places. A missed field is caught by mypy or by a test, not at runtime in
  production.
