# 0010. Separate process roles from one image

- Status: Accepted
- Date: 2026-08-01

## Context

The Foundation runs everything in the API process. Acquisition work is not
async-friendly: yt-dlp and FFmpeg are blocking subprocesses that saturate a
core for minutes to hours. On a four-core Raspberry Pi, one transcode in the API
process freezes every HTTP request, every health check, and the Telegram
gateway's poll loop.

There are also independent lifecycles: a downloader fix should not drop HTTP
connections, and a memory leak in an extractor should not take down the API.

The competing pressure is operational simplicity — a self-hosting user should
not have to orchestrate a fleet.

## Decision

**Four process roles, one image**, selected by the container command:
`api`, `worker`, `telegram-gateway`, `scheduler`.

- One build, one dependency set, one version tag. Rollback is one tag.
- The worker separation is **structural** and may not be collapsed.
- The gateway and scheduler *may* be collapsed into the API process behind a
  flag on a memory-constrained device; the worker may not.
- The scheduler is a singleton, guarded by an advisory lock row so that two
  accidental instances cannot double-fire timers.
- Coordination is exclusively through the database (queue, leases, heartbeats) —
  no inter-process RPC, no shared memory.

## Consequences

**Better.** A blocking transcode cannot affect API latency. Roles get
independent CPU and memory limits, and the worker — the component most exposed
to hostile input — gets the tightest confinement and no inbound network path.
Restarting a worker mid-download is safe: it drains, checkpoints and releases its
lease, and the next worker resumes. Lane-specialised workers
(`--lanes=delivery`) become possible without new code.

**Worse.** Four containers instead of one: more RAM overhead (~40 MB per idle
Python process), a more complex Compose file, and a migration ordering problem
(a dedicated one-shot migrator, since four processes racing `alembic upgrade` on
one SQLite file is a corruption scenario). The API image also carries FFmpeg and
yt-dlp it never uses — accepted, because version skew between roles is a
correctness problem while image size is only a cost.

**Depends on** ADR-0008: the database-mediated queue is what allows the roles to
coordinate without any direct communication.
