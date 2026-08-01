"""Free space, reservations, and the difference between them.

The filesystem's free-space figure answers "what is unallocated *now*", which is
the wrong question: two workers can read the same generous number a millisecond
apart and both start a download that only one of them has room for. What the
workspace needs is "what is unallocated and unpromised", so this module keeps a
ledger of outstanding reservations and subtracts them.

The ledger is per process. That is honest rather than ideal: it makes one
process' concurrent slots safe against each other, and two processes sharing a
root still need the lease table in the database
(``docs/architecture/11-storage-strategy.md`` §11.6) to be safe against each
other. Deployments run one worker; the seam is here for when they do not.

Disk-full detection lives here too. ``ENOSPC`` arrives as a generic ``OSError``
from anywhere in a write path, and turning it into a typed, *transient* failure
at the point it happens is what stops a full device from being reported as an
unknown error and retried thirty seconds later, forever.
"""

from __future__ import annotations

import errno
import shutil
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from mediahub.domain.workspace.value_objects import DiskBudget

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

_OUT_OF_SPACE: Final[frozenset[int]] = frozenset(
    {
        code
        for code in (
            getattr(errno, "ENOSPC", None),
            getattr(errno, "EDQUOT", None),
            getattr(errno, "EFBIG", None),
        )
        if code is not None
    }
)
"""Errno values that mean "there is no room", including quota and file-size caps."""


def is_out_of_space(error: OSError) -> bool:
    """Return whether ``error`` means the device or quota is full."""
    return error.errno in _OUT_OF_SPACE


@dataclass(frozen=True, slots=True)
class DiskUsage:
    """A point-in-time reading of the device holding the workspace.

    Attributes:
        capacity_bytes: Total size of the filesystem.
        free_bytes: Space the filesystem reports as free.
    """

    capacity_bytes: int
    free_bytes: int


class DiskProbe(Protocol):
    """Reads how much room a device has.

    A seam rather than a call to :mod:`shutil`, so a device of any size can be
    substituted without filling a real one. Every disk-pressure behaviour in the
    workspace is exercised through here - a test that has to actually fill a
    disk is a test nobody runs.
    """

    def usage(self, path: Path) -> DiskUsage:
        """Return the capacity and free space of the filesystem holding ``path``."""
        ...


class SystemDiskProbe:
    """Reads real free space from the filesystem."""

    __slots__ = ()

    def usage(self, path: Path) -> DiskUsage:
        """Return the capacity and free space of the filesystem holding ``path``."""
        raw = shutil.disk_usage(path)
        return DiskUsage(capacity_bytes=raw.total, free_bytes=raw.free)


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    """The ceilings an operator sets on scratch space.

    Attributes:
        min_free_bytes: Emergency reserve. Never allocatable, so that when
            everything else has gone wrong SQLite can still commit the
            transaction that records it.
        max_lease_bytes: Largest a single lease may become. Bounds the damage
            one runaway source can do, whatever it claimed its size was.
        max_total_bytes: Largest the whole workspace may become. ``None`` means
            the device is the only limit, which is right when the workspace has
            a volume of its own.
    """

    min_free_bytes: int = 0
    max_lease_bytes: int | None = None
    max_total_bytes: int | None = None


class ReservationLedger:
    """Tracks space promised to open leases but not yet written.

    Each lease contributes ``reserved - used``: the written part is already
    visible in the filesystem's own free-space figure, so counting it as
    reserved as well would hold every byte twice and refuse work the device can
    comfortably do.

    Thread-safe because leases are opened from worker slots that may run in
    different threads, and an accounting race here is exactly the failure the
    ledger exists to prevent.
    """

    __slots__ = ("_entries", "_lock")

    def __init__(self) -> None:
        """Start with nothing reserved."""
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[int, int]] = {}

    def reserve(self, lease_id: str, amount_bytes: int) -> None:
        """Record ``amount_bytes`` as promised to ``lease_id``."""
        with self._lock:
            self._entries[lease_id] = (max(0, amount_bytes), 0)

    def settle(self, lease_id: str, used_bytes: int) -> None:
        """Record how much of a lease's promise it has now written.

        Absolute rather than incremental, so calling it repeatedly with the
        lease's current size converges instead of drifting.
        """
        with self._lock:
            entry = self._entries.get(lease_id)
            if entry is None:
                return
            self._entries[lease_id] = (entry[0], max(0, used_bytes))

    def release(self, lease_id: str) -> None:
        """Forget a lease's promise entirely. Safe to call twice."""
        with self._lock:
            self._entries.pop(lease_id, None)

    def outstanding_bytes(self) -> int:
        """Return the total still promised across every open lease."""
        with self._lock:
            return sum(max(0, reserved - used) for reserved, used in self._entries.values())

    def count(self) -> int:
        """Return how many leases currently hold a reservation."""
        with self._lock:
            return len(self._entries)

    def identifiers(self) -> frozenset[str]:
        """Return the leases this process is currently holding open.

        A snapshot rather than a view: orphan detection compares it against what
        is on disk, and a set that changed underneath that comparison would
        report a lease being opened right now as a leak.
        """
        with self._lock:
            return frozenset(self._entries)


class DiskAccountant:
    """Combines the device, the ledger and the emergency floor into a budget.

    Everything that decides whether work may start asks this object, so the
    subtraction happens once. A caller that reads free space directly is a
    caller that will eventually forget the reservations.
    """

    __slots__ = ("_emergency_bytes", "_ledger", "_probe", "_root")

    def __init__(
        self,
        root: Path,
        *,
        emergency_bytes: int = 0,
        probe: DiskProbe | None = None,
        ledger: ReservationLedger | None = None,
    ) -> None:
        """Bind the accountant to a root, a floor, and a way to read the device.

        Args:
            root: Path on the device being accounted for.
            emergency_bytes: Space that is never allocatable, so that the
                database can still commit the transaction recording whatever
                went wrong.
            probe: How free space is read. Defaults to the real filesystem.
            ledger: Where reservations are tracked. Defaults to a fresh one.
        """
        self._root = root
        self._emergency_bytes = max(0, emergency_bytes)
        self._probe = probe or SystemDiskProbe()
        self._ledger = ledger or ReservationLedger()

    @property
    def ledger(self) -> ReservationLedger:
        """Return the reservation ledger, for leases to update as they write."""
        return self._ledger

    def budget(self) -> DiskBudget:
        """Return the current free/reserved/emergency picture."""
        usage = self._probe.usage(self._root)
        return DiskBudget(
            capacity_bytes=usage.capacity_bytes,
            free_bytes=usage.free_bytes,
            reserved_bytes=self._ledger.outstanding_bytes(),
            emergency_bytes=self._emergency_bytes,
        )
