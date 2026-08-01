"""Use case: decide what to do with everything that survived the last run.

Run at startup, before any work is claimed. Every lease directory under the root
belonged to a process that may or may not still exist, and each one is worth
real disk on a device that does not have much. Three things must be true of this
sweep, and the split of responsibilities below is what makes them true:

* **It never deletes live work.** The decision is
  :class:`~mediahub.domain.workspace.policies.RecoveryPolicy`'s alone - pure,
  exhaustively testable, and conservative by default.
* **It always converges.** A directory it cannot remove is reported, not raised;
  the next sweep tries again. A recovery pass that aborts halfway is how a full
  disk becomes a permanent full disk.
* **It is idempotent.** Running it twice does nothing the second time, which is
  what makes it safe to run after a crash - including a crash inside it.

It is synchronous, unlike every other use case here, because it runs during
composition, before there is an event loop, and it performs no awaitable I/O.
Pretending otherwise would mean ``asyncio.run`` inside a container builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.workspace.dto import RecoveryReport
from mediahub.domain.workspace.enums import RecoveryAction

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mediahub.application.common.ports import Clock
    from mediahub.application.workspace.ports import LeaseRecord, WorkspaceInventory
    from mediahub.domain.workspace.policies import RecoveryPolicy
    from mediahub.domain.workspace.value_objects import LeaseOwner


class RecoverWorkspaces:
    """Find orphaned leases, decide their fate, and reclaim what is dead."""

    __slots__ = ("_clock", "_owner", "_policy", "_workspace")

    def __init__(
        self,
        *,
        workspace: WorkspaceInventory,
        policy: RecoveryPolicy,
        owner: LeaseOwner,
        clock: Clock,
    ) -> None:
        """Wire the use case to the workspace, the rules and this process.

        Args:
            workspace: Where the leases are.
            policy: How to decide. The caller chooses it, because "wipe
                everything" and "adopt my own leases" are deployment decisions.
            owner: Who is sweeping - the identity every decision is made
                relative to.
            clock: Source of the current time.
        """
        self._workspace = workspace
        self._policy = policy
        self._owner = owner
        self._clock = clock

    def execute(self) -> RecoveryReport:
        """Sweep the workspace root once.

        Returns:
            What was found and what was done about it. Never raises for a lease
            it could not handle: the failure is counted and the sweep continues,
            so one undeletable directory cannot strand the rest.
        """
        now = self._clock.now()
        records = self._workspace.leases_on_disk()
        adopted: list[str] = []
        deleted: list[str] = []
        left: list[str] = []
        failed: list[str] = []
        reclaimed = 0

        for record in records:
            action = self._decide(record, now=now)
            if action is RecoveryAction.ADOPT:
                adopted.append(record.lease_id)
                continue
            if action is RecoveryAction.LEAVE:
                left.append(record.lease_id)
                continue
            try:
                reclaimed += self._workspace.discard(record.lease_id)
            except OSError:
                # The directory is busy, read-only, or gone in a way the adapter
                # could not absorb. Never fatal: the next sweep tries again.
                logger.bind(lease_id=record.lease_id).opt(exception=True).warning(
                    "Could not reclaim a workspace lease; it will be retried"
                )
                failed.append(record.lease_id)
            else:
                deleted.append(record.lease_id)

        report = RecoveryReport(
            examined=len(records),
            adopted=tuple(adopted),
            deleted=tuple(deleted),
            left=tuple(left),
            reclaimed_bytes=reclaimed,
            failed=tuple(failed),
        )
        self._report(report)
        return report

    def _decide(self, record: LeaseRecord, *, now: datetime) -> RecoveryAction:
        """Return the action for one record, manifest or not."""
        if record.lease is None:
            return self._policy.decide_unclaimed(record.age_seconds)
        return self._policy.decide(record.lease, now=now, owner=self._owner)

    @staticmethod
    def _report(report: RecoveryReport) -> None:
        """Log the sweep, loudly enough to notice a leak and no louder."""
        bound = logger.bind(
            examined=report.examined,
            adopted=len(report.adopted),
            deleted=len(report.deleted),
            left=len(report.left),
            failed=len(report.failed),
            reclaimed_bytes=report.reclaimed_bytes,
        )
        if report.changed_anything:
            bound.info("Workspace recovery finished")
        else:
            bound.debug("Workspace recovery found nothing to do")
