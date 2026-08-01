"""The numbers a worker runs on.

A parameter object rather than a settings import, for the same reason every
other driver in this codebase takes primitives: the runtime is then testable
with three lines and no environment, and the mapping from configuration to
behaviour happens exactly once, in the composition root.

The one relationship that must hold is checked in two places on purpose - here,
where a hand-built runtime could get it wrong, and in
:class:`~mediahub.shared.config.settings.WorkerSettings`, where a deployment
could. **The heartbeat must be comfortably faster than the lease.** If it is
not, healthy jobs are reclaimed while they are still running, which presents as
random duplicate execution rather than as a misconfiguration - the most
expensive kind of bug to diagnose.
"""

from __future__ import annotations

from dataclasses import dataclass

from mediahub.application.download.errors import WorkerNotReadyError

MINIMUM_HEARTBEAT_RATIO = 2.0
"""A lease must outlast at least two heartbeats, so one missed tick is survivable."""


@dataclass(frozen=True, slots=True)
class WorkerTimings:
    """How long a worker waits, holds and reports.

    Attributes:
        lease_seconds: How long a claim is owned before it may be reclaimed.
        heartbeat_seconds: Gap between lease renewals, and therefore the
            worst-case cancellation latency.
        idle_poll_seconds: First wait after finding nothing to do.
        max_idle_poll_seconds: Ceiling the idle wait backs off to.
        progress_interval_seconds: Shortest gap between durable progress writes.
        progress_percent_step: Progress movement that forces a write anyway.
        drain_grace_seconds: How long in-flight work may take to wind down
            before the process stops waiting for it.
    """

    lease_seconds: float = 120.0
    heartbeat_seconds: float = 30.0
    idle_poll_seconds: float = 1.0
    max_idle_poll_seconds: float = 5.0
    progress_interval_seconds: float = 5.0
    progress_percent_step: float = 5.0
    drain_grace_seconds: float = 30.0

    def __post_init__(self) -> None:
        """Refuse timings that would lose leases on healthy jobs."""
        if self.heartbeat_seconds * MINIMUM_HEARTBEAT_RATIO > self.lease_seconds:
            message = (
                f"a lease of {self.lease_seconds}s cannot survive a heartbeat of "
                f"{self.heartbeat_seconds}s; the lease must outlast at least two"
            )
            raise WorkerNotReadyError(message)
        if self.max_idle_poll_seconds < self.idle_poll_seconds:
            message = "the idle backoff ceiling must not be below its starting point"
            raise WorkerNotReadyError(message)

    def backoff_from(self, current: float) -> float:
        """Return the next idle wait, doubling up to the ceiling.

        A worker with nothing to do should get quieter, not busier: on a small
        device a fast poll loop is a measurable and entirely pointless power
        draw (``docs/architecture/10-worker-architecture.md`` §10.5).
        """
        return min(current * 2, self.max_idle_poll_seconds)
