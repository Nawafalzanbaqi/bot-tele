# Operations

Documentation for running MediaHub, as opposed to building it. Written for a
device that is left alone for years and is expected to keep working.

| Document | Read it when |
| --- | --- |
| [Production Checklist](production-checklist.md) | Before an instance is left unattended. Configuration, first start, the safety properties to verify yourself, and what to alert on. |
| [Failure Matrix](failure-matrix.md) | You want to know what the system does about a specific failure — and see the test that proves it still does. |
| [Recovery Matrix](recovery-matrix.md) | Something already broke and you need to know who notices, how long it takes, and what it costs. |
| [Performance Report](performance-report.md) | You are sizing hardware or settings, or a benchmark ceiling just failed. |
| [Operational Runbook](runbook.md) | Something is wrong right now. |

## The short version

Three mechanisms recover everything:

* **The lease lapses.** The only crash-detection primitive in the system. No
  liveness ping, no registry, no lock service.
* **The startup sweep runs.** Once per process start, for job leases and for
  workspace directories.
* **The context manager unwinds.** On any exit path including `BaseException`,
  which is why a killed attempt still leaks no bytes.

Two numbers bound almost everything:

* A hard crash is recovered within **two lease periods** (240 s on defaults), or
  within **one process start** if the same worker identity comes back.
* A workspace lease is deleted when its attempt ends — success, failure,
  cancellation or crash.

And one rule worth internalising: **nothing recovers because shutdown was
polite.** A graceful stop is an optimisation over what the lease already
guarantees. That is what makes power loss an ordinary event here rather than an
incident.

## Design documents

These describe how the system is built rather than how it is run:

* [`docs/architecture/`](../architecture/) — the complete system design.
* [`docs/adr/`](../adr/) — the decisions and why they were made.
* [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — the layering and its enforcement.
