"""The lease: one job's private, accounted, disposable space.

A lease is an entity rather than a bare directory because three questions have
to be answerable *after the process that opened it has died*: who owned it, how
much space it still holds against its reservation, and whether it was mid-work
or already being torn down. A directory answers none of those; a lease that is
written down as a manifest answers all three, which is what makes crash
recovery a decision instead of a guess.

Like every aggregate here it is a state machine with an explicit transition
table (``docs/architecture/11-storage-strategy.md`` §11.5), it never reads the
clock, and it never touches the filesystem - the adapter does that, and reports
back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Final

from mediahub.domain.common.entity import Entity
from mediahub.domain.common.errors import InvalidStateTransitionError, InvariantViolationError
from mediahub.domain.common.time import ensure_utc
from mediahub.domain.workspace.enums import LeaseState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from datetime import datetime

    from mediahub.domain.workspace.identifiers import LeaseId
    from mediahub.domain.workspace.value_objects import LeaseOwner

_ENTITY_NAME: Final[str] = "WorkspaceLease"


class WorkspaceLease(Entity["LeaseId"]):
    """One accounted, owned scratch directory.

    Attributes:
        ALLOWED_TRANSITIONS: The only state moves that exist. Anything else
            raises rather than silently corrupting the accounting.
    """

    __slots__ = (
        "_created_at",
        "_label",
        "_owner",
        "_reserved_bytes",
        "_state",
        "_updated_at",
        "_used_bytes",
    )

    ALLOWED_TRANSITIONS: ClassVar[Mapping[LeaseState, frozenset[LeaseState]]] = {
        LeaseState.RESERVED: frozenset(
            {LeaseState.ACTIVE, LeaseState.RELEASING, LeaseState.ORPHANED}
        ),
        LeaseState.ACTIVE: frozenset(
            {LeaseState.ACTIVE, LeaseState.RELEASING, LeaseState.ORPHANED}
        ),
        LeaseState.RELEASING: frozenset({LeaseState.DELETED}),
        LeaseState.ORPHANED: frozenset({LeaseState.DELETED}),
        LeaseState.DELETED: frozenset(),
    }

    def __init__(
        self,
        *,
        lease_id: LeaseId,
        owner: LeaseOwner,
        label: str,
        created_at: datetime,
        reserved_bytes: int = 0,
        used_bytes: int = 0,
        state: LeaseState = LeaseState.RESERVED,
        updated_at: datetime | None = None,
    ) -> None:
        """Open a lease in ``state``, defaulting to a fresh reservation.

        Args:
            lease_id: The generated identifier, which is also the directory name.
            owner: The process that may write inside it.
            label: Short, already-sanitised purpose, for logs and diagnostics.
            created_at: When the lease was opened.
            reserved_bytes: Space accounted against the device's headroom.
            used_bytes: Space currently occupied.
            state: Where the lease is in its lifecycle. Non-default values exist
                so a manifest can be read back after a restart.
            updated_at: Last activity, defaulting to ``created_at``.

        Raises:
            InvariantViolationError: If a byte count is negative.
        """
        super().__init__(lease_id)
        if reserved_bytes < 0 or used_bytes < 0:
            message = "A lease cannot reserve or use a negative number of bytes."
            raise InvariantViolationError(message)
        self._owner = owner
        self._label = label
        self._created_at = ensure_utc(created_at, field_name="created_at")
        self._reserved_bytes = reserved_bytes
        self._used_bytes = used_bytes
        self._state = state
        self._updated_at = (
            self._created_at
            if updated_at is None
            else ensure_utc(updated_at, field_name="updated_at")
        )

    @property
    def owner(self) -> LeaseOwner:
        """Return the process currently entitled to write inside the lease."""
        return self._owner

    @property
    def label(self) -> str:
        """Return the lease's human-readable purpose."""
        return self._label

    @property
    def state(self) -> LeaseState:
        """Return where the lease is in its lifecycle."""
        return self._state

    @property
    def created_at(self) -> datetime:
        """Return when the lease was opened."""
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        """Return when the lease last showed a sign of life."""
        return self._updated_at

    @property
    def reserved_bytes(self) -> int:
        """Return the space accounted against the device for this lease."""
        return self._reserved_bytes

    @property
    def used_bytes(self) -> int:
        """Return the space the lease currently occupies."""
        return self._used_bytes

    @property
    def outstanding_bytes(self) -> int:
        """Return reserved space not yet written.

        Only the unwritten part is still a *promise*; the written part is
        already visible in the filesystem's own free-space figure. Counting both
        would reserve every byte twice and refuse work the device can do.
        """
        return max(0, self._reserved_bytes - self._used_bytes)

    @property
    def is_recoverable(self) -> bool:
        """Return whether the lease could still be handed back to an owner."""
        return self._state.holds_work

    def age_seconds(self, now: datetime) -> float:
        """Return how long it has been since the lease last showed activity."""
        return (ensure_utc(now, field_name="now") - self._updated_at).total_seconds()

    def has_expired(self, now: datetime, ttl_seconds: float) -> bool:
        """Return whether the lease has been quiet for longer than ``ttl_seconds``."""
        return self.age_seconds(now) >= ttl_seconds

    def record_usage(self, used_bytes: int, now: datetime) -> None:
        """Record how much space the lease now occupies.

        The first byte written moves the lease from ``RESERVED`` to ``ACTIVE``,
        which is the distinction crash recovery needs: an empty reservation is
        worthless, a partly written download may be resumable.

        Args:
            used_bytes: Total bytes currently inside the lease.
            now: The current time.

        Raises:
            InvariantViolationError: If ``used_bytes`` is negative.
            InvalidStateTransitionError: If the lease is no longer writable.
        """
        if used_bytes < 0:
            message = f"A lease cannot use a negative number of bytes, got {used_bytes}."
            raise InvariantViolationError(message)
        if not self._state.holds_work:
            raise InvalidStateTransitionError(_ENTITY_NAME, self._state, LeaseState.ACTIVE)
        self._used_bytes = used_bytes
        if used_bytes > 0:
            self._transition(LeaseState.ACTIVE, now)
        else:
            self._touch(now)

    def adopt(self, owner: LeaseOwner, now: datetime) -> None:
        """Hand a surviving lease to a new owner.

        Recovery, not theft: the caller has already established that the
        previous owner is gone. A lease that was being torn down, or already
        declared an orphan, is never adopted - its bytes are worthless by
        definition and pretending otherwise resurrects a leak.

        Args:
            owner: The process taking over.
            now: The current time.

        Raises:
            InvalidStateTransitionError: If the lease no longer holds work.
        """
        if not self._state.holds_work:
            raise InvalidStateTransitionError(_ENTITY_NAME, self._state, LeaseState.ACTIVE)
        self._owner = owner
        self._touch(now)

    def begin_release(self, now: datetime) -> None:
        """Announce that the lease is being torn down.

        Written down *before* the files are removed, so a crash halfway through
        deletion leaves a manifest that says "this was being deleted" rather
        than one that says "this is live work".
        """
        self._transition(LeaseState.RELEASING, now)

    def mark_orphaned(self, now: datetime) -> None:
        """Record that the lease outlived its owner."""
        self._transition(LeaseState.ORPHANED, now)

    def mark_deleted(self, now: datetime) -> None:
        """Record that the directory is gone."""
        self._transition(LeaseState.DELETED, now)
        self._used_bytes = 0
        self._reserved_bytes = 0

    def _transition(self, target: LeaseState, now: datetime) -> None:
        """Move to ``target`` if the table allows it, stamping the time."""
        if target not in self.ALLOWED_TRANSITIONS[self._state]:
            raise InvalidStateTransitionError(_ENTITY_NAME, self._state, target)
        self._state = target
        self._touch(now)

    def _touch(self, now: datetime) -> None:
        """Record a sign of life, which is what defers expiry."""
        self._updated_at = ensure_utc(now, field_name="now")

    def __repr__(self) -> str:
        """Return a representation naming the state and the owner."""
        return (
            f"WorkspaceLease(id={self.id!r}, state={self._state.value!r}, "
            f"owner={str(self._owner)!r})"
        )
