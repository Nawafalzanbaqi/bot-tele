# MediaHub Architecture Specification

**Phase 02 — Product Architecture & Execution Blueprint**
Status: Accepted · Date: 2026-08-01 · Supersedes: nothing · Applies to: all future phases

This directory is the **single source of truth** for MediaHub's design. Code that
disagrees with it is a bug in the code, or a change that should first be made
here. No implementation exists for anything described as *Planned*.

---

## How to read this

| # | Document | Read it when |
| - | -------- | ------------ |
| [01](01-product-architecture.md) | Overall product architecture | You need the whole picture |
| [02](02-bounded-contexts.md) | Bounded contexts & context map | You are deciding where code belongs |
| [03](03-subsystems.md) | Subsystem catalogue | You are building or changing a subsystem |
| [04](04-dependency-graph.md) | Allowed & forbidden dependencies | Your import feels awkward |
| [05](05-component-communication.md) | Sync, async, events, commands, queries | Two components must talk |
| [06](06-domain-model.md) | Complete domain model | You are modelling business rules |
| [07](07-download-pipeline.md) | End-to-end acquisition lifecycle | You are touching the pipeline |
| [08](08-state-machine.md) | Job & delivery state machines | You are adding a state or transition |
| [09](09-queue-architecture.md) | Queue, priority, retry, DLQ | You are changing scheduling |
| [10](10-worker-architecture.md) | Worker lifecycle & crash recovery | You are changing execution |
| [11](11-storage-strategy.md) | Workspace, custody, cleanup, retention | You are touching files |
| [12](12-telegram-architecture.md) | Telegram as an adapter | You are integrating Telegram |
| [13](13-configuration-architecture.md) | Config, secrets, flags, profiles | You are adding a setting |
| [14](14-security-architecture.md) | Threat model & controls | Always, before merging |
| [15](15-plugin-architecture.md) | Extension points & plugin SDK | You are adding a provider |
| [16](16-monitoring.md) | Health, metrics, logs, traces, alerts | You need to see what happened |
| [17](17-testing-strategy.md) | Test taxonomy & obligations | You are writing tests |
| [18](18-deployment-architecture.md) | Docker, Compose, Kubernetes readiness | You are shipping |
| [19](19-folder-structure.md) | Final folder structure | You are creating a file |
| [20](20-architecture-decision-review.md) | Critical review of Foundation | **Read this second** |

New to the project: read [01](01-product-architecture.md), then
[20](20-architecture-decision-review.md) — it lists what Phase 01 got wrong and
what Phase 03 must fix before anything else is built.

Decisions are recorded as [ADRs](../adr/). This specification explains the
system; ADRs explain why a fork in the road was taken.

---

## The five rules

Everything else follows from these. If a proposal violates one, it is wrong,
regardless of how convenient it is.

1. **The Core never knows about Telegram.** Telegram is one delivery provider
   behind one port. Same for yt-dlp, FFmpeg, SQLite and HTTP. Deleting the
   Telegram package must break nothing but Telegram.
2. **Dependencies point inward, and across contexts only through contracts.**
   Enforced by tests, not by discipline. See [04](04-dependency-graph.md).
3. **Local media is temporary custody, never a library.** Bytes exist on the
   device only while a job needs them. Once delivery is proven, they are
   deleted. See [11](11-storage-strategy.md).
4. **Every unit of work is resumable, cancellable and idempotent.** The device
   is a Raspberry Pi on domestic power. It *will* lose power mid-download.
5. **The stable contract is the event and the port, not the class.** Internal
   models are free to change; published events and plugin interfaces are
   versioned and additive-only.

---

## Target environment

MediaHub runs on a **Raspberry Pi 4/5** (ARM64, 2–8 GB RAM), storage on SD card
or USB SSD, domestic broadband, mains power without a UPS. This is not a
footnote — it is the constraint that decides the queue, the concurrency, the
database, the storage strategy and the observability budget. Every document
states where the Pi drove the decision.

Design point (not a limit): a few hundred acquisitions per day, 1–2 concurrent
downloads, single node. The ceiling and what breaks first is documented in
[18](18-deployment-architecture.md).

---

## Status of each subsystem

| Subsystem | Status |
| --------- | ------ |
| Core domain (catalogue, acquisition) | Partially implemented (Phase 01), **needs rework** — see [20](20-architecture-decision-review.md) |
| Application use cases | Partially implemented (Phase 01) |
| HTTP API v1 | Implemented (Phase 01) |
| Configuration, logging | Implemented (Phase 01) |
| Persistence | Implemented for PostgreSQL, **must move to SQLite** |
| Queue, Worker, Scheduler | Designed here, not implemented |
| Download, Processing, Storage | Designed here, not implemented |
| Delivery, Telegram | Designed here, not implemented |
| Metadata, Search, Automation | Designed here, not implemented |
| Plugins, AI | Designed here, deliberately deferred |
