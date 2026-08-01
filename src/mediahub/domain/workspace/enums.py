"""The states a lease and a device can be in.

Both are closed sets with rules attached, which is why they are enums in the
domain rather than strings in an adapter: "is this lease recoverable?" and "may
this device accept more work?" are questions with one answer, not three
implementations.
"""

from __future__ import annotations

from enum import StrEnum


class LeaseState(StrEnum):
    """Where a lease is in its lifecycle.

    The lifecycle is the one in ``docs/architecture/11-storage-strategy.md``
    §11.5. It is stored in the lease manifest, so a process that starts after a
    crash can tell a lease that was mid-download from one that was already being
    torn down - and treat them differently.

    Attributes:
        RESERVED: Space has been accounted for; nothing has been written yet.
        ACTIVE: At least one artifact exists inside the lease.
        RELEASING: The owner asked for the lease to go away.
        ORPHANED: The lease outlived its owner.
        DELETED: The directory is gone. Terminal.
    """

    RESERVED = "reserved"
    ACTIVE = "active"
    RELEASING = "releasing"
    ORPHANED = "orphaned"
    DELETED = "deleted"

    @property
    def is_terminal(self) -> bool:
        """Return whether the lease can never change state again."""
        return self is LeaseState.DELETED

    @property
    def holds_work(self) -> bool:
        """Return whether a live owner could still be writing inside it.

        The two states that answer ``True`` are exactly the ones a crash can
        leave behind with useful bytes in them, which is what makes recovery
        worth attempting rather than always deleting.
        """
        return self in {LeaseState.RESERVED, LeaseState.ACTIVE}


class DiskState(StrEnum):
    """How much room the device has left, as a decision rather than a number.

    The thresholds live in :class:`~mediahub.domain.workspace.policies.DiskPolicy`
    (``docs/architecture/11-storage-strategy.md`` §11.7). Running out of disk on
    a small device does not produce a clean error - it produces a corrupt
    database - so free space is an admission input, not an exception path.

    Attributes:
        HEALTHY: Plenty of headroom; work is admitted normally.
        TIGHT: Only small requests are admitted.
        LOW: No new acquisitions; work already holding bytes may finish.
        CRITICAL: Nothing is admitted.
    """

    HEALTHY = "healthy"
    TIGHT = "tight"
    LOW = "low"
    CRITICAL = "critical"

    @property
    def accepts_new_work(self) -> bool:
        """Return whether a new reservation may be granted at all."""
        return self in {DiskState.HEALTHY, DiskState.TIGHT}


class RecoveryAction(StrEnum):
    """What to do with a lease directory found on disk after a restart.

    Attributes:
        ADOPT: It is ours and it still holds usable bytes; keep it.
        DELETE: Nobody can use it; reclaim the space.
        LEAVE: It may belong to a process that is still running. Deleting it
            would corrupt a healthy job, so the sweep looks away and tries again
            later - the conservative half of
            ``docs/architecture/11-storage-strategy.md`` §11.6.
    """

    ADOPT = "adopt"
    DELETE = "delete"
    LEAVE = "leave"
