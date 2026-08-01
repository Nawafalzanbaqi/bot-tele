# 0008. A leased SQLite table is the job queue

- Status: Accepted
- Date: 2026-08-01
- Retracts the Redis service provisioned in Phase 01

## Context

Acquisition is slow, retryable and must survive power loss. That needs a durable
queue. The Foundation's Compose file provisioned Redis "reserved for the future
download queue" — infrastructure added for an anticipated need.

Requirements, honestly scoped: a few jobs per minute, one node, crash recovery,
priority, backoff, cancellation, a dead-letter path. Not: fan-out, distributed
workers, or 10 000 messages per second.

## Decision

The queue is a **table in the same SQLite database**, with lease-based ownership.

- Claim is one atomic `UPDATE … WHERE id IN (SELECT … LIMIT n) RETURNING *`.
  SQLite's global write lock makes this simpler than `FOR UPDATE SKIP LOCKED`,
  not a workaround for it.
- Ownership is a **lease** (`owner`, `expires_at`) extended by heartbeat.
  A crashed worker's lease expires and the job is reclaimed — this *is* crash
  recovery, with no additional mechanism.
- Retry backoff is `available_at`, evaluated by the claim query. No timer, no
  separate "retry" state, nothing to miss while the device is off.
- Separate lanes (acquisition, delivery, maintenance, enrichment) with
  independent concurrency budgets.
- Redis is removed from the stack.

## Consequences

**Better.** No broker process, no extra RAM, no second persistence
configuration, no additional failure domain on a device with none to spare.
Enqueue is transactional with the state change that caused it, which removes the
classic race where a job runs before its row is visible. One thing to back up.
The queue is inspectable with `sqlite3` and a `SELECT` — on a self-hosted box,
that is a real operational feature.

**Worse.** Claiming is a write, so it contends with every other writer; polling
replaces push (idle poll backs off to 5 s, so latency is up to a few seconds).
Throughput ceiling is far lower than a real broker — and far above this
workload.

**Reversible.** The `JobQueuePort` specifies semantics, not mechanism. Moving to
PostgreSQL (`SKIP LOCKED`) or a broker is an adapter change. Revisit if sustained
throughput exceeds ~50 jobs/second or a second node appears.
