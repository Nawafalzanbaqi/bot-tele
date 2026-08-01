# 16. Monitoring

A self-hosted product has no on-call rota. The operator is a person who will
notice something is wrong **days later**, and whose first question will be
"what happened to the thing I asked for on Tuesday?".

Observability is therefore designed around **one question per signal**, and
around costing almost nothing on a Raspberry Pi.

---

## 16.1 Budget

| Signal | Cost ceiling | Enforcement |
| ------ | ------------ | ----------- |
| Logs | < 50 MB/day, rotated at 7 days / 100 MB | rotation config, sampling of chatty events |
| Metrics | < 20 MB RSS, pull-based | in-process registry, no push agent |
| Traces | off by default | flag; sampled at 1% when on |
| Progress writes | ≤ 1 DB write / job / 5 s | throttled registry ([05](05-component-communication.md) §5.9) |
| Health checks | < 5 ms (liveness), < 200 ms (readiness) | no dependency calls in liveness |

Unbounded logging is not a monitoring problem on this hardware — it is a
hardware failure mode. An unrotated debug log fills an SD card in weeks and
takes the database with it.

---

## 16.2 Health checks

Three endpoints with genuinely different meanings. Collapsing them is the most
common health-check mistake and produces restart loops during database blips.

| Endpoint | Question | Checks | Failure |
| -------- | -------- | ------ | ------- |
| `/health/live` | Is the process alive? | nothing | restart the container |
| `/health/ready` | Should it receive traffic? | DB reachable, migrations current, workspace writable | remove from rotation |
| `/health/deep` | Is the whole system healthy? | + disk headroom, queue depth, lease reclaim rate, provider circuits, DLQ size, scheduler heartbeat | operator attention |

`/health/deep` is not called by the orchestrator — it is the operator's
dashboard, and it is the one that must answer "why is nothing downloading?".

Per-role liveness: workers and the scheduler have no HTTP surface, so they write
a **heartbeat row** (`component_heartbeats`) that `/health/deep` reads. A worker
that has not beaten in 3 periods is reported as dead — which is exactly the
symptom of the silent failure this system is most prone to.

---

## 16.3 Logging

Structured, correlated, redacted. Phase 01's foundation (Loguru, correlation id
in a `ContextVar`, stdlib interception) is correct and extends as follows.

**Mandatory context on every record:**

| Field | Source |
| ----- | ------ |
| `correlation_id` | request/update, propagated into the job |
| `job_id`, `stage`, `attempt` | worker context |
| `principal_id` | command context |
| `component`, `environment`, `version` | process |

**Correlation must survive the async boundary.** A job carries the
`correlation_id` of the request that created it, so a user's complaint traces
through gateway → API → queue → worker → delivery in one query. This is the
single most valuable observability property in the system, and it is free if the
id is stored on the job row and re-bound when a worker claims it.

**Levels, with meaning:**

| Level | Use | Example |
| ----- | --- | ------- |
| DEBUG | Development only | Selected format, planner reasoning |
| INFO | Lifecycle facts | Job submitted, stage completed, delivery succeeded |
| WARNING | Handled degradation | Transient failure, retry scheduled, circuit opened |
| ERROR | Needs a human eventually | Dead letter, permanent failure, plugin rejected |
| CRITICAL | Needs a human now | DB corruption, token invalid, disk critical |

Rules: no `print` (enforced by lint); no secrets (redacting sink, §14.8); no
full URLs at INFO; no per-chunk logging; exceptions logged once, at the boundary
that handles them — a stack trace logged at three levels is three times the
noise and none of the information.

---

## 16.4 Metrics

Prometheus text format on `/metrics`, pull-based. No push agent, no time-series
database required on the device — a scraper elsewhere on the LAN is optional,
and the endpoint is readable by a human with `curl`.

### The signals that matter

**Golden signals, per lane:**

| Metric | Type | Purpose |
| ------ | ---- | ------- |
| `mediahub_jobs_total{lane,outcome}` | counter | throughput and failure rate |
| `mediahub_job_duration_seconds{lane,stage}` | histogram | where time goes |
| `mediahub_queue_depth{lane,status}` | gauge | backlog |
| `mediahub_queue_wait_seconds` | histogram | enqueue → claim |

**The ones specific to this product** — more useful than the generic ones:

| Metric | Why it exists |
| ------ | ------------- |
| `mediahub_disk_headroom_bytes` | The device's life expectancy |
| `mediahub_custody_assets{state}` | **`LOCAL_*` climbing means cleanup is broken** |
| `mediahub_orphans_deleted_total` | Steady growth means a leak somewhere |
| `mediahub_lease_reclaims_total` | Silent crashes |
| `mediahub_dlq_size` | Work that needs a human |
| `mediahub_dedup_hits_total{layer}` | How much work is being avoided |
| `mediahub_bytes_downloaded_total` / `mediahub_bytes_delivered_total` | Bandwidth reality |
| `mediahub_provider_failures_total{provider,kind}` | Which extractor broke this week |
| `mediahub_sqlite_busy_retries_total` | Write contention approaching the ceiling |
| `mediahub_processing_seconds_total` | CPU spent transcoding — the Pi's scarcest resource |

`mediahub_custody_assets{state="LOCAL_AND_REMOTE"}` is the most important gauge
in the system: it is the direct measurement of the product's central rule. If it
does not return to zero, the device is filling up and the design has been
violated.

**Cardinality discipline:** labels are bounded sets (lane, stage, outcome,
provider, kind). Never `job_id`, never `url`, never `principal_id`. Unbounded
labels turn a 20 MB metrics registry into a 2 GB memory leak.

---

## 16.5 Tracing

OpenTelemetry, **off by default**, enabled by `features.enable_tracing`.

| Span | Attributes |
| ---- | ---------- |
| `acquisition.job` (root, links to the request span) | job id, lane, priority |
| `stage.download` | provider, bytes, resumed |
| `stage.process` | steps, cpu seconds |
| `delivery.transfer` | provider, mode (artifact/ref), bytes |
| `db.query` (sampled) | statement name |

Rationale: on a single node, structured logs with a correlation id answer 95% of
questions at a fraction of the cost. Tracing earns its keep for latency
archaeology — "why did this job take four hours?" — and is a flag away when
needed.

Trace context propagates through the job row alongside the correlation id, so a
trace spans the async boundary rather than stopping at the queue.

---

## 16.6 Alerts

Alerting on a self-hosted box means **notifying the owner through a destination
they already have** — MediaHub delivers, so it can notify itself.

| Alert | Condition | Severity | Channel |
| ----- | --------- | -------- | ------- |
| Disk critical | headroom < 5% | critical | notification target + log CRITICAL |
| Token invalid | Telegram `401` | critical | log CRITICAL (the usual channel is dead) |
| DB corruption | `integrity_check` fails | critical | log CRITICAL |
| Dead letters | `dlq_size > 0` for 1 h | error | notification target |
| Queue stalled | depth > 0, no claims for 15 min | error | notification target |
| Custody stuck | `LOCAL_AND_REMOTE` older than 1 h | error | notification target |
| Worker missing | no heartbeat for 3 periods | error | notification target |
| Provider degraded | circuit open > 30 min | warning | log |
| Backup failed | no successful backup in 48 h | warning | notification target |
| High retry rate | > 50% transient for 15 min | warning | log |

Design rules: **alert on symptoms the operator cares about**, not on internal
metrics ("CPU high" is not an alert; "nothing has downloaded for 15 minutes"
is). Every alert names the check that fired and the command that diagnoses it.
Alerts are rate-limited per condition (one per hour) so a bad night does not
produce 400 messages.

---

## 16.7 The operator's questions

Monitoring is designed backwards from these:

| Question | Answered by |
| -------- | ----------- |
| "Did my download work?" | Job status + delivery receipt in the UI/bot |
| "Why did it fail?" | `FailureReport` (kind, code, stage) on the job; logs by `job_id` |
| "Why is nothing happening?" | `/health/deep`: queue depth, worker heartbeats, disk state, circuits |
| "Is the disk filling up?" | `disk_headroom_bytes` + `custody_assets{LOCAL_*}` |
| "What did I download last month?" | Catalogue history query |
| "Is Telegram broken or is it me?" | `provider_failures_total{provider=telegram}` + circuit state |
| "Did the update break anything?" | Job success rate before/after the version label change |
| "Can I still restore?" | Last successful backup timestamp on `/health/deep` |

If a signal answers none of these, it is not collected. That constraint is what
keeps observability affordable on this hardware.
