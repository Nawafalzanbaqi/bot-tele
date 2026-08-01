# 03. Subsystems

Each subsystem below states: **responsibility**, **key abstractions**,
**dependencies**, **extension point**, **failure behaviour**, and **status**.

A subsystem is a unit of *change*: it should be possible to rewrite one without
reading the others.

---

## 3.1 Core

**Responsibility.** The rules that are true regardless of interface, storage or
provider: what an asset is, when it is a duplicate, when bytes may be deleted,
which state transitions are legal, when a failure may be retried.

**Key abstractions.** Aggregates, value objects, domain events, policies,
specifications, repository ports ([06](06-domain-model.md)).

**Dependencies.** The Python standard library. Nothing else, ever.

**Extension point.** None — Core is extended by editing it, deliberately. A
plugin that needs to change a core rule is a design error, not a missing hook.

**Failure behaviour.** Raises typed domain errors. Never logs, never retries,
never has side effects.

**Status.** Partially implemented; requires the custody rework in
[20](20-architecture-decision-review.md).

---

## 3.2 Download

**Responsibility.** Turning a source reference into bytes on local disk:
provider selection, resumption, progress reporting, byte-ceiling enforcement,
cancellation.

**Key abstractions.**

| Abstraction | Kind | Notes |
| ----------- | ---- | ----- |
| `DownloaderPort` | port (application) | `supports(source)`, `probe(source)`, `fetch(request, sink, cancel_token)` |
| `FetchRequest` | DTO | source, workspace handle, byte ceiling, resume token, format selection |
| `FetchOutcome` | DTO | artifact handles, bytes written, container/codec facts, resume token if partial |
| `ProgressSink` | port | throttled, non-blocking; see [16](16-monitoring.md) §16.4 |
| `CancellationToken` | port | cooperative; checked between chunks |

**Dependencies.** Workspace (space), Source Intelligence (which provider),
Configuration (limits). Never the Catalogue, never Delivery.

**Extension point.** `DownloaderPlugin` — yt-dlp, plain HTTP, torrent, local
file import, custom site scrapers ([15](15-plugin-architecture.md)).

**Failure behaviour.** Classifies every failure as `TRANSIENT`, `PERMANENT` or
`POLICY`. The distinction drives retry ([09](09-queue-architecture.md) §9.5) and
is the adapter's responsibility because only the adapter can read a 403 and know
whether it means "geo-blocked forever" or "rate-limited, try in 60 s".

**Critical constraint.** The byte ceiling is enforced **during streaming**, not
after. A `Content-Length` header is a hint from an untrusted party.

**Status.** Designed, not implemented. Explicitly out of scope for Phase 02.

---

## 3.3 Processing

**Responsibility.** Making an artifact satisfy a destination's constraints, and
producing derived artifacts (thumbnails, previews).

**Key abstractions.**

- `ProcessingPlanner` (domain service): `(TechnicalProfile, DeliveryConstraints,
  ProcessingPolicy) → ProcessingPlan`. Pure. Deterministic. Unit-testable with
  zero I/O — this is where the intelligence lives.
- `ProcessingPlan`: an ordered, immutable list of `ProcessingStep`
  (`Remux`, `Transcode`, `Split`, `Thumbnail`, `StripMetadata`), each with an
  estimated cost.
- `MediaProcessorPort` (port): executes one step. Implemented by FFmpeg.

**Dependencies.** Workspace, Configuration. Receives constraints as data; does
not know which destination they came from.

**Extension point.** `MediaProcessorPlugin`.

**Failure behaviour.** A step that fails is retried only if classified
transient (rare — FFmpeg failures are usually deterministic). A plan that cannot
satisfy the constraints fails the job as `POLICY` with a specific code
(`undeliverable_after_processing`), never silently delivers something wrong.

**Critical constraint (Pi).** Transcoding on a Pi 4 is roughly real-time at
720p with hardware acceleration and far slower without. The planner must
**prefer remux over transcode, splitting over transcoding, and rejection over an
8-hour job**. `max_processing_cost` is a first-class admission policy input, not
an afterthought.

**Status.** Designed, not implemented.

---

## 3.4 Telegram

**Responsibility.** Two adapters that share nothing but a client.

1. **Inbound gateway** — receives updates, authenticates the sender against
   Access, translates a message into exactly one application command, replies
   with a formatted result.
2. **Outbound delivery provider** — implements `DeliveryProvider`: declares
   capabilities, uploads artifacts, reuses remote references, returns receipts.

**Dependencies.** Application command/query handlers (inbound); Workspace read
access and the Delivery port contract (outbound).

**Hard rules.** No business logic in handlers. No repository access. No policy
decisions. No domain imports beyond identifiers and DTOs. Enforced by test
([04](04-dependency-graph.md) §4.6).

**Failure behaviour.** Telegram is rate-limited and flaky by design: `429` with
`retry_after` is normal traffic, not an incident. The adapter honours
`retry_after` internally for short waits and surfaces longer ones as
`TRANSIENT` failures with a computed `available_at`.

**Status.** Designed, not implemented. See [12](12-telegram-architecture.md).

---

## 3.5 API

**Responsibility.** The HTTP contract: validation, authentication,
serialisation, pagination, errors, OpenAPI, live progress via SSE.

**Key abstractions.** Routers per resource, Pydantic schemas per version,
dependency providers, problem-details error mapping.

**Dependencies.** Application layer only. Infrastructure only in the composition
root (`app.py`, `lifespan.py`, `dependencies.py`).

**Extension point.** New versioned package (`v2/`) beside `v1/`; never edit a
shipped schema.

**Failure behaviour.** Never leaks internals. Category → status mapping, stable
`code`, correlation id in every problem document.

**Status.** Implemented (Phase 01), needs new resources for deliveries,
destinations, subscriptions and progress streams.

---

## 3.6 Queue

**Responsibility.** Durable, ordered, prioritised, leased hand-off of work from
producers to workers, with backoff, cancellation and a dead-letter path.

**Key abstractions.** `JobQueuePort` (`enqueue`, `claim`, `heartbeat`,
`complete`, `fail`, `release`, `cancel`), lease, `available_at`, priority
weight with ageing, dead-letter record.

**Dependencies.** Persistence only.

**Decision.** The queue **is a table in SQLite**, not Redis, not Celery.
Rationale, mechanics and the exact claim statement: [09](09-queue-architecture.md).
This removes a process, a RAM allocation, a persistence configuration and an
entire failure domain from a device that has none to spare.

**Failure behaviour.** The queue never loses a job: everything is a transactional
state change. A crashed worker loses its lease, not its job.

**Status.** Designed, not implemented. Phase 01's Redis service must be removed.

---

## 3.7 Worker

**Responsibility.** Executing one job at a time: claim → run stages → checkpoint
→ report → release. Nothing else.

**Key abstractions.** `WorkerLoop`, `StageExecutor`, `JobContext` (id,
correlation id, cancellation token, progress sink, workspace lease),
`ShutdownController`.

**Dependencies.** Queue, application use cases, Workspace, ports for the actual
work.

**Failure behaviour.** Crash-safe by construction: the lease expires and another
worker (or the same one after restart) resumes from the last checkpointed stage.
Graceful shutdown drains: stop claiming, finish or checkpoint the current stage,
release the lease, exit. See [10](10-worker-architecture.md).

**Status.** Designed, not implemented.

---

## 3.8 Scheduler

**Responsibility.** Everything time-driven, in one place with one clock:

| Trigger | Period | Effect |
| ------- | ------ | ------ |
| Lease reaper | 30 s | Reclaim jobs whose lease expired |
| Retry promoter | 15 s | Nothing — `available_at` is evaluated at claim time (documented so nobody adds a redundant timer) |
| Expiry sweep | 5 min | Move jobs past their deadline to `EXPIRED` |
| Workspace sweep | 10 min | Delete orphaned/expired workspace leases |
| Disk guard | 1 min | Update headroom; flip admission to refuse when low |
| Subscription tick | configurable | Enqueue due subscription runs |
| Retention sweep | daily | Apply retention policy to history and annotations |
| Backup | daily | SQLite online backup + checkpoint |

**Dependencies.** Queue, Workspace, Automation, application use cases.

**Design rules.** Single owner (exactly one scheduler process — enforced by an
advisory lock row, so two accidental instances cannot double-fire). Every task
is **idempotent** and **catch-up safe**: after the Pi is off for a day, the
scheduler must not fire 1 440 subscription runs. Missed ticks are coalesced.

**Status.** Designed, not implemented.

---

## 3.9 Storage

**Responsibility.** The ephemeral workspace: leasing space, handing out
artifact handles, enforcing containment, guaranteeing deletion, tracking disk
headroom. **Not a library.**

**Key abstractions.** `WorkspaceLease`, `ArtifactHandle` (opaque; resolves to a
path only inside the adapter), `DiskBudget`, `RetentionPolicy`.

**Extension point.** `StorageProviderPlugin` for the *archival* destination
(NAS/S3) — which is a `DeliveryTarget`, not a second library.

**Failure behaviour.** Out of space is a **first-class, expected outcome**:
admission refuses new jobs before the disk is full, not after. Cleanup failure
is retried and always converges — a leaked file is a bug that a sweep must fix
without human help.

**Status.** Implemented (Phase 07): leasing with accounted reservations, atomic
publication after verification, streaming content hashes, containment, ceilings,
disk-full detection, and ownership-based crash recovery over per-lease
manifests. Still outstanding: the lease *table* that would make a workspace root
shared by two processes safe, and the timer that runs the sweep periodically
rather than only at startup ([11](11-storage-strategy.md) §11.6).

---

## 3.10 Metadata (Source Intelligence)

**Responsibility.** Knowing what a source *is* before spending bytes on it:
canonicalisation, provider identification, probing, capability discovery,
caching, provider health.

**Key abstractions.** `SourceCanonicalizer` (versioned!), `SourceProbe`
(title, kind, duration, expected size, formats, resumability, live-ness),
`ProbeCache`, `ProviderHealth`.

**Why it matters more than it looks.** Canonicalisation defines identity, and
identity defines deduplication. If the rules change, previously-distinct assets
may become duplicates. Hence `canonicalization_version` stored per asset and a
documented re-canonicalisation migration path.

**Failure behaviour.** A failed probe fails admission — the job is never
created. Better a clear refusal than a queued job that will fail in an hour.

**Status.** Designed, not implemented.

---

## 3.11 Search

**Responsibility.** Finding things in the catalogue: full text over title,
description, tags, provider and annotations; faceted filtering; saved queries.

**Key abstractions.** `SearchIndexPort`, `SearchQuery`, `SearchResult`
projection, `IndexProjector` (subscribes to catalogue events).

**Decision.** SQLite **FTS5**, in the same database file. No Elasticsearch, no
Meilisearch — both would consume more RAM than the entire application on a Pi.
The index is derived and rebuildable from the Catalogue at any time.

**Failure behaviour.** A stale or missing index degrades search, never writes.
Rebuild is a maintenance command, not an incident.

**Status.** Designed, not implemented.

---

## 3.12 Configuration

**Responsibility.** One typed, validated, immutable settings object per process;
secrets handling; feature flags; profiles.

**Status.** Implemented (Phase 01), extended in
[13](13-configuration-architecture.md).

---

## 3.13 Security

**Responsibility.** Authentication of principals, authorisation of actions,
quotas and rate limits, URL and file policy (SSRF, traversal, size bombs),
secret handling, subprocess confinement, audit logging.

**Key abstractions.** `Principal`, `AuthorizationPolicy`, `Quota`,
`RateLimiter`, `UrlPolicy`, `FilenamePolicy`, `AuditLog`.

**Design rule.** Security is a **layer of policies inside the domain plus
adapters at the edges** — not middleware bolted on. "Is this URL allowed?" is a
business rule with a domain error and an audit entry, not an HTTP concern.

**Status.** Partially designed in Phase 01 (config hardening, non-root
container); the substantive controls are specified in
[14](14-security-architecture.md) and unimplemented.

---

## 3.14 Monitoring

**Responsibility.** Health probes, metrics, structured logs, optional traces,
alert conditions.

**Design rule (Pi).** Observability must cost less than the work observed.
Pull-based Prometheus metrics, no per-chunk writes, tracing off by default,
log rotation mandatory (an unrotated log fills an SD card in weeks).

**Status.** Partially implemented (logging, health); see
[16](16-monitoring.md).

---

## 3.15 Automation

**Responsibility.** Subscriptions, watches, recurring acquisition, rules
("when a new item appears on X, acquire it and send it to Y").

**Key abstractions.** `Subscription` (source + `Schedule` + filters +
destinations), `SubscriptionRun`, `SeenMarker`, `AutomationRule`.

**Hard rules.** No privileged path (§2.2). Bounded fan-out per run
(`max_items_per_run`) so one popular channel cannot enqueue 500 jobs overnight.
Catch-up safe.

**Status.** Designed, not implemented.

---

## 3.16 Plugins

**Responsibility.** A stable, versioned contract that lets capabilities be added
without touching the Core: downloaders, processors, delivery providers, storage
providers, AI providers, notification providers.

**Status.** Contract designed, discovery designed, **execution deliberately
in-process and trusted for v1** — a sandbox is a large subsystem and there is no
third-party plugin ecosystem to protect against yet. See
[15](15-plugin-architecture.md) for the full reasoning and the migration path.

---

## 3.17 AI / Enrichment

**Responsibility.** Optional annotations: tags, summaries, transcripts,
translations, scene detection, NSFW/safety classification.

**Design rules.**

- **Never on the critical path.** Enrichment runs as a separate, low-priority
  job class after delivery. Its failure is invisible to the user.
- **Provider-agnostic.** `AiProviderPort` with local (whisper.cpp, ollama) and
  remote (API) implementations. On a Pi, "local" means *tiny models or nothing*.
- **Privacy is a policy, not a default.** Sending media or metadata to a remote
  AI provider is opt-in per provider, per data class, and audited. This is
  someone's personal media library.

**Status.** Designed, not implemented, not scheduled.

---

## 3.18 Future extensions

Named here so the architecture reserves space for them without building them.

| Extension | What it needs from today's architecture | Already provided? |
| --------- | --------------------------------------- | ----------------- |
| Web UI | Read models, SSE progress, session auth | Yes — `presentation/web/` slot, SSE designed |
| Desktop / Mobile app | Stable versioned REST API, tokens | Yes — API v1 + Access |
| CLI | Direct use-case invocation, no HTTP | Yes — `presentation/cli/` slot |
| Multi-node | Postgres, object storage, broker | Path documented ([18](18-deployment-architecture.md) §18.6), not built |
| NAS / S3 archive | A second `DeliveryProvider` with `can_serve_back=true` | Yes — Delivery is provider-agnostic |
| Sharing / public links | New context; must not weaken Access | Space reserved, nothing assumed |
| Multi-user households | Access already models principals & quotas | Partially |
| Transcode profiles per destination | `DeliveryConstraints` already an input to the planner | Yes |

The test for "did we design for the future correctly" is not whether these exist
— it is whether adding one requires editing `domain/`. For every row above, the
answer is no.
