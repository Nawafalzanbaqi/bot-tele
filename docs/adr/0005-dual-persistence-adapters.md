# 0005. Two persistence adapters, one contract

- Status: Accepted
- Date: 2026-08-01

## Context

Tests that need PostgreSQL are slow to run and slow to set up, so they get run
less often, and eventually they get skipped. The usual escapes are worse:

- **Mocking repositories** asserts that the code called the mock, not that it
  stored anything. Transactional behaviour - the part most likely to be wrong -
  goes completely untested.
- **SQLite as a stand-in** silently differs from Postgres on exactly the things
  that matter here: partial indexes, `SKIP LOCKED`, timezone handling.

## Decision

Provide two implementations of the repository and unit-of-work ports:

- `persistence/sqlalchemy` - PostgreSQL. The system of record.
- `persistence/memory` - dictionaries with snapshot isolation, selected by
  `MEDIAHUB_DATABASE__BACKEND=memory` and used by the entire test suite.

The in-memory unit of work checks out a deep copy on enter and publishes it
back only on `commit`, so uncommitted work is discarded exactly as a rolled-back
transaction would be. Ordering and filtering match the SQL adapter deliberately.

Production configuration refuses to start with the memory backend selected.

## Consequences

- The full suite - including every HTTP integration test - runs in about two
  seconds with no Docker and no database.
- Use cases are exercised for real: the transaction boundary, the rollback
  path and the repository contract are all covered.
- The API can be demonstrated with a single command and no infrastructure.
- Cost: **two implementations to keep honest.** If the in-memory adapter drifts
  from the SQL one, tests start lying. `tests/unit/infrastructure/` pins the
  transactional semantics and the ordering explicitly to make drift visible,
  and SQL-specific behaviour (partial unique index, `SKIP LOCKED`) still needs
  verification against a real database before it can be trusted in production.
