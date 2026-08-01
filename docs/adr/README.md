# Architecture Decision Records

Short documents recording decisions that were expensive to make and would be
expensive to reverse: what was decided, why, and what it costs.

They are immutable. A decision that no longer holds is not edited - a new ADR
supersedes it, and the old one keeps the history intact. Code explains *how*;
git explains *when*; these explain *why*, which is the part nobody can
reconstruct two years later.

## Phase 01 — Foundation

| ADR | Title | Status |
| --- | ----- | ------ |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-clean-architecture-layering.md) | Clean Architecture with enforced layering | Accepted |
| [0003](0003-explicit-orm-mapping.md) | Explicit ORM mapping instead of active record | Accepted |
| [0004](0004-downloader-behind-a-port.md) | Ship the download seam before the engine | Accepted |
| [0005](0005-dual-persistence-adapters.md) | Two persistence adapters, one contract | Accepted |

## Phase 02 — Product architecture

| ADR | Title | Status |
| --- | ----- | ------ |
| [0006](0006-sqlite-system-of-record.md) | SQLite as the system of record | Accepted — supersedes the PostgreSQL assumption in 0005 |
| [0007](0007-ephemeral-local-media.md) | Local media is ephemeral; custody transfers to the destination | Accepted — supersedes the storage model in 0003 |
| [0008](0008-sqlite-job-queue.md) | A leased SQLite table is the job queue | Accepted — retracts the Redis service |
| [0009](0009-provider-agnostic-delivery.md) | Delivery is a bounded context; Telegram is one provider | Accepted |
| [0010](0010-separate-worker-process.md) | Separate process roles from one image | Accepted |
| [0011](0011-trusted-in-process-plugins.md) | Plugins are contracts now, trusted code for v1 | Accepted |

The full reasoning behind Phase 02, including a critical review of every
Foundation decision, is in [docs/architecture/](../architecture/) — start with
[20. Architecture Decision Review](../architecture/20-architecture-decision-review.md).

## Format

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by ADR-XXXX
- Date: YYYY-MM-DD

## Context      What forces are at play?
## Decision     What we are doing, in the active voice.
## Consequences What gets better, what gets worse, what we accept.
```
