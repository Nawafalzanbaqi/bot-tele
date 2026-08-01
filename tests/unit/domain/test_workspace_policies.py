"""Disk admission and crash recovery, decided without a disk or a crash.

Both policies are pure, so the interesting cases - a device at 4% headroom, a
lease left by a process that no longer exists - are three lines each instead of
an afternoon with a virtual machine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm
from mediahub.domain.workspace.entities import WorkspaceLease
from mediahub.domain.workspace.enums import DiskState, RecoveryAction
from mediahub.domain.workspace.errors import IntegrityCheckFailedError
from mediahub.domain.workspace.identifiers import LeaseId
from mediahub.domain.workspace.policies import DiskPolicy, RecoveryPolicy
from mediahub.domain.workspace.value_objects import (
    DiskBudget,
    IntegrityExpectation,
    LeaseOwner,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
GIGABYTE = 1024**3
OURS = LeaseOwner(identity="pi:worker:0", process_id=100)
OUR_PREVIOUS_RUN = LeaseOwner(identity="pi:worker:0", process_id=99)
SOMEBODY_ELSE = LeaseOwner(identity="pi:worker:1", process_id=101)

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def a_budget(*, free: int, reserved: int = 0, emergency: int = 0) -> DiskBudget:
    return DiskBudget(
        capacity_bytes=100 * GIGABYTE,
        free_bytes=free,
        reserved_bytes=reserved,
        emergency_bytes=emergency,
    )


def a_lease(owner: LeaseOwner, *, created_at: datetime = NOW, used: int = 10) -> WorkspaceLease:
    lease = WorkspaceLease(
        lease_id=LeaseId("0" * 31 + "1"),
        owner=owner,
        label="acquire",
        created_at=created_at,
        reserved_bytes=1_000,
    )
    if used:
        lease.record_usage(used, created_at)
    return lease


class TestDiskBudget:
    def test_headroom_excludes_reservations_and_the_floor(self) -> None:
        budget = a_budget(free=10 * GIGABYTE, reserved=3 * GIGABYTE, emergency=GIGABYTE)

        assert budget.headroom_bytes == 6 * GIGABYTE

    def test_headroom_never_goes_negative(self) -> None:
        assert a_budget(free=1, emergency=GIGABYTE).headroom_bytes == 0

    def test_an_unknown_capacity_reports_no_headroom_ratio(self) -> None:
        budget = DiskBudget(capacity_bytes=0, free_bytes=10)

        assert budget.headroom_ratio == 0.0

    def test_reserving_reduces_the_projected_headroom(self) -> None:
        budget = a_budget(free=10 * GIGABYTE)

        assert budget.after_reserving(4 * GIGABYTE).headroom_bytes == 6 * GIGABYTE

    def test_refuses_negative_quantities(self) -> None:
        with pytest.raises(Exception, match="must not be negative"):
            DiskBudget(capacity_bytes=1, free_bytes=-1)


class TestDiskPolicy:
    @pytest.mark.parametrize(
        ("free_gigabytes", "expected"),
        [
            (50, DiskState.HEALTHY),
            (20, DiskState.TIGHT),
            (7, DiskState.LOW),
            (2, DiskState.CRITICAL),
        ],
    )
    def test_thresholds_follow_the_storage_strategy(
        self, free_gigabytes: int, expected: DiskState
    ) -> None:
        budget = a_budget(free=free_gigabytes * GIGABYTE)

        assert DiskPolicy().state_of(budget) is expected

    def test_a_healthy_device_admits_everything_that_fits(self) -> None:
        budget = a_budget(free=50 * GIGABYTE)

        assert DiskPolicy().admits(budget, 49 * GIGABYTE)
        assert not DiskPolicy().admits(budget, 51 * GIGABYTE)

    def test_a_tight_device_admits_only_small_requests(self) -> None:
        budget = a_budget(free=20 * GIGABYTE)

        assert DiskPolicy().admits(budget, 4 * GIGABYTE)
        assert not DiskPolicy().admits(budget, 10 * GIGABYTE)

    @pytest.mark.parametrize("free_gigabytes", [7, 2])
    def test_a_low_or_critical_device_admits_nothing(self, free_gigabytes: int) -> None:
        budget = a_budget(free=free_gigabytes * GIGABYTE)

        assert DiskPolicy().largest_admissible(budget) == 0
        assert not DiskPolicy().admits(budget, 1)

    def test_reservations_push_a_healthy_device_into_trouble(self) -> None:
        # The whole point of accounting: free space alone would still say yes.
        budget = a_budget(free=50 * GIGABYTE, reserved=48 * GIGABYTE)

        assert DiskPolicy().state_of(budget) is DiskState.CRITICAL


class TestRecoveryPolicy:
    def test_our_own_live_lease_is_never_touched(self) -> None:
        action = RecoveryPolicy().decide(a_lease(OURS), now=NOW, owner=OURS)

        assert action is RecoveryAction.LEAVE

    def test_a_lease_from_our_previous_run_is_reclaimed(self) -> None:
        action = RecoveryPolicy().decide(a_lease(OUR_PREVIOUS_RUN), now=NOW, owner=OURS)

        assert action is RecoveryAction.DELETE

    def test_a_lease_from_our_previous_run_can_be_adopted_instead(self) -> None:
        policy = RecoveryPolicy(adopt_own_leases=True)

        action = policy.decide(a_lease(OUR_PREVIOUS_RUN), now=NOW, owner=OURS)

        assert action is RecoveryAction.ADOPT

    def test_an_empty_lease_of_ours_is_not_worth_adopting(self) -> None:
        policy = RecoveryPolicy(adopt_own_leases=True)
        lease = a_lease(OUR_PREVIOUS_RUN, used=0)
        lease.begin_release(NOW)

        assert policy.decide(lease, now=NOW, owner=OURS) is RecoveryAction.DELETE

    def test_another_workers_fresh_lease_is_left_alone(self) -> None:
        action = RecoveryPolicy().decide(a_lease(SOMEBODY_ELSE), now=NOW, owner=OURS)

        assert action is RecoveryAction.LEAVE

    def test_another_workers_abandoned_lease_is_reclaimed(self) -> None:
        policy = RecoveryPolicy(lease_expiry_seconds=60)
        lease = a_lease(SOMEBODY_ELSE)

        action = policy.decide(lease, now=NOW + timedelta(seconds=61), owner=OURS)

        assert action is RecoveryAction.DELETE

    def test_purge_mode_deletes_everything_except_our_live_leases(self) -> None:
        policy = RecoveryPolicy(delete_every_lease=True)

        assert policy.decide(a_lease(SOMEBODY_ELSE), now=NOW, owner=OURS) is RecoveryAction.DELETE
        assert policy.decide(a_lease(OURS), now=NOW, owner=OURS) is RecoveryAction.LEAVE

    def test_a_directory_nobody_claims_waits_out_the_lease_period(self) -> None:
        policy = RecoveryPolicy(lease_expiry_seconds=60)

        assert policy.decide_unclaimed(age_seconds=30) is RecoveryAction.LEAVE
        assert policy.decide_unclaimed(age_seconds=61) is RecoveryAction.DELETE

    def test_purge_mode_does_not_wait(self) -> None:
        policy = RecoveryPolicy(delete_every_lease=True)

        assert policy.decide_unclaimed(age_seconds=0) is RecoveryAction.DELETE


class TestIntegrityExpectation:
    def test_nothing_expected_is_not_a_failure(self) -> None:
        expectation = IntegrityExpectation()

        assert expectation.is_empty
        expectation.check("a.mp4", actual_bytes=10)

    def test_a_matching_size_passes(self) -> None:
        IntegrityExpectation(expected_bytes=10).check("a.mp4", actual_bytes=10)

    def test_a_truncated_download_is_caught(self) -> None:
        with pytest.raises(IntegrityCheckFailedError) as excinfo:
            IntegrityExpectation(expected_bytes=10).check("a.mp4", actual_bytes=9)

        assert excinfo.value.reason == "size mismatch"
        assert excinfo.value.code == "integrity_check_failed"

    def test_a_matching_digest_passes(self) -> None:
        fingerprint = Fingerprint(algorithm=HashAlgorithm.SHA256, digest=DIGEST)

        IntegrityExpectation(expected_fingerprint=fingerprint).check(
            "a.mp4", actual_bytes=10, actual_fingerprint=fingerprint
        )

    def test_corrupt_content_is_caught(self) -> None:
        expectation = IntegrityExpectation(
            expected_fingerprint=Fingerprint(algorithm=HashAlgorithm.SHA256, digest=DIGEST)
        )

        with pytest.raises(IntegrityCheckFailedError) as excinfo:
            expectation.check(
                "a.mp4",
                actual_bytes=10,
                actual_fingerprint=Fingerprint(algorithm=HashAlgorithm.SHA256, digest=OTHER_DIGEST),
            )

        assert excinfo.value.reason == "digest mismatch"

    def test_a_required_digest_that_was_never_computed_fails(self) -> None:
        expectation = IntegrityExpectation(
            expected_fingerprint=Fingerprint(algorithm=HashAlgorithm.SHA256, digest=DIGEST)
        )

        with pytest.raises(IntegrityCheckFailedError):
            expectation.check("a.mp4", actual_bytes=10)

    def test_size_is_checked_before_the_digest(self) -> None:
        # Cheap check first: catching a truncated file must not cost a full hash.
        expectation = IntegrityExpectation(
            expected_bytes=10,
            expected_fingerprint=Fingerprint(algorithm=HashAlgorithm.SHA256, digest=DIGEST),
        )

        with pytest.raises(IntegrityCheckFailedError) as excinfo:
            expectation.check("a.mp4", actual_bytes=9)

        assert excinfo.value.reason == "size mismatch"

    def test_refuses_a_negative_expected_size(self) -> None:
        with pytest.raises(Exception, match="must not be negative"):
            IntegrityExpectation(expected_bytes=-1)
