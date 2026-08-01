# 06. Domain Model

Notation below is a **specification sketch**, not code: attribute tables and
signature declarations with no bodies. Types are named so that Phase 03 can
implement them mechanically.

---

## 6.1 Modelling rules

1. **Value objects validate on construction.** If you hold one, it is valid.
   No defensive checks downstream.
2. **Aggregates are state machines.** Every mutation is a named intention that
   validates a transition, stamps time, and records an event.
3. **Time is an argument.** `now: datetime` is passed in; the domain never reads
   a clock.
4. **Identity is generated outside.** Ids arrive via a port, so aggregates are
   deterministic under test.
5. **One aggregate per transaction.** Cross-aggregate consistency is eventual,
   via events or a process manager. Two aggregates in one transaction is a
   modelling error 90% of the time — and on SQLite it is also a lock-contention
   error.
6. **Aggregates are small.** An aggregate is a consistency boundary, not an
   object graph. If a field is never used to *enforce a rule*, it belongs in a
   read model.

---

## 6.2 Shared kernel — `domain/common`

Frozen. Additions require an ADR.

| Type | Kind | Purpose |
| ---- | ---- | ------- |
| `Entity[TId]` | base | Identity equality |
| `AggregateRoot[TId]` | base | Entity + event buffer (`record_event`, `pull_events`) |
| `ValueObject` | base | Frozen, slotted, self-validating marker |
| `DomainEvent` | base | `occurred_at`, `event_id`, `name` |
| `DomainError` hierarchy | errors | `InvariantViolation`, `EntityNotFound`, `Conflict`, `InvalidStateTransition` — each with a stable `code` |
| `Page[T]`, `PageRequest` | VO | Bounded windows for repository ports |
| `ensure_utc(dt)` | function | Rejects naive datetimes |
| `Fingerprint` | VO | `(algorithm, digest)` — used by two contexts, hence shared |

`Fingerprint` is the only new shared-kernel member proposed by Phase 02, and it
is justified: Catalogue computes identity from it, Workspace verifies integrity
with it, and duplicating it would allow the two to disagree about what "sha256"
means.

---

## 6.3 Catalogue context

### Aggregate: `MediaAsset`

The permanent record. **Survives deletion of the bytes** — that is its purpose.

| Attribute | Type | Notes |
| --------- | ---- | ----- |
| `id` | `AssetId` | UUIDv7 |
| `source` | `SourceRef` | canonical URL + provider + provider item id |
| `title` | `AssetTitle` | normalised, ≤500 chars |
| `kind` | `AssetKind` | `VIDEO/AUDIO/IMAGE/DOCUMENT/ARCHIVE/OTHER` |
| `profile` | `TechnicalProfile \| None` | known after probe/download |
| `fingerprint` | `Fingerprint \| None` | known after download; `None` for never-acquired |
| `custody` | `CustodyState` | **the central concept** |
| `remote_refs` | `tuple[RemoteArtifactRef, ...]` | where the bytes actually live now |
| `annotations` | `tuple[AnnotationRef, ...]` | AI/user metadata, additive |
| `first_seen_at` / `updated_at` | `datetime` | UTC |
| `canonicalization_version` | `int` | which URL-normalisation rules produced `source` |

**Custody — the model that replaces Phase 01's `storage_key` + `AVAILABLE`.**

```
CustodyState = NONE | LOCAL_ONLY | LOCAL_AND_REMOTE | REMOTE_ONLY | LOST
```

| State | Meaning | Bytes on device? | Recoverable? |
| ----- | ------- | ---------------- | ------------ |
| `NONE` | Catalogued, never acquired | no | by acquiring |
| `LOCAL_ONLY` | Downloaded, not yet delivered | yes | yes |
| `LOCAL_AND_REMOTE` | Delivered, cleanup pending | yes | yes |
| `REMOTE_ONLY` | **Steady state.** Bytes only at a destination | no | via remote ref |
| `LOST` | Every remote ref invalid, no local copy | no | only by re-acquiring from source |

**Invariants.**

- `REMOTE_ONLY` requires ≥1 `RemoteArtifactRef` marked *verified*.
- Local bytes may be deleted **only** in `LOCAL_AND_REMOTE`, and the transition
  to `REMOTE_ONLY` is what authorises deletion.
- `LOST` is reachable only from `REMOTE_ONLY` and only by explicit evidence
  (a provider reported the reference invalid). It is never inferred from a
  timeout.
- `fingerprint` is immutable once set. A different fingerprint means a different
  asset.

**Behaviour.**

```
MediaAsset.catalogue(id, source, title, kind, now) -> MediaAsset
  .describe(profile, now)
  .fingerprint_as(fingerprint, now)          # once only
  .record_local_copy(now)                    # NONE|REMOTE_ONLY -> LOCAL_*
  .record_remote_copy(ref, now)              # -> LOCAL_AND_REMOTE
  .release_local_copy(now)                   # LOCAL_AND_REMOTE -> REMOTE_ONLY
  .invalidate_remote(ref_id, reason, now)    # may reach LOST
  .annotate(annotation_ref, now)
  .forget(now)                               # user-initiated erasure
```

**Events.** `asset.registered`, `asset.described`, `asset.fingerprinted`,
`asset.custody_changed`, `asset.remote_invalidated`, `asset.forgotten`.

### Value objects

| VO | Fields | Validation |
| -- | ------ | ---------- |
| `AssetId` | `UUID` | — |
| `SourceRef` | `canonical_url`, `provider_id`, `provider_item_id?` | scheme ∈ {http,https}; host present; no credentials in URL; ≤2048 chars |
| `AssetTitle` | `str` | non-empty, whitespace-collapsed, ≤500 |
| `TechnicalProfile` | `duration_ms?`, `width?`, `height?`, `container?`, `video_codec?`, `audio_codec?`, `bitrate?`, `size_bytes?` | all non-negative; consistent (video codec ⇒ dimensions) |
| `Fingerprint` | `algorithm`, `digest` | hex, length matches algorithm |
| `RemoteArtifactRef` | `provider`, `principal`, `remote_id`, `remote_unique_id?`, `message_ref?`, `verified_at`, `expires_at?` | provider non-empty; `remote_id` opaque |
| `AnnotationRef` | `kind`, `producer`, `version`, `created_at` | — |

`RemoteArtifactRef.principal` exists because of a hard-won external constraint:
**a Telegram `file_id` is only usable by the bot that created it**. Modelling
the ref without the owning principal produces a system that silently breaks
when the token is rotated. The generic name keeps Telegram's quirk out of the
domain while preserving the fact ([12](12-telegram-architecture.md) §12.5).

### Domain services

| Service | Signature | Why a service |
| ------- | --------- | ------------- |
| `DuplicateResolver` | `(SourceRef, Fingerprint?) -> DuplicateVerdict` | Spans candidate assets; belongs to no single one |
| `CustodyPolicy` | `(MediaAsset, RetentionSettings) -> CustodyDecision` | Encodes "may we delete the bytes?" in one place |

`CustodyDecision ∈ {KEEP_LOCAL, RELEASE_LOCAL, RETAIN_FOREVER}`.
`RETAIN_FOREVER` supports a per-asset `retain_local` override — the escape hatch
for the "Telegram is the only custodian" risk ([01](01-product-architecture.md) §1.9).

### Specifications

`IsDuplicateOf(source, fingerprint)` · `IsEligibleForLocalRelease()` ·
`HasUsableRemoteRef(provider, principal)` · `IsOrphaned()` (no refs, no local)

### Repository port

```
CatalogueRepository:
  add(asset) · save(asset) · get(asset_id) -> MediaAsset?
  find_by_source(source_ref) -> MediaAsset?
  find_by_fingerprint(fingerprint) -> MediaAsset?
  find_many(filters, page) -> Page[MediaAsset]
  find_eligible_for_release(limit) -> Sequence[AssetId]
```

---

## 6.4 Acquisition context

### Aggregate: `AcquisitionJob`

The **process manager** for one acquisition, from admission to terminal state.
It sequences stages and owns retry/lease/expiry rules; it delegates the rules
*inside* each stage to the owning context.

| Attribute | Type | Notes |
| --------- | ---- | ----- |
| `id` | `JobId` | UUIDv7 |
| `asset_id` | `AssetId` | Catalogue entry exists before the job |
| `requested_by` | `PrincipalId` | for quota, audit, notification |
| `destinations` | `tuple[DeliveryTargetId, ...]` | ≥1; where it goes when done |
| `status` | `JobStatus` | [08](08-state-machine.md) |
| `stage` | `JobStage` | last **completed** stage — the resume point |
| `priority` | `JobPriority` | `LOW/NORMAL/HIGH` + ageing |
| `attempts` | `int` | consumed attempts |
| `retry_policy` | `RetryPolicy` | frozen at submission |
| `lease` | `Lease \| None` | ownership while running |
| `progress` | `StageProgress` | coarse, checkpointed |
| `plan` | `ProcessingPlanRef \| None` | decided after probe |
| `artifacts` | `tuple[ArtifactHandle, ...]` | workspace handles |
| `failure` | `FailureReport \| None` | last failure |
| `available_at` | `datetime` | backoff / scheduling gate |
| `expires_at` | `datetime` | admission deadline |
| `cancel_requested` | `bool` | cooperative cancellation |
| timestamps | | `submitted_at`, `started_at?`, `finished_at?`, `updated_at` |

**Behaviour.**

```
AcquisitionJob.submit(id, asset_id, principal, destinations,
                      priority, retry_policy, expires_at, now)
  .claim(lease, now)                # QUEUED -> RUNNING(stage=next)
  .heartbeat(now, extend_to)
  .checkpoint(stage, progress, artifacts, now)
  .advance_to(stage, now)
  .request_cancel(now)              # sets flag; does not stop anything
  .acknowledge_cancel(now)          # worker confirms -> CANCELLED
  .fail(report, now)                # -> QUEUED(backoff) | FAILED | DEAD_LETTER
  .complete(now)                    # all stages done -> COMPLETED
  .expire(now)                      # past deadline -> EXPIRED
  .release_lease(now)               # worker shutting down -> QUEUED
```

**Invariants.**

- A lease exists **iff** status is `RUNNING`.
- `attempts ≤ retry_policy.max_attempts`; reaching the cap forbids `QUEUED`.
- Terminal statuses (`COMPLETED`, `CANCELLED`, `FAILED`, `EXPIRED`) accept no
  further transitions — the only exception is an explicit operator `requeue`,
  which creates a **new** job rather than reviving a dead one. Reviving terminal
  aggregates destroys the audit trail.
- `stage` only moves forward. Re-running a stage after a crash is allowed;
  regressing the recorded stage is not.

### Value objects

| VO | Fields | Notes |
| -- | ------ | ----- |
| `JobId` | `UUID` | |
| `Lease` | `owner_id`, `acquired_at`, `expires_at` | `is_expired(now)` |
| `RetryPolicy` | `max_attempts`, `base_backoff_s`, `max_backoff_s`, `jitter_ratio` | `delay_for(attempt, kind)` |
| `StageProgress` | `stage`, `done_bytes?`, `total_bytes?`, `percent?`, `updated_at` | coarse by design |
| `FailureReport` | `kind`, `code`, `message`, `stage`, `provider?`, `occurred_at`, `retry_after?` | `kind ∈ TRANSIENT/PERMANENT/POLICY/CANCELLED` |
| `JobPriority` | enum + `weight` | ageing applied at claim time |
| `ArtifactHandle` | `lease_id`, `artifact_id`, `role`, `size_bytes?` | opaque; resolves to a path only inside Workspace |

`FailureReport.kind` is the single most important field in the context: it is
what decides retry vs dead-letter, and classification is the **adapter's**
responsibility because only the adapter can interpret a provider's error
([03](03-subsystems.md) §3.2).

### Policies

| Policy | Decides | Inputs |
| ------ | ------- | ------ |
| `AdmissionPolicy` | may this request become a job? | probe (size, duration, kind), principal quota, disk headroom, provider health, allow/deny lists |
| `RetryPolicy` | retry, and when? | failure kind, attempt count, provider `retry_after` |
| `ExpiryPolicy` | when does a queued job stop being worth doing? | submitted_at, priority, source liveness |
| `PriorityPolicy` | effective ordering | base priority, age (anti-starvation), principal fairness |
| `CleanupPolicy` | when may artifacts go? | job status, custody state, retention settings |

Policies are **first-class objects, not `if` statements scattered in use
cases**. Each is independently testable, independently configurable, and
appears exactly once in the codebase.

### Factory

`AcquisitionJobFactory.from_request(request, probe, admission_verdict, policies,
ids, now) -> AcquisitionJob` — the only sanctioned way to create a job. It
guarantees a job never exists without a passed admission check.

### Specifications

`IsClaimable(now)` · `IsRetryable()` · `IsExpired(now)` · `IsStalled(now)` ·
`RequiresProcessing()` · `IsCancellable()`

### Repository port

```
AcquisitionRepository:
  add(job) · save(job) · get(job_id) -> AcquisitionJob?
  find_active_for_asset(asset_id) -> AcquisitionJob?
  find_many(filters, page) -> Page[AcquisitionJob]
  claim_next(owner, lease_duration, classes, limit, now) -> Sequence[AcquisitionJob]
  find_expired_leases(now, limit) -> Sequence[JobId]
```

`claim_next` is specified as **"atomically claims and returns"** — the mechanism
is the adapter's business. Phase 01 wrote `SELECT … FOR UPDATE SKIP LOCKED`
into the port's contract; that is a PostgreSQL detail leaking into the domain
and it does not exist in SQLite ([20](20-architecture-decision-review.md) §4.4).

---

## 6.5 Delivery context

### Aggregate: `Delivery`

One asset going to one destination, once. Its own aggregate — **not** a stage of
the job — because re-delivery must be possible without an acquisition, and each
destination must fail and retry independently.

| Attribute | Type |
| --------- | ---- |
| `id` | `DeliveryId` |
| `asset_id` | `AssetId` |
| `target_id` | `DeliveryTargetId` |
| `source_mode` | `FROM_ARTIFACT \| FROM_REMOTE_REF` |
| `status` | `DeliveryStatus` ([08](08-state-machine.md) §8.5) |
| `attempts`, `retry_policy`, `lease`, `available_at` | as per job |
| `receipt` | `DeliveryReceipt \| None` |
| `failure` | `FailureReport \| None` |

**Behaviour.** `order → claim → transferring → delivered(receipt) | failed |
cancelled`

**Invariant.** `DELIVERED` requires a `DeliveryReceipt`, and a receipt requires
a `RemoteArtifactRef` — no receipt, no custody transfer, no local deletion.

### Entity: `DeliveryTarget`

| Attribute | Type | Notes |
| --------- | ---- | ----- |
| `id` | `DeliveryTargetId` | |
| `provider` | `str` | `telegram`, `s3`, `nas`, `webhook` |
| `address` | `TargetAddress` | opaque to the domain (VO wrapping provider-specific data) |
| `owner` | `PrincipalId` | |
| `capabilities` | `DeliveryCapabilities` | refreshed from the provider |
| `enabled` | `bool` | |

### Value objects

| VO | Fields |
| -- | ------ |
| `DeliveryCapabilities` | `max_bytes`, `allowed_containers`, `max_duration_s?`, `supports_ref_reuse`, `can_serve_back`, `supports_streaming`, `max_caption_len?` |
| `DeliveryReceipt` | `provider`, `remote_ref`, `message_ref?`, `bytes_sent`, `delivered_at` |
| `RemoteMessageRef` | `provider`, `container_id`, `message_id` |
| `TargetAddress` | `provider`, `opaque: Mapping[str, str]` |

`DeliveryCapabilities` is the contract that makes provider-agnostic processing
possible: the Processing planner consumes it without knowing what Telegram is.
`can_serve_back` distinguishes a destination that can return the bytes (Telegram,
S3) from one that cannot (a webhook, an email) — and **only a `can_serve_back`
destination can justify local deletion** ([11](11-storage-strategy.md) §11.3).

### Repository port

```
DeliveryRepository:
  add · save · get
  find_for_asset(asset_id) -> Sequence[Delivery]
  find_reusable_ref(asset_id, provider, principal) -> RemoteArtifactRef?
  claim_next(owner, lease, limit, now) -> Sequence[Delivery]
DeliveryTargetRepository:
  add · save · get · find_for_principal(principal_id)
```

---

## 6.6 Processing context

Pure decision logic; execution is an adapter.

| Type | Kind | Notes |
| ---- | ---- | ----- |
| `ProcessingPlan` | VO | ordered `steps`, `estimated_cost`, `expected_output` |
| `ProcessingStep` | VO | `Remux \| Transcode \| Split \| Thumbnail \| StripMetadata`, each with parameters |
| `ProcessingCost` | VO | `cpu_seconds_estimate`, `output_bytes_estimate` |
| `DeliveryConstraints` | VO | projection of `DeliveryCapabilities` into planner inputs |
| `ProcessingPlanner` | domain service | `(TechnicalProfile, DeliveryConstraints, ProcessingPolicy) -> ProcessingPlan \| Undeliverable` |
| `ProcessingPolicy` | policy | `max_cpu_seconds`, `allow_transcode`, `prefer_split_over_transcode`, `quality_floor` |

The planner's decision order encodes the Pi constraint explicitly:
**passthrough → remux → split → transcode → refuse.** Every step down that list
costs an order of magnitude more CPU.

---

## 6.7 Source Intelligence context

| Type | Kind | Notes |
| ---- | ---- | ----- |
| `SourceProbe` | VO | `title`, `kind`, `duration_ms?`, `expected_bytes?`, `formats`, `is_live`, `is_resumable`, `provider_id`, `probed_at` |
| `SourceCanonicalizer` | domain service | `(raw_url) -> SourceRef`; **versioned** |
| `ProviderId` | VO | stable string |
| `ProviderHealth` | entity | consecutive failures, last success, circuit state |
| `UrlPolicy` | policy | scheme/host/IP rules — the SSRF gate ([14](14-security-architecture.md) §14.3) |

`UrlPolicy` lives in the **domain**, not in middleware, because "which URLs may
this system fetch" is a business rule that must apply identically to HTTP,
Telegram, CLI and automation.

---

## 6.8 Access context

| Type | Kind | Notes |
| ---- | ---- | ----- |
| `Principal` | aggregate | `id`, `display_name`, `role`, `enabled`, `identities` |
| `PrincipalIdentity` | VO | `(scheme, external_id)` — e.g. `("telegram", "12345")` |
| `Role` | enum | `OWNER`, `MEMBER`, `READONLY` |
| `Quota` | VO | `window`, `max_jobs`, `max_bytes`, `max_concurrent` |
| `QuotaUsage` | entity | rolling counters per principal per window |
| `AuthorizationPolicy` | policy | `(principal, action, resource) -> Allowed \| Denied(reason)` |
| `RateLimitPolicy` | policy | token bucket per principal per action |
| `AuditEntry` | entity | append-only: who, what, when, outcome, correlation id |

---

## 6.9 Automation context (future)

| Type | Kind | Notes |
| ---- | ---- | ----- |
| `Subscription` | aggregate | `source`, `Schedule`, `filters`, `destinations`, `max_items_per_run`, `enabled` |
| `Schedule` | VO | cron expression + timezone; `next_after(now)` |
| `SubscriptionRun` | entity | one execution: found, admitted, skipped, failed |
| `SeenMarker` | VO | `(subscription_id, provider_item_id)` — idempotency across runs |
| `ItemFilter` | specification | title regex, duration range, kind, published-after |

---

## 6.10 DTO boundary

Domain objects never cross out of the application layer.

| Direction | Type | Example |
| --------- | ---- | ------- |
| in | Command / Query | `SubmitAcquisitionCommand` |
| out | Summary DTO | `AssetSummary`, `JobSummary`, `DeliverySummary` |
| out (bulk) | Projection row | `AssetListRow` — read model, never an aggregate |

DTOs are frozen dataclasses of primitives. Enums may cross (they are closed
value sets and part of the published language); aggregates and mutable entities
may not.

---

## 6.11 Aggregate boundary summary

| Aggregate | Transaction scope | Cardinality | Consistency with |
| --------- | ----------------- | ----------- | ---------------- |
| `MediaAsset` | itself | 1 per unique source/fingerprint | Delivery: eventual (via events) |
| `AcquisitionJob` | itself | 0..N per asset, ≤1 active | Asset: eventual |
| `Delivery` | itself | 1 per (asset, target, attempt-set) | Asset: eventual |
| `DeliveryTarget` | itself | small, stable set | — |
| `Principal` | itself | small, stable set | — |
| `Subscription` | itself | small | Jobs: eventual |

"≤1 active job per asset" is enforced at **two** levels: a domain check for a
friendly error, and a partial unique index for correctness under concurrency.
The domain check is a courtesy; the index is the guarantee.
