# MediaHub

A production-grade, self-hosted media hub built on Clean Architecture.

**Status: foundation.** The catalogue, the job queue, the HTTP API, persistence,
configuration, logging, migrations, CI and the test suite are complete and
working. **Download execution is deliberately not implemented yet** - the engine
sits behind a port with a null adapter, so the seam is real, typed and tested,
and adding an engine touches one package. See
[Adding a download engine](#adding-a-download-engine).

- Python 3.13 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · Pydantic Settings · Loguru
- Ruff · Black · Mypy (strict) · Pytest · Docker Compose

---

## Quick start

### With Docker (recommended)

```bash
cp .env.example .env          # then edit the secrets
docker compose up -d --build
```

- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>
- Liveness: <http://localhost:8000/health/live>

Compose merges `docker-compose.override.yml` automatically, which builds the
`dev` image stage, mounts the working tree and enables auto-reload. For a
production-shaped run, use the base file only:

```bash
docker compose -f docker-compose.yml up -d
```

### Without Docker

Requires Python 3.13.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
make install                                        # deps + git hooks
MEDIAHUB_DATABASE__BACKEND=memory python -m mediahub
```

The in-memory backend needs no database and loses everything on exit - fine for
a first look, refused outright in production.

---

## Try it

```bash
# Catalogue an item
curl -s -X POST localhost:8000/api/v1/media \
  -H 'content-type: application/json' \
  -d '{"source_url":"https://example.com/talk.mp4","title":"A talk","media_type":"video"}'

# Queue its acquisition (accepted and stored; nothing transfers yet)
curl -s -X POST localhost:8000/api/v1/downloads \
  -H 'content-type: application/json' \
  -d '{"media_id":"<id from above>","priority":"high"}'

# Watch the queue
curl -s 'localhost:8000/api/v1/downloads?status=queued&limit=10'
```

### API surface

| Method | Path                             | Purpose                                |
| ------ | -------------------------------- | -------------------------------------- |
| `GET`  | `/health`                        | Build and environment information      |
| `GET`  | `/health/live`                   | Liveness - no dependencies touched     |
| `GET`  | `/health/ready`                  | Readiness - `503` when degraded        |
| `POST` | `/api/v1/media`                  | Register an item (`201`)               |
| `GET`  | `/api/v1/media`                  | List items, filtered and paginated     |
| `GET`  | `/api/v1/media/{id}`             | Read one item                          |
| `POST` | `/api/v1/media/{id}/archive`     | Archive an item                        |
| `POST` | `/api/v1/downloads`              | Queue a download (`202`)               |
| `GET`  | `/api/v1/downloads`              | List jobs, filtered and paginated      |
| `GET`  | `/api/v1/downloads/{id}`         | Read one job                           |
| `POST` | `/api/v1/downloads/{id}/cancel`  | Cancel a job                           |

Errors follow [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem
details. Branch on the stable `code` field, never on `detail`:

```json
{
  "type": "about:blank",
  "title": "Conflict",
  "status": 409,
  "detail": "A media item is already registered for 'https://example.com/talk.mp4'.",
  "code": "duplicate_media",
  "instance": "/api/v1/media",
  "correlation_id": "9f1c2e0a5b7d4f31a0c6e8b2d4f60193"
}
```

---

## Project structure

```
src/mediahub/
├── domain/              # Enterprise rules. Pure Python, zero dependencies.
│   ├── common/          #   entity/value-object bases, events, errors, pagination
│   ├── media/           #   the MediaItem aggregate + repository port
│   └── download/        #   the DownloadJob aggregate + repository port
├── application/         # Use cases. Depends on domain only, talks through ports.
│   ├── common/          #   use case contract, ports, unit of work
│   ├── media/           #   register / get / list / archive
│   └── download/        #   request / get / list / cancel + DownloaderPort
├── infrastructure/      # Adapters. Implements the ports.
│   ├── persistence/     #   SQLAlchemy (Postgres) and in-memory repositories
│   ├── downloader/      #   NullDownloader - the not-yet-implemented engine
│   ├── messaging/       #   domain event publishing
│   ├── system/          #   clock, id generation
│   └── di/              #   the composition root
├── presentation/        # Delivery. Today: HTTP.
│   └── api/             #   app factory, lifespan, middleware, errors, v1 routes
└── shared/              # Cross-cutting: configuration and logging. Imports no layer.

tests/
├── unit/                # domain rules, use cases, adapters - no I/O
├── integration/         # the HTTP surface end to end, over in-memory adapters
├── contract/            # every adapter of a port passes the same suite
├── security/            # a control that still holds
├── failure/             # one named production failure per module, and its recovery
├── chaos/               # random disruption; the invariants that survive any ordering
├── benchmarks/          # a measured cost with a ceiling asserted on it
└── architecture/        # the dependency rule, enforced as a test

migrations/              # Alembic; the initial schema is 0001
docker/                  # container entrypoint
```

The dependency rule points inward and is enforced by
`tests/architecture/test_layer_dependencies.py`, which fails CI on any
violation. [ARCHITECTURE.md](ARCHITECTURE.md) explains the reasoning; the
[ADRs](docs/adr/) record the decisions.

> **The full system design lives in [docs/architecture/](docs/architecture/)** —
> 20 documents covering bounded contexts, the acquisition pipeline, queue and
> worker design, storage custody, security and the target folder structure. It
> is the source of truth for all future phases and supersedes this README where
> the two disagree. Start with
> [01. Product Architecture](docs/architecture/01-product-architecture.md) and
> [20. Architecture Decision Review](docs/architecture/20-architecture-decision-review.md),
> which lists the corrections this codebase still needs (SQLite migration,
> ephemeral-media custody model, worker process).

> **For running it rather than building it, see
> [docs/operations/](docs/operations/)** — the
> [Production Checklist](docs/operations/production-checklist.md) before an
> instance is left unattended, the
> [Failure](docs/operations/failure-matrix.md) and
> [Recovery](docs/operations/recovery-matrix.md) matrices for what happens when
> something breaks and how long it takes to come back, the
> [Performance Report](docs/operations/performance-report.md) for measured costs,
> and the [Runbook](docs/operations/runbook.md) for when something is wrong now.

---

## Configuration

Every setting is a typed field on `Settings`. Environment variables are prefixed
`MEDIAHUB_` and nested with a double underscore. See `.env.example` for the full
list.

| Variable                            | Default            | Meaning                                  |
| ----------------------------------- | ------------------ | ---------------------------------------- |
| `MEDIAHUB_ENVIRONMENT`              | `local`            | `local`/`testing`/`staging`/`production` |
| `MEDIAHUB_DEBUG`                    | `false`            | Verbose errors; forced off in production |
| `MEDIAHUB_API__PORT`                | `8000`             | Listen port                              |
| `MEDIAHUB_API__CORS_ORIGINS`        | `[]`               | JSON list of allowed browser origins     |
| `MEDIAHUB_DATABASE__BACKEND`        | `postgres`         | `postgres` or `memory`                   |
| `MEDIAHUB_DATABASE__PASSWORD`       | `mediahub`         | Must be changed for production           |
| `MEDIAHUB_LOGGING__LEVEL`           | `INFO`             | Sink threshold                           |
| `MEDIAHUB_LOGGING__JSON_FORMAT`     | `false`            | One JSON object per line                 |
| `MEDIAHUB_SECURITY__SECRET_KEY`     | placeholder        | Must be changed for production           |

Startup **fails loudly** if production is configured with a placeholder secret,
`debug` enabled, `logging.diagnose` enabled, or the in-memory backend. A process
that refuses to start is far safer than one that runs misconfigured.

---

## Development

```bash
make help          # list every target
make check         # what CI runs: lint + types + tests
make format        # Black + Ruff autofix
make test-unit     # the fast subset
make up / down     # the Docker stack
make migrate       # apply pending migrations
make migration m="add tags"   # autogenerate a migration
```

Quality gates, all enforced in CI:

- **Ruff** with a broad rule set, including `D` - every module, class and
  function carries a docstring.
- **Black** at 100 columns.
- **Mypy `strict`** over `src` and `tests`.
- **Pytest** - the whole suite runs without Docker, a database or a network,
  because the in-memory adapters implement the same ports as the real ones.

Beyond the usual suites, three exist specifically to keep the system honest
about running unattended, and all three run on every commit because none of them
needs infrastructure:

```bash
pytest tests/failure    -m failure     # power loss, disk full, read-only fs,
                                       # corruption, clock jumps, kill -9
pytest tests/chaos      -m chaos       # seeded random disruption; the five
                                       # invariants that must survive any ordering
pytest tests/benchmarks -m benchmark   # memory, descriptors, hashing, throughput,
                                       # recovery and cleanup - each with a ceiling
```

A benchmark that only prints a number is a number nobody reads, so every one of
them asserts a bound. See
[docs/operations/](docs/operations/) for what those bounds mean in production.

---

## Adding a download engine

Everything around the seam already exists. To plug in an engine:

1. Write an adapter in `src/mediahub/infrastructure/downloader/` implementing
   `DownloaderPort` (`supports`, `fetch`) from
   `mediahub.application.download.ports`.
2. Return it instead of `NullDownloader` in
   `mediahub.infrastructure.di.container.build_container`.

No change is required in `domain`, `application` or `presentation`. Until then
the API answers `501 Not Implemented` with code `downloader_not_configured` if
anything tries to execute a transfer - a documented state, not a crash.

---

## The worker process

```bash
python -m mediahub.presentation.worker      # MEDIAHUB_WORKER__ENABLED=true
```

A separate process by design ([ADR-0010](docs/adr/0010-separate-worker-process.md)):
acquisition work is blocking and memory-hungry, and one transcode inside the API
process would freeze every HTTP request on a small device. Same image, different
command.

What it does, and nothing more: claims a job under a **lease**, runs its stages
in order, **checkpoints** after each one, reports progress, extends the lease
(reading the cancellation flag on the same round trip), and settles the attempt.
Every decision it appears to make - retry or not, how long to back off, when an
attempt is spent - lives in `application/download/use_cases/`, on top of the
domain. See [docs/architecture/10-worker-architecture.md](docs/architecture/10-worker-architecture.md).

Crash recovery rests on one primitive. A worker that dies stops renewing its
lease; when the lease lapses the job is claimable again and resumes **from its
last completed stage**, so a power cut costs one stage rather than one job. A
restarted worker keeps its identity (`host:role:index`) and releases its own
leases at startup instead of waiting a lease period for them to lapse.

### The acquisition pipeline

The five stages the worker executes live in `presentation/worker/stages/`:

| Stage      | What it guarantees                                                  |
| ---------- | ------------------------------------------------------------------- |
| `probe`    | The source is described and a rendition chosen - no bandwidth spent  |
| `download` | The media is in this attempt's workspace lease                       |
| `verify`   | Size, digest and lease consistency all agree                         |
| `deliver`  | The destination has confirmed, and the receipt is durable            |
| `cleanup`  | The local copy is released - **only** once a receipt exists          |

Each handler is three lines. The work is in `stages/acquisition.py`, as one
`ensure_*` method per stage, and each method *ensures* its outcome rather than
performing it: called with the outcome already recorded it returns immediately,
and called without its inputs it rebuilds them. That is what makes every stage
safe to re-run and every checkpoint safe to resume from - including the awkward
case, where a job resumes past `download` into a **new, empty lease** and simply
fetches the bytes again rather than delivering nothing.

What crosses between stages is one JSON `PipelineState` in the checkpoint's
resume token, so the write that records a stage and the state it produced is a
single write and the two cannot disagree. Once the receipt is in it, delivery
never happens twice.

**Adding a stage:** implement `StageHandler` in `presentation/worker/stages/`,
return it from `build_stage_handlers`. Handlers are thin translation into use
cases; a worker whose plan has a stage with no handler refuses to start rather
than claiming work it cannot finish.

**Where a queued job is delivered.** A `DownloadJob` carries no destination -
nobody chose one when it was enqueued - so the worker sends to
`delivery.default_provider` and the router picks the adapter. Per-request
destinations come from the interface that knows them: the Telegram gateway calls
`AcquireMedia` directly with the chat it was messaged from.

---

## Roadmap

- [x] Clean Architecture skeleton with enforced boundaries
- [x] Media catalogue and download-job lifecycle
- [x] HTTP API v1, RFC 9457 errors, health probes
- [x] Postgres persistence, Alembic migrations, Docker Compose
- [x] Download engine adapter (yt-dlp) and delivery providers
- [x] Worker runtime: leases, checkpoints, heartbeat, cancellation, recovery
- [x] End-to-end acquisition pipeline: probe → download → verify → deliver → cleanup
- [x] Production hardening: leak detection, disk backpressure, durable manifests,
      clock-jump tolerance, and the failure/chaos/benchmark suites behind them
- [ ] Storage adapter (staging → library promotion, checksum verification)
- [ ] Authentication and authorisation (`PermissionDeniedError` is already wired)
- [ ] Metrics and tracing

## License

MIT.
