# 0006. SQLite as the system of record

- Status: Accepted
- Date: 2026-08-01
- Supersedes the PostgreSQL assumption in ADR-0005's context

## Context

The Foundation phase was built against PostgreSQL, a reasonable default for a
server application. Phase 02 fixed the deployment target: **a Raspberry Pi in a
home, operated by its owner**, with a workload of a few jobs per minute, a
single node, and no database administrator.

Against that target, PostgreSQL costs a second container, 100–200 MB of RAM on a
2–4 GB device, a connection pool, its own backup and restore procedure, and
another process that can fail at 3 a.m. It buys concurrency and features the
workload will not use.

## Decision

Use **SQLite** as the system of record: one file, WAL journal mode, accessed
through SQLAlchemy 2 + `aiosqlite`, migrated with Alembic in batch mode.

Mandatory configuration, all of which the Foundation lacked:

- `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout`
- `PRAGMA foreign_keys=ON` **per connection** — otherwise every `ondelete`
  clause is silently inert
- A `UtcDateTime` type decorator — SQLite has no tz-aware type and returns naive
  datetimes, which the domain correctly rejects
- `sqlite_where=` for partial indexes; `postgresql_where=` is silently dropped
- `render_as_batch=True` in Alembic — SQLite cannot `ALTER` most columns
- Online backup API for backups; never `cp` a live WAL database

## Consequences

**Better.** One file to back up (a year of history is tens of megabytes). No
second process, no pool tuning, no server to secure. Transactional enqueue for
free, which is what makes the SQLite job queue possible (ADR-0008). Tests run
against the real engine in milliseconds.

**Worse.** A single writer: sustained concurrent writes serialise, so
transactions must be short and progress writes throttled. No `SKIP LOCKED` —
claiming uses `UPDATE … RETURNING` instead. Four PostgreSQL-shaped defects in
the Foundation must be fixed before anything is built on them
(docs/architecture/20 §§4.7–4.10) — three of which fail *silently*.

**Accepted risk.** A single-node ceiling. The migration path to PostgreSQL is
confined to `infrastructure/persistence/` because the repository and queue ports
specify *semantics*, not mechanisms. That containment is the reason this
decision is safe to make now and reversible later.
