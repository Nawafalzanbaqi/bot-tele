"""The worker process: the part of MediaHub that actually runs jobs.

It lives under ``presentation`` because it is a **driver** - something that
drives the application layer from the outside, like the HTTP API and the
Telegram gateway - even though it presents nothing to anybody
(``docs/architecture/19-folder-structure.md`` §19.1).

It is deliberately the dumbest component in the system. It claims a job, runs
its stages in order, checkpoints between them, reports what it sees, and hands
the outcome back. It decides nothing: not whether to retry, not how long to wait,
not what a stage means, not where bytes go. Every one of those is a rule, and
rules live in the domain and the use cases
(``docs/architecture/10-worker-architecture.md``).

The pieces:

===================  ==========================================================
:mod:`runtime`       Supervisor: readiness, recovery, slots, drain, shutdown
:mod:`loop`          One claim loop per slot: claim, execute, settle, back off
:mod:`executor`      Runs the stage sequence for one job and checkpoints it
:mod:`heartbeat`     Keeps the lease alive and notices cancellation
:mod:`progress`      In-memory, coalescing, throttled progress
:mod:`shutdown`      Turns signals into a drain request
:mod:`stages`        What a stage is; the handlers that implement them
:mod:`services`      The use cases a worker is allowed to call
===================  ==========================================================
"""

from __future__ import annotations
