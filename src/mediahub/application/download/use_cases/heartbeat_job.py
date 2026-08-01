"""Use case: keep a lease alive and find out whether to stop.

One round trip does both jobs. That is deliberate: a separate cancellation poll
is a second code path, a second failure mode and a second thing to forget
(``docs/architecture/10-worker-architecture.md`` §10.2).

The use case touches no aggregate. Extending a lease changes nothing a user can
see and records no history - it is the mechanical statement "I am still here",
and giving it a domain event would fill the log with noise at one line per
thirty seconds per job.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.ports import Clock
    from mediahub.application.download.queue import JobQueue, Lease, LeaseState

DEFAULT_LEASE_SECONDS: Final[float] = 120.0


class HeartbeatJob:
    """Extend the lease on a running job and read its cancellation flag."""

    def __init__(
        self,
        *,
        queue: JobQueue,
        clock: Clock,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        """Wire the use case to its ports."""
        self._queue = queue
        self._clock = clock
        self._lease_seconds = lease_seconds

    async def execute(self, lease: Lease) -> LeaseState:
        """Renew ``lease`` and report whether cancellation was requested.

        Args:
            lease: The lease held by the caller.

        Returns:
            The renewed lease and the cancellation flag as of this instant.

        Raises:
            LeaseLostError: If the caller no longer owns the job. The correct
                response is to abandon the work immediately: another worker owns
                it now, and a second writer is how one job becomes two.
        """
        return await self._queue.extend_lease(
            lease,
            lease_seconds=self._lease_seconds,
            now=self._clock.now(),
        )
