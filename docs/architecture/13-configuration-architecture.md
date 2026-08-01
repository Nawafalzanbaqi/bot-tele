# 13. Configuration Architecture

Configuration is where a well-designed system goes to die quietly: an untyped
string read three layers deep, defaulting to something plausible, wrong in
production, discovered in six months.

**One typed object, validated at boot, immutable, injected.** Everything else is
a bug.

---

## 13.1 Principles

1. **Typed or it does not exist.** Every setting is a field with a type,
   default, and docstring. `os.environ` appears in exactly one package.
2. **Validated at startup, not at use.** A misconfigured process must **refuse
   to start**. A process that boots and fails on the first job at 2 a.m. is
   strictly worse.
3. **Immutable.** Frozen models. No runtime mutation, no hot-reload of
   behaviour-changing values. Configuration that changes under a running job is
   a debugging nightmare for a feature nobody asked for.
4. **Injected, never imported.** Components receive `Settings` (or a section)
   through their constructor. `from ...settings import settings` at module scope
   makes testing a fight.
5. **Secrets are typed as secrets.** `SecretStr`, redacted in logs, never in the
   database, never in an error message.
6. **Every setting has a safe default except secrets.** The app should start on
   a fresh Pi with an empty `.env` in local mode. Secrets must be explicit.

Phase 01 implemented 1, 2, 3 and 6 correctly. This document extends the schema
and adds flags, profiles and secret handling.

---

## 13.2 Structure

`MEDIAHUB_<SECTION>__<FIELD>`, nested by double underscore.

| Section | Owns | Examples |
| ------- | ---- | -------- |
| *(root)* | environment, debug | `MEDIAHUB_ENVIRONMENT` |
| `api` | HTTP surface | `API__PORT`, `API__CORS_ORIGINS` |
| `database` | SQLite file, pragmas, busy timeout | `DATABASE__PATH`, `DATABASE__BUSY_TIMEOUT_MS` |
| `workspace` | root, backend, size caps, sweep periods | `WORKSPACE__ROOT`, `WORKSPACE__MAX_ITEM_BYTES` |
| `queue` | lanes, concurrency, lease, backoff | `QUEUE__ACQUISITION_SLOTS` |
| `worker` | identity, drain grace, poll interval | `WORKER__DRAIN_GRACE_SECONDS` |
| `download` | provider config, timeouts, ceilings | `DOWNLOAD__STALL_TIMEOUT_SECONDS` |
| `processing` | FFmpeg path, cost ceilings, policy | `PROCESSING__MAX_CPU_SECONDS` |
| `delivery` | default targets, retry policy | `DELIVERY__MAX_ATTEMPTS` |
| `telegram` | token, allow-list, api base url, mode | `TELEGRAM__BOT_TOKEN` |
| `security` | secret key, allowed hosts, URL policy | `SECURITY__BLOCK_PRIVATE_NETWORKS` |
| `logging` | level, format, rotation | `LOGGING__JSON_FORMAT` |
| `monitoring` | metrics, tracing, alerts | `MONITORING__METRICS_ENABLED` |
| `retention` | history, DLQ, audit, cache TTLs | `RETENTION__DLQ_DAYS` |
| `features` | feature flags | `FEATURES__ENABLE_AI_ENRICHMENT` |

Sections map 1:1 to subsystems. A subsystem receives **its own section**, not
the whole `Settings` object — a downloader that can read the Telegram token is
an unnecessary blast radius.

---

## 13.3 Profiles

`MEDIAHUB_ENVIRONMENT` ∈ `local | testing | staging | production`. It changes
**defaults and validation strictness**, never behaviour paths.

| Setting | local | testing | staging | production |
| ------- | ----- | ------- | ------- | ---------- |
| `debug` | true | true | false | **false, enforced** |
| `logging.level` | DEBUG | WARNING | INFO | INFO |
| `logging.json_format` | false | false | true | true |
| `logging.diagnose` | true | false | false | **false, enforced** |
| `api.docs_enabled` | true | true | true | operator choice |
| `database.path` | `./data/dev.db` | `:memory:` | `/data/…` | `/data/…` |
| `workspace.backend` | disk | memory | disk | disk |
| placeholder secrets | allowed | allowed | **refused** | **refused** |

"Enforced" means a `model_validator` raises and the process exits non-zero. This
already exists in Phase 01 and is one of its better decisions — it turns a
class of production incident into a failed deploy.

**No `if environment == "production"` outside configuration.** Branching on the
environment inside business code means production runs a path that was never
tested anywhere else.

---

## 13.4 Feature flags

Flags exist to **decouple deployment from release** and to keep unfinished
subsystems inert.

| Flag | Default | Purpose |
| ---- | ------- | ------- |
| `features.enable_telegram` | false | Gateway + provider registration |
| `features.enable_processing` | false | FFmpeg-dependent stages |
| `features.enable_automation` | false | Subscriptions |
| `features.enable_ai_enrichment` | false | AI lane and providers |
| `features.enable_search_index` | true | FTS5 projector |
| `features.enable_webhook_mode` | false | Telegram webhook instead of long-poll |
| `features.enable_local_bot_api` | false | Raises upload ceiling |
| `features.enable_tracing` | false | OpenTelemetry |

Rules:

- **Evaluated once, at composition.** A disabled subsystem is simply not wired —
  no `if flag:` scattered through call paths, no half-initialised adapters.
- **Boolean and typed.** No string flags, no percentage rollouts: there is one
  node and one household.
- **Flags are temporary by intent.** Each carries a comment naming the condition
  for its removal. A permanent flag is really a configuration option and should
  be modelled as one.
- **Off by default** for anything unfinished, so `main` is always deployable.

---

## 13.5 Secrets

| Secret | Source | Never |
| ------ | ------ | ----- |
| `telegram.bot_token` | env / Docker secret file | in the DB, in logs, in errors, in metrics labels |
| `security.secret_key` | env / Docker secret file | committed, defaulted in production |
| Provider credentials (S3, AI) | env / secret file | in plugin manifests |

Mechanics:

- Typed `SecretStr`; `repr` and serialisation are redacted by construction.
- `_FILE` convention supported: `MEDIAHUB_TELEGRAM__BOT_TOKEN_FILE=/run/secrets/tg`
  so Docker/K8s secrets work without env exposure. Env vars are visible in
  `/proc/<pid>/environ` to anything running as the same user.
- **A redacting log sink** is mandatory: even with `SecretStr`, tokens leak
  through third-party libraries that log request URLs (`api.telegram.org/bot<TOKEN>/…`).
  The sink pattern-matches known secret shapes and replaces them. This is the
  single highest-value logging control in the system.
- Rotation is a documented procedure with consequences (§12.5), not a config
  edit.
- Startup logs a **fingerprint** (first 4 + last 2 chars, length) so an operator
  can confirm *which* token is loaded without exposing it.

---

## 13.6 Validation

Three layers, in order:

1. **Field level** — types, ranges, formats. `port ∈ [1, 65535]`,
   `slots ∈ [0, 16]`, paths absolute.
2. **Cross-field** — coherence. `emergency_reserve < min_free_bytes`;
   `worker.drain_grace < container stop grace`; webhook mode requires a public
   URL and a secret; `enable_processing` requires an FFmpeg binary that exists
   and executes.
3. **Environment** — production hardening (§13.3).

Startup emits a **configuration report** at INFO: every non-secret effective
value with its source (default / env / file). "Which value is actually in
effect?" must never require reading code — it is the first question of every
incident.

---

## 13.7 What is configurable, and what is not

| Configurable | Not configurable |
| ------------ | ---------------- |
| Limits, timeouts, concurrency, paths | State transitions |
| Retry counts and backoff parameters | Which failures are retryable (that is domain classification) |
| Which providers are enabled | The port contracts |
| Retention windows | That media is deleted after delivery |
| Log level and format | What gets logged |
| Feature flags | Layer boundaries |

The right-hand column is the architecture. Making it configurable would mean
every deployment is a different system, and no test result would generalise.

**Anti-pattern refused:** configuration-driven pipelines ("define your stages in
YAML"). It looks flexible and produces a bespoke, untested, undebuggable
program per installation. Stages are code, reviewed and tested.

---

## 13.8 Per-process configuration

Each process role validates only what it uses:

| Process | Requires | Ignores |
| ------- | -------- | ------- |
| `api` | api, database, security, logging | download, processing, telegram token |
| `worker` | queue, worker, workspace, download, processing, delivery, database | api, cors |
| `telegram-gateway` | telegram, security, database, logging | processing, workspace |
| `scheduler` | queue, retention, workspace, database | api, telegram token |

A missing Telegram token must not stop the API from booting. Role-scoped
validation is what makes partial deployments (e.g. Telegram disabled) genuinely
supported rather than accidentally working.

---

## 13.9 Configuration changes at runtime

| Change | Requires |
| ------ | -------- |
| Log level | restart (or a signal handler, if ever justified) |
| Concurrency slots | worker restart |
| Limits and timeouts | restart of the owning process |
| Feature flags | restart |
| Destinations, quotas, subscriptions | **nothing — these are data, not configuration** |

The last row matters. Anything a user manages (delivery targets, quotas,
subscriptions) belongs in the database with a UI and an audit trail, not in
`.env`. Confusing the two produces a product where adding a chat requires SSH.
