# 20. Architecture Decision Review

A critical review of every significant decision made in the Foundation phase,
now that the product requirements are known: **SQLite**, **Raspberry Pi**,
**Telegram as one interface**, and **local media deleted after delivery**.

Verdicts are honest. Several Foundation decisions were correct and are
reinforced. Several were wrong, and two of them are *silent* failures — code
that will run and produce incorrect behaviour without raising anything.

| Verdict | Count |
| ------- | ----- |
| ✅ Keep — correct, reinforced | 9 |
| 🔧 Revise — right idea, wrong details | 8 |
| ❌ Replace — wrong for this product | 5 |
| ⚠️ **Silent defect** — will not raise, will misbehave | 4 |

---

## Part A — Decisions that were right

### 4.1 ✅ Clean Architecture with enforced layering ([ADR-0002](../adr/0002-clean-architecture-layering.md))

**Verdict: keep, and extend with a context axis.**

This decision is what makes Phase 02 possible at all. Because the domain has no
framework imports, replacing PostgreSQL with SQLite touches one package instead
of the codebase. Because rules live in the domain, `UrlPolicy` can be a pure
function tested without a network stack.

The executable enforcement is the part that matters. Conventions decay; a test
that names the offending file does not.

**Extension required:** the context axis ([04](04-dependency-graph.md) §4.2).
With two contexts, layer enforcement was sufficient. With ten, nothing today
prevents `domain/acquisition` from importing `domain/catalogue` internals and
fusing two models.

### 4.2 ✅ Explicit ORM mapping ([ADR-0003](../adr/0003-explicit-orm-mapping.md))

**Verdict: keep — and SQLite makes it more valuable, not less.**

The mapper layer is where SQLite's type quirks (§4.4) get corrected once,
centrally, instead of leaking into aggregates as nullable columns and naive
datetimes. Had the ORM been mapped directly onto aggregates, the timezone defect
below would be unfixable without changing the domain.

### 4.3 ✅ Dual persistence adapters ([ADR-0005](../adr/0005-dual-persistence-adapters.md))

**Verdict: keep, with a new obligation.**

A 300-test suite running in under two seconds with no database is worth
defending. But the risk named in that ADR — the two implementations drifting —
is now larger, because SQLite has genuinely different semantics (single writer,
no `SKIP LOCKED`, no native tz).

**Obligation added:** the shared **contract test suite**
([17](17-testing-strategy.md) §17.4). Every port gets one suite, run against
both the in-memory and the SQLite adapter. Without it, the in-memory adapter is
a comfortable fiction.

### 4.4 ✅ Domain purity (no third-party imports)

**Verdict: keep, absolutely.**

This is what allows the security controls to be domain policies
([14](14-security-architecture.md)) rather than middleware, and therefore to
apply identically to HTTP, Telegram, the CLI and automation. A control that
exists in only one interface is not a control.

### 4.5 ✅ Production hardening in configuration validation

**Verdict: keep, extend.**

Refusing to boot production with a placeholder secret converts a class of
incident into a failed deploy. Extended in
[13](13-configuration-architecture.md) with role-scoped validation and
cross-field coherence checks.

### 4.6 ✅ Other decisions retained without change

| Decision | Why it holds |
| -------- | ------------ |
| RFC 9457 problem details with a stable `code` | Three interfaces translate one taxonomy ([05](05-component-communication.md) §5.8) |
| API versioning (`/api/v1`, additive-only) | Unchanged |
| Application factory over a global `app` | Enables per-test configuration and the multi-role process model |
| `Page`/`PageRequest` in the domain | Framework-free contract, bounded by construction |
| Ports as `Protocol` (structural typing) | Adapters need not inherit from a domain symbol |
| Non-root, multi-stage container | Correct; tightened in [14](14-security-architecture.md) §14.11 |
| ADR practice itself | Directly enabled this review |

---

## Part B — Silent defects (highest priority)

These do not raise. They run, and they are wrong. Each is a direct consequence
of writing the Foundation against PostgreSQL.

### 4.7 ⚠️ `DateTime(timezone=True)` loses timezone on SQLite

**What happens.** SQLite has no native timezone-aware type. SQLAlchemy stores
the value and returns a **naive** `datetime`. The mapper then calls the
aggregate constructor, which calls `ensure_utc(...)`, which raises
`InvariantViolationError` — *on every single row load*.

**Why it is worse than a crash.** It surfaces at load time, in the adapter, far
from the cause, and only once real data exists. Locally it looks like "SQLite is
broken".

**Fix.** A `UtcDateTime` `TypeDecorator` in `infrastructure/persistence/sqlite/types.py`
that stores ISO-8601 UTC text and re-attaches `UTC` on load. Applied to every
timestamp column. The domain rule (`ensure_utc`) stays untouched — it is
correct; the adapter was lying to it.

### 4.8 ⚠️ Foreign keys are not enforced

**What happens.** SQLite ignores foreign key constraints unless
`PRAGMA foreign_keys=ON` is issued **per connection**. The Foundation schema
declares `ondelete="CASCADE"` on `download_jobs.media_id`. On SQLite that clause
is inert: deleting an asset silently orphans its jobs.

**Fix.** A connection-event listener issuing the pragma on every connect, plus a
startup assertion that it is on. This is one of the most commonly missed SQLite
facts and produces referential corruption that no test written against
PostgreSQL will catch.

### 4.9 ⚠️ The partial unique index silently disappears

**What happens.** The "at most one active job per media item" invariant is
implemented as
`Index(..., unique=True, postgresql_where=text("status IN ('queued','running')"))`.
The `postgresql_` prefix means the dialect **drops it on SQLite** — no index, no
error. The application-level check in the use case then loses races, which is
precisely the scenario the index existed to cover.

**Fix.** `sqlite_where=` (both in the model and in the migration). And a
contract test that asserts the constraint actually rejects a second active job
against the real SQLite adapter — testing the invariant, not the declaration.

### 4.10 ⚠️ `SELECT … FOR UPDATE SKIP LOCKED` does not exist in SQLite

**What happens.** `claim_next_queued` uses `with_for_update(skip_locked=True)`.
SQLite does not support row locking; the statement fails or the locking clause is
meaningless. Worse, the *port's docstring* specifies the PostgreSQL mechanism —
a storage detail written into a domain contract.

**Fix.** Two changes: the port promises **"atomically claims and returns"**
without naming a mechanism; the SQLite adapter implements it as a single
`UPDATE … WHERE id IN (SELECT … LIMIT n) RETURNING *`
([09](09-queue-architecture.md) §9.4). SQLite's global write lock makes this
*simpler* than the PostgreSQL version, not a workaround.

---

## Part C — Decisions to replace

### 4.11 ❌ PostgreSQL → SQLite ([ADR-0006](../adr/0006-sqlite-system-of-record.md))

Beyond §§4.7–4.10, the full change list:

| Item | Foundation | Required |
| ---- | ---------- | -------- |
| Driver | `asyncpg` | `aiosqlite` |
| Engine config | `pool_size`, `max_overflow`, `pool_timeout` | Meaningless for a serialised writer; `StaticPool` for `:memory:`; `busy_timeout` instead |
| Pragmas | none | `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout` |
| Alembic | default mode | **`render_as_batch=True`** — SQLite's `ALTER TABLE` cannot drop or alter columns; without batch mode, the second migration that changes a column fails |
| Search | `ILIKE` | Works via `lower() LIKE lower()`, but unindexed; use **FTS5** ([03](03-subsystems.md) §3.11) |
| Concurrency | connection pool | One writer; short transactions; retry on `SQLITE_BUSY` |
| Backup | `pg_dump` | Online backup API — **never `cp` a live WAL database** |

**Was SQLite the right call?** For this product, yes, and by a wide margin:
zero-administration, one file to back up, no second process on a 2 GB device,
transactional enqueue for free. Its ceiling (single writer, single node) sits far
above the design point of a few jobs per minute. The cost is real — the four
silent defects above — and it is paid once, in one package.

### 4.12 ❌ Redis in the Compose stack

Provisioned "reserved for the future download queue". Phase 02 chose a
SQLite-backed queue ([ADR-0008](../adr/0008-sqlite-job-queue.md)), so Redis is
now 50–200 MB of RAM, an extra failure domain, an extra persistence
configuration and extra SD-card writes, in exchange for nothing.

**Lesson worth recording:** the Foundation added infrastructure for an
anticipated need. Reserving a *port* costs nothing; reserving a *service* costs
memory, attack surface and operational burden on every deployment until the day
it is used.

### 4.13 ❌ Permanent local storage model

The Foundation models a media **library**:

| Symbol | Problem |
| ------ | ------- |
| `MediaItem.storage_key` | Implies a permanent local location; after delivery there is none |
| `MediaStatus.AVAILABLE` | Means "bytes are local" — the steady state is bytes *not* local |
| `StorageSettings.library_path` | There is no library. Naming one legitimises permanent files |
| `MediaItem.mark_available(storage_key, size, checksum)` | Records the wrong fact at the wrong moment |

**Replacement:** the custody model ([06](06-domain-model.md) §6.3):
`CustodyState`, `RemoteArtifactRef`, and `release_local_copy()`. Local paths
become job-scoped `ArtifactHandle`s owned by the Workspace context; `library_path`
becomes `workspace.root`.

This is the single largest domain change in Phase 03, and it must happen before
more code is written against `storage_key`.

### 4.14 ❌ Single-process deployment

Everything runs in the API process. One `ffmpeg` call would block the event loop
and freeze every request; a downloader memory leak would take the API with it.

**Replacement:** four roles from one image
([01](01-product-architecture.md) §1.3, [ADR-0010](../adr/0010-separate-worker-process.md)).

### 4.15 ❌ `DownloaderPort` shape

```
async def fetch(request, *, on_progress=None) -> DownloadOutcome
```

Wrong in five ways:

1. **No probe/fetch split.** Admission needs metadata *before* creating a job.
   Without it, a 40 GB file is discovered after it lands on a 32 GB card.
2. **No cancellation.** `on_progress` cannot stop anything.
3. **No resume.** A 90%-complete download restarts from zero after a power cut —
   unacceptable on domestic broadband.
4. **`staging_key: str`** is a path-shaped string, defeating workspace
   containment ([14](14-security-architecture.md) §14.4). It must be an opaque
   handle.
5. **No error classification contract.** Retry logic depends on
   `TRANSIENT`/`PERMANENT`, and only the adapter can classify.

Corrected in [03](03-subsystems.md) §3.2 and [15](15-plugin-architecture.md) §15.2.

The `NullDownloader` decision itself ([ADR-0004](../adr/0004-downloader-behind-a-port.md))
was **right** — the seam being real, typed and tested is exactly why this
critique is a signature change rather than a redesign.

---

## Part D — Decisions to revise

### 4.16 🔧 Context naming: `media` → `catalogue`, `download` → `acquisition`

`MediaItem` is fine as an aggregate name but `domain/media` is too vague a
context once Processing, Delivery and Sources exist. `DownloadJob` is actively
misleading: the job spans download, processing, delivery and cleanup — it is an
*acquisition*.

Rename in Phase 03, while it is a mechanical change across ~15 files.

**The HTTP resource stays `/api/v1/downloads`** — deliberate boundary
translation. "Download" is the user's word; "acquisition" is the domain's. The
presentation layer translating vocabulary is legitimate; the divergence is
documented so it reads as a decision rather than an inconsistency.

### 4.17 🔧 Missing concurrency and integrity controls

| Missing | Consequence | Fix |
| ------- | ----------- | --- |
| `version` column | API, worker and scheduler all write a job; last write wins silently | Optimistic concurrency with `WHERE version = :expected` |
| CHECK constraint on lease/status | A leased job in a non-running state is invisible corruption | `CHECK ((lease_owner IS NULL) = (status NOT IN (...)))` |
| `correlation_id` on the job row | A user's request cannot be traced into the worker | Column + re-bind on claim ([16](16-monitoring.md) §16.3) |
| `idempotency_key` | Telegram redelivery creates duplicate jobs | Column + unique index |
| `retry_of` | No lineage between a dead job and its replacement | Self-referencing column |

### 4.18 🔧 `MediaType` defaulting to `OTHER`

`RegisterMediaRequest.media_type` defaults to `OTHER`, so a client that omits it
silently mis-files an item, and the probe's better answer never overrides it.

**Fix:** the kind is **derived from the probe**, not supplied by the client;
`OTHER` becomes what it should be — an explicit "we could not classify this"
signal, not a default. Similarly, `title` should be optional on submission and
default to the probed title. Today the API forces a user to name something the
system is about to look up.

### 4.19 🔧 Event publishing without an outbox

`LoggingEventPublisher` publishes after commit, in-process. That is adequate for
metrics and logging, and **insufficient** the moment an event triggers work:
a power cut between commit and publish loses the event permanently. On a Pi that
is a weekly occurrence.

**Fix:** transactional outbox for events that trigger work or update another
context ([05](05-component-communication.md) §5.5). `delivery.succeeded.v1` is
the critical one — it authorises deletion of the bytes.

### 4.20 🔧 Configuration gaps

| Gap | Fix |
| --- | --- |
| `api.host` defaults to `0.0.0.0` | Default `127.0.0.1`. A self-hosted product should not be exposed to the LAN by accident |
| No secret redaction in logging | **Mandatory** redacting sink — the bot token appears inside Telegram API URLs logged by third-party libraries ([14](14-security-architecture.md) §14.8) |
| `settings.py` is one file | Split into `sections/`; a 15-section file is a merge-conflict magnet |
| No role-scoped validation | A missing bot token must not stop the API from booting |
| No `_FILE` secret convention | Env vars are readable in `/proc/<pid>/environ` |
| `STORAGE__LIBRARY_PATH` | Rename to `WORKSPACE__ROOT` (§4.13) |

### 4.21 🔧 UUID4 → UUIDv7

Random v4 keys scatter B-tree inserts. UUIDv7 is time-ordered, keeps inserts
sequential, and makes range queries by creation time efficient — which matters
on an SD card, where random writes are the slow path. The `Uuid4Generator`
docstring already anticipates this; the port needs no change.

### 4.22 🔧 Health checks and diagnostics

`check_database` is correct but shallow. Add `/health/deep`
([16](16-monitoring.md) §16.2) covering disk headroom, queue depth, worker
heartbeats, DLQ size, provider circuits and last backup — the questions an
operator actually asks. Workers and the scheduler need heartbeat rows, since
they have no HTTP surface.

### 4.23 🔧 Architecture tests

Currently four assertions. Five more are required
([04](04-dependency-graph.md) §4.6), of which **no foreign vocabulary** is the
most valuable: it is the mechanical enforcement of "Telegram is not the
product". Without it, `chat_id` reaches the domain within a quarter, and every
claim in [12](12-telegram-architecture.md) becomes aspirational.

---

## Part E — Alternatives considered for the overall design

Being critical includes re-examining the shape of the whole thing, not just its
details.

### Event sourcing for the acquisition job

**Attractive.** The job is a sequence of state changes; audit and replay are
genuinely useful; "what happened to job X?" becomes trivial.

**Rejected.** SQLite is a mediocre event store; projections add a second
consistency model; schema evolution of events is harder than of tables; and the
operational burden lands on a self-hosting user who did not sign up for it.

**Mitigation:** the *observable benefit* is captured without the machinery —
`job_events` records every transition as an append-only audit trail. If replay
is ever needed, the outbox and event catalogue are already in place.

### Saga / process manager across contexts

**This is what `AcquisitionJob` actually is**, and the specification names it as
such ([06](06-domain-model.md) §6.4). The alternative — a general saga engine
with compensations — was rejected: there is one workflow, and a generic engine
would be a second state machine competing with the domain's.

**Compensations are handled explicitly** where they matter: cleanup on failure,
custody rollback, orphan sweeps. These are named operations, not a framework.

### Actor model / per-job supervisors

Rejected: a runtime and a mental model for a workload of ≤2 concurrent jobs.
Lease + checkpoint gives the same crash-recovery property with a table.

### Context-first folder layout

Considered seriously, rejected with reasons in [19](19-folder-structure.md) §19.1.

### Telegram MTProto (user account) instead of the Bot API

Would raise the upload ceiling to 2 GB without a local API server. Rejected:
it operates a user account programmatically (ToS risk), needs phone-number
credentials on the device, and a session file that is a far more dangerous
secret than a bot token. The local Bot API server achieves the same ceiling
supportedly ([12](12-telegram-architecture.md) §12.4).

---

## Part F — Phase 03 remediation backlog

In dependency order. Items 1–4 are blocking: they are silent defects or
foundations that everything else builds on.

| # | Task | Why now |
| - | ---- | ------- |
| 1 | SQLite migration: driver, pragmas, `UtcDateTime`, FK enforcement, `sqlite_where`, batch-mode Alembic | Four silent defects (§§4.7–4.10) |
| 2 | Custody model: replace `storage_key`/`AVAILABLE` with `CustodyState` + `RemoteArtifactRef` | Every later feature builds on the wrong model otherwise |
| 3 | Rename contexts (`media`→`catalogue`, `download`→`acquisition`); add the context axis to the architecture tests | Mechanical now, expensive later |
| 4 | Job columns: `version`, `correlation_id`, `idempotency_key`, `lease_*`, `stage`, `expires_at`, `cancel_requested`, `retry_of`; CHECK constraint | Schema churn is cheapest before there is data |
| 5 | Queue: table, atomic claim, lease reaper, backoff, DLQ | The core of the async system |
| 6 | Worker + scheduler process roles; remove Redis; Compose topology | Unblocks all real work |
| 7 | Workspace context: leases, handles, containment, sweeper | Required before any download exists |
| 8 | Delivery context + `DeliveryProvider` port + in-memory provider | The custody rule needs a receipt to react to |
| 9 | Transactional outbox | `delivery.succeeded` must not be lossy |
| 10 | Secret redaction sink; `api.host` default; `_FILE` secrets; config sections | Cheap, and a token leak is unrecoverable |
| 11 | Contract test suites for every port | Keeps the in-memory adapters honest |
| 12 | `UrlPolicy` + `FilenamePolicy` + security test corpora | Before the first real fetch, not after |
| 13 | Deep health, metrics, heartbeats | Before the system runs unattended |
| 14 | UUIDv7; `/health/deep`; split settings | Cleanups |

**Only after 1–13 should any downloader, FFmpeg or Telegram code be written.**
Building acquisition on an unfixed foundation would mean paying for every defect
above twice — once in the foundation, once in everything layered on it.

---

## Part G — What this review says about the process

Two observations worth keeping, because they generalise:

1. **The Foundation's discipline is what made this review cheap.** Every defect
   above is confined to one package, and none required changing a business rule.
   That is the return on the layering investment — not portability in theory,
   but a scoped, reviewable change list in practice.

2. **The Foundation's mistakes were all of one kind: building for an assumed
   environment.** PostgreSQL, Redis, a permanent library, a single process — each
   was a reasonable default for a generic server application, and each was wrong
   for *this* product on *this* hardware. The lesson is not "assume less"; it is
   that **infrastructure choices must be deferred until the deployment target is
   known, while interface choices should be made immediately** — because ports
   are cheap to define and expensive to retrofit, and services are the opposite.
