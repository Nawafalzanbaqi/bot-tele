"""What the workspace reports back.

One DTO, describing a sweep. It is deliberately a *count of everything*, not
just of what was deleted: "left alone: 4" is the number that tells an operator a
second process is sharing the root, and "reclaimed: 0" after a crash tells them
the leak they are chasing is somewhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """The outcome of one crash-recovery sweep.

    Attributes:
        examined: Lease directories found under the root.
        adopted: Ids of leases kept because they are ours and still hold work.
        deleted: Ids of leases removed.
        left: Ids of leases that were left alone, because they may belong to a
            process that is still running.
        reclaimed_bytes: Space returned to the device.
        failed: Ids of leases that could not be removed. Not an error - a sweep
            that cannot finish must still report and converge on the next run.
    """

    examined: int = 0
    adopted: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    left: tuple[str, ...] = ()
    reclaimed_bytes: int = 0
    failed: tuple[str, ...] = field(default=())

    @property
    def changed_anything(self) -> bool:
        """Return whether the sweep had anything to do."""
        return bool(self.deleted or self.adopted or self.failed)
