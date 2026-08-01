"""A lease accounts for its space and refuses illegal moves.

These are the rules crash recovery leans on, so they are tested against the
entity rather than through the adapter: "was this lease mid-download or already
being deleted" has to be answerable from the manifest alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediahub.domain.common.errors import InvalidStateTransitionError, InvariantViolationError
from mediahub.domain.workspace.entities import WorkspaceLease
from mediahub.domain.workspace.enums import LeaseState
from mediahub.domain.workspace.identifiers import InvalidLeaseIdError, LeaseId
from mediahub.domain.workspace.value_objects import LeaseOwner

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
LEASE_ID = LeaseId("0" * 31 + "1")
OWNER = LeaseOwner(identity="pi:worker:0", process_id=100)


def a_lease(**overrides: object) -> WorkspaceLease:
    arguments: dict[str, object] = {
        "lease_id": LEASE_ID,
        "owner": OWNER,
        "label": "acquire",
        "created_at": NOW,
        "reserved_bytes": 1_000,
    }
    arguments.update(overrides)
    return WorkspaceLease(**arguments)  # type: ignore[arg-type]


class TestLeaseId:
    def test_normalises_case(self) -> None:
        assert LeaseId("A" * 32).value == "a" * 32

    @pytest.mark.parametrize("value", ["", "short", "z" * 32, "0" * 33, "../../etc/passwd"])
    def test_refuses_anything_that_is_not_a_generated_id(self, value: str) -> None:
        with pytest.raises(InvalidLeaseIdError):
            LeaseId(value)

    def test_prints_as_its_directory_name(self) -> None:
        assert str(LEASE_ID) == "0" * 31 + "1"


class TestOwnership:
    def test_the_same_process_is_the_same_process(self) -> None:
        assert OWNER.is_same_process(LeaseOwner(identity="pi:worker:0", process_id=100))

    def test_a_new_incarnation_shares_the_identity_only(self) -> None:
        restarted = LeaseOwner(identity="pi:worker:0", process_id=200)

        assert OWNER.is_same_identity(restarted)
        assert not OWNER.is_same_process(restarted)

    def test_another_worker_shares_nothing(self) -> None:
        other = LeaseOwner(identity="pi:worker:1", process_id=100)

        assert not OWNER.is_same_identity(other)

    @pytest.mark.parametrize(
        ("identity", "process_id"),
        [("", 1), ("   ", 1), ("with\nnewline", 1), ("ok", -1)],
    )
    def test_refuses_an_owner_that_cannot_be_told_apart(
        self, identity: str, process_id: int
    ) -> None:
        with pytest.raises(InvariantViolationError):
            LeaseOwner(identity=identity, process_id=process_id)


class TestLifecycle:
    def test_starts_reserved_and_holding_work(self) -> None:
        lease = a_lease()

        assert lease.state is LeaseState.RESERVED
        assert lease.is_recoverable

    def test_the_first_byte_makes_it_active(self) -> None:
        lease = a_lease()

        lease.record_usage(10, NOW)

        assert lease.state is LeaseState.ACTIVE

    def test_an_empty_lease_stays_reserved(self) -> None:
        lease = a_lease()

        lease.record_usage(0, NOW)

        assert lease.state is LeaseState.RESERVED

    def test_release_then_delete(self) -> None:
        lease = a_lease()

        lease.record_usage(10, NOW)
        lease.begin_release(NOW)
        releasing = lease.state

        lease.mark_deleted(NOW)
        deleted = lease.state

        assert releasing is LeaseState.RELEASING
        assert deleted is LeaseState.DELETED
        assert deleted.is_terminal
        assert lease.used_bytes == 0

    def test_an_orphan_can_only_be_deleted(self) -> None:
        lease = a_lease()
        lease.mark_orphaned(NOW)

        with pytest.raises(InvalidStateTransitionError):
            lease.record_usage(10, NOW)

        lease.mark_deleted(NOW)
        assert lease.state is LeaseState.DELETED

    def test_a_deleted_lease_cannot_come_back(self) -> None:
        lease = a_lease()
        lease.begin_release(NOW)
        lease.mark_deleted(NOW)

        with pytest.raises(InvalidStateTransitionError):
            lease.begin_release(NOW)

    def test_refuses_negative_accounting(self) -> None:
        with pytest.raises(InvariantViolationError):
            a_lease(reserved_bytes=-1)

        with pytest.raises(InvariantViolationError):
            a_lease().record_usage(-1, NOW)


class TestAccounting:
    def test_only_the_unwritten_part_is_still_promised(self) -> None:
        lease = a_lease(reserved_bytes=1_000)

        lease.record_usage(400, NOW)

        assert lease.outstanding_bytes == 600

    def test_overshooting_the_reservation_promises_nothing_further(self) -> None:
        lease = a_lease(reserved_bytes=100)

        lease.record_usage(500, NOW)

        assert lease.outstanding_bytes == 0


class TestExpiry:
    def test_activity_defers_expiry(self) -> None:
        lease = a_lease()
        later = NOW + timedelta(seconds=90)

        lease.record_usage(10, later)

        assert not lease.has_expired(later + timedelta(seconds=30), ttl_seconds=60)

    def test_a_quiet_lease_expires(self) -> None:
        lease = a_lease()

        assert lease.has_expired(NOW + timedelta(seconds=61), ttl_seconds=60)

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(InvariantViolationError):
            a_lease(created_at=datetime(2026, 1, 1, 12, 0, 0))  # noqa: DTZ001


class TestAdoption:
    def test_a_surviving_lease_can_be_handed_over(self) -> None:
        lease = a_lease()
        lease.record_usage(10, NOW)
        successor = LeaseOwner(identity="pi:worker:0", process_id=200)

        lease.adopt(successor, NOW)

        assert lease.owner == successor
        assert lease.state is LeaseState.ACTIVE

    def test_a_lease_being_torn_down_is_never_adopted(self) -> None:
        lease = a_lease()
        lease.begin_release(NOW)

        with pytest.raises(InvalidStateTransitionError):
            lease.adopt(LeaseOwner(identity="pi:worker:0", process_id=200), NOW)

    def test_repr_names_the_state_and_the_owner(self) -> None:
        assert "reserved" in repr(a_lease())
        assert "pi:worker:0#100" in repr(a_lease())
