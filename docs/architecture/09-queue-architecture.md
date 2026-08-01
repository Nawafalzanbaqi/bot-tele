# 09. Queue Architecture

The queue is the seam between "the user asked" and "the machine did". On a
single Raspberry Pi with no UPS, its only non-negotiable property is: **a job
that was accepted is never silently lost.**

---

## 9.1 Decision: the queue is a table

| Option | RAM | Extra process | Survives power cut | Verdict |
| ------ | --- | ------------- | ------------------ | ------- |
| **SQLite table + leases** | ~0 | no | yes (WAL + fsync) | **Chosen** |
| Redis + RQ/arq | 50–200 MB | yes | only with AOF fsync (SD wear) | Rejected |
| Celery + Redis/Rabbit | 200 MB+ | 2+ | yes | Rejected |
| In-memory `asyncio.Queue` | ~0 | no | **no** | Rejected outright |
| Filesystem spool | ~0 | no | partially | Rejected — reimplements a database, badly |

Reasoning:

- **The database already exists and is already durable.** A second durable store
  means two things to back up, two to restore, and two ways for them to disagree
  about whether a job ran.
- **Throughput needed: a few jobs per minute at peak.** SQLite handles orders of
  magnitude more. Redis would be solving a problem that does not exist.
- **Transactional enqueue.** Creating an asset and enqueuing its job in one
  transaction is free here, and requires an outbox with any external broker.
  Getting this wrong produces the classic bug: job runs before its row is
  visible.
- **One fewer failure domain.** Every additional daemon on a Pi is another thing
  that OOMs at 3 a.m.

Recorded as [ADR-0008](../adr/0008-sqlite-job-queue.md). Phase 01's Compose file
provisions Redis "for the future queue" — that must be removed
([20](20-architecture-decision-review.md) §4.5).

**When this decision expires:** sustained >50 jobs/second, or more than one
node. Both are far outside the product's design point, and the migration path
(Postgres + `SKIP LOCKED`, same port, same semantics) is a persistence-layer
change.

---

## 9.2 Lanes

Separate lanes with independent concurrency budgets. A single FIFO would let one
3-hour download block every 5-second re-delivery — technically correct, visibly
broken.

| Lane | Work | Concurrency (Pi 4) | Max attempts | Lease | Priority source |
| ---- | ---- | ------------------ | ------------ | ----- | --------------- |
| `acquisition` | download → process | 1–2 | 3 | 120 s | user + ageing |
| `delivery` | upload / send by ref | 2 | 5 | 60 s | always high |
| `maintenance` | sweeps, cleanup, backups | 1 | 3 | 60 s | lowest |
| `enrichment` | AI annotations | 0–1 | 2 | 300 s | lowest |

Lanes are rows in one table (`lane` column), not separate tables: one claim
implementation, one set of indexes, one place for bugs.

Delivery is ranked above acquisition deliberately — it is short, user-visible,
and it is what releases disk space.

---

## 9.3 Queue record

Specification of the fields the queue itself needs. The domain aggregate
([06](06-domain-model.md) §6.4) carries the business fields; these are the
scheduling mechanics.

| Field | Purpose |
| ----- | ------- |
| `id`, `lane`, `job_type` | identity and routing |
| `payload_ref` | **identifiers only** — never a serialised aggregate |
| `status` | `queued` / `running` / terminal |
| `priority_weight` | base ordering |
| `available_at` | backoff / scheduling gate |
| `enqueued_at` | ageing input (anti-starvation) |
| `expires_at` | deadline |
| `attempts`, `max_attempts` | retry budget |
| `lease_owner`, `lease_expires_at` | ownership and crash detection |
| `cancel_requested` | cooperative cancellation |
| `last_error_kind`, `last_error_code` | triage without opening a payload |
| `version` | optimistic concurrency |

**Indexes** (the only ones that matter):

```
ix_queue_claim   ON (lane, status, available_at, priority_weight DESC, enqueued_at)
ix_queue_lease   ON (status, lease_expires_at)   -- reaper
ix_queue_payload ON (payload_ref)                -- idempotency lookups
```

`payload_ref` holding identifiers rather than data is a rule with teeth: a
payload that embeds a copy of the job is stale the moment it is claimed, and
produces the worst class of bug — work performed against state that no longer
exists.

---

## 9.4 The claim

One atomic statement. No `SELECT` then `UPDATE`, no application-level locking,
no advisory locks.

```sql
-- specification, not implementation
UPDATE queue
   SET status            = 'running',
       lease_owner       = :worker_id,
       lease_expires_at  = :now + :lease_duration,
       attempts          = attempts + 1,
       version           = version + 1,
       started_at        = COALESCE(started_at, :now)
 WHERE id IN (
       SELECT id FROM queue
        WHERE lane = :lane
          AND status = 'queued'
          AND available_at <= :now
          AND (expires_at IS NULL OR expires_at > :now)
          AND cancel_requested = 0
        ORDER BY (priority_weight
                  + MIN(:age_cap, (:now - enqueued_at) / :age_step)) DESC,
                 enqueued_at ASC
        LIMIT :batch
 )
RETURNING *;
```

Why this shape:

- **SQLite serialises writers**, so the `UPDATE` is the lock. Two workers
  cannot both claim a row; the loser sees zero rows and backs off. This is the
  SQLite equivalent of `FOR UPDATE SKIP LOCKED`, and it is *simpler*, not a
  workaround.
- **`RETURNING`** (SQLite ≥ 3.35) avoids a second round trip and the race
  between claim and read.
- **Ageing inside `ORDER BY`** prevents starvation without a background
  re-prioritising job: a `LOW` job that has waited long enough overtakes fresh
  `NORMAL` work. `age_cap` bounds the boost so priority still means something.
- **`attempts` is incremented at claim**, not at failure. A worker that dies
  without reporting has still consumed an attempt — otherwise a job that
  reliably kills its worker is retried forever.
  Counter-rule: a *graceful* release decrements it back ([08](08-state-machine.md) §8.4).

**Claim ordering is advisory, not a guarantee.** Under concurrency the effective
order can differ slightly. Anything requiring strict ordering (rare) must model
it as a dependency, not assume queue order.

---

## 9.5 Retry and backoff

```
delay(attempt) = min(max_backoff,
                     base × 2^(attempt-1)) × (1 ± jitter)
available_at   = now + delay
```

Defaults: `base=30 s`, `max=30 min`, `jitter=±20%`, `max_attempts=3`
(acquisition) / `5` (delivery).

Overrides in order of precedence:

1. **Provider-supplied `retry_after`** (Telegram `429`, HTTP `Retry-After`) —
   always wins. Guessing when a provider has told you the answer is
   self-inflicted damage.
2. Failure-kind rules — `insufficient_disk` uses a long flat delay (5 min): the
   disk will not free itself in 30 seconds, and hammering it wastes wakeups.
3. Policy defaults.

**Jitter is not optional.** Without it, ten jobs failing on the same provider
outage retry in lockstep forever, producing a self-inflicted thundering herd
against a service that is already unhappy.

**Only `TRANSIENT` failures retry.** `PERMANENT` and `POLICY` go straight to
`FAILED` ([07](07-download-pipeline.md) §7.7).

---

## 9.6 Dead letter queue

Reaching `max_attempts` on a transient failure moves the job to
`DEAD_LETTERED` and writes a **dead letter record**:

| Field | Why |
| ----- | --- |
| `job_id`, `lane`, `job_type` | identity |
| `payload_ref` | to requeue |
| `attempts`, `first_failed_at`, `last_failed_at` | shape of the failure |
| `failure_history` | every `FailureReport`, in order — the pattern is the diagnosis |
| `correlation_ids` | to find the logs |
| `requeued_as` | link to the new job if a human retried it |

Design rules:

- The DLQ is a **table, not a lane**. Nothing claims from it. It is an inbox for
  a person.
- Requeuing creates a **new** job linked to the dead letter. Terminal states are
  never reanimated.
- A non-empty DLQ raises an alert ([16](16-monitoring.md) §16.6). A silent DLQ
  is a queue that has quietly stopped delivering.
- Dead letters are retained for `dlq_retention_days` (default 90) and then
  summarised into history, so the DLQ cannot grow without bound.

---

## 9.7 Cancellation

The queue supports cancellation in two shapes:

| Job state | Mechanism | Latency |
| --------- | --------- | ------- |
| `queued` | `UPDATE … SET status='cancelled' WHERE status='queued'` | immediate |
| `running` | `cancel_requested = 1`; worker observes at its next checkpoint or heartbeat | ≤ 1 heartbeat (30 s) |

There is deliberately **no forced kill**. Terminating a worker mid-write risks
partial files, orphaned subprocesses and half-written database rows; the sweeper
would then have to clean up after a mess that cooperative cancellation avoids
entirely. Where a hard stop is genuinely required (operator emergency), the
correct action is to stop the container — the lease then expires and the job is
reclaimed normally.

---

## 9.8 Idempotency and duplicate suppression

Three independent mechanisms, because they catch different mistakes:

| Level | Mechanism | Prevents |
| ----- | --------- | -------- |
| Command | `idempotency_key`, replayed for 24 h | double-tapped button, repeated Telegram update |
| Domain | partial unique index: ≤1 active job per asset | two jobs racing for one asset |
| Stage | each stage re-runnable, checkpointed | duplicated work after a lease reclaim |

The middle one is the load-bearing guarantee, and it must be a **database
constraint**, not an application check — application checks lose races by
definition.

---

## 9.9 Queue health signals

| Metric | Meaning | Alert |
| ------ | ------- | ----- |
| `queue_depth{lane,status}` | backlog | depth > 100 for 10 min |
| `queue_oldest_available_age` | starvation / stall | > 30 min in `acquisition` |
| `queue_claim_latency` | enqueue → claim | p95 > 60 s |
| `queue_lease_reclaims_total` | crashes / stalls | > 3 per hour |
| `queue_dlq_size` | needs a human | > 0 |
| `queue_retry_rate{kind}` | provider trouble | > 50% for 15 min |
| `sqlite_busy_retries_total` | write contention | sustained growth |

`queue_lease_reclaims_total` is the best single indicator of instability: it
counts the times a worker died holding a job. A quiet system with a rising
reclaim count is failing invisibly.

---

## 9.10 What this queue deliberately does not do

| Feature | Why not |
| ------- | ------- |
| Job chaining / DAG | The acquisition job **is** the workflow, with checkpointed stages. A generic DAG engine would be a second, competing state machine. |
| Cron inside the queue | The Scheduler owns time. Two schedulers is one too many. |
| Fan-out/fan-in primitives | The one real fan-out (multi-destination delivery) is modelled as N `Delivery` aggregates — visible in the domain rather than hidden in queue mechanics. |
| Priority pre-emption | Suspending a running download to start a "more important" one wastes the bytes already fetched. Ageing solves fairness; pre-emption solves nothing here. |
| Distributed locks | One node. Adding them now would be designing for a topology that may never arrive. |
