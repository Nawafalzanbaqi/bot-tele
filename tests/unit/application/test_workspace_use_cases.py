"""Recovery decides with the policy and never lets one bad lease stop a sweep."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mediahub.application.workspace.dto import RecoveryReport
from mediahub.application.workspace.ports import LeaseRecord
from mediahub.application.workspace.use_cases.recover_workspaces import RecoverWorkspaces
from mediahub.domain.workspace.entities import WorkspaceLease
from mediahub.domain.workspace.identifiers import LeaseId
from mediahub.domain.workspace.policies import RecoveryPolicy
from mediahub.domain.workspace.value_objects import LeaseOwner

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
OURS = LeaseOwner(identity="pi:worker:0", process_id=100)
OUR_PREVIOUS_RUN = LeaseOwner(identity="pi:worker:0", process_id=99)
SOMEBODY_ELSE = LeaseOwner(identity="pi:worker:1", process_id=101)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class FakeWorkspace:
    """Just enough of the port to drive the sweep."""

    def __init__(
        self, records: Sequence[LeaseRecord], *, undeletable: frozenset[str] = frozenset()
    ) -> None:
        self._records = tuple(records)
        self._undeletable = undeletable
        self.discarded: list[str] = []

    def leases_on_disk(self) -> Sequence[LeaseRecord]:
        return self._records

    def discard(self, lease_id: str) -> int:
        if lease_id in self._undeletable:
            message = "device or resource busy"
            raise OSError(message)
        self.discarded.append(lease_id)
        return 100


def a_record(
    identifier: str,
    owner: LeaseOwner | None,
    *,
    used: int = 10,
    age_seconds: float = 5.0,
) -> LeaseRecord:
    lease: WorkspaceLease | None = None
    if owner is not None:
        lease = WorkspaceLease(
            lease_id=LeaseId(identifier),
            owner=owner,
            label="acquire",
            created_at=NOW,
            reserved_bytes=1_000,
        )
        if used:
            lease.record_usage(used, NOW)
    return LeaseRecord(
        lease_id=identifier,
        lease=lease,
        used_bytes=used,
        age_seconds=age_seconds,
    )


def an_id(suffix: str) -> str:
    return suffix.rjust(32, "0")


def sweep(
    records: Sequence[LeaseRecord],
    *,
    policy: RecoveryPolicy | None = None,
    undeletable: frozenset[str] = frozenset(),
) -> tuple[FakeWorkspace, RecoveryReport]:
    workspace = FakeWorkspace(records, undeletable=undeletable)
    report = RecoverWorkspaces(
        workspace=workspace,
        policy=policy or RecoveryPolicy(),
        owner=OURS,
        clock=FrozenClock(),
    ).execute()
    return workspace, report


class TestRecoverWorkspaces:
    def test_an_empty_root_reports_nothing_to_do(self) -> None:
        _, report = sweep([])

        assert report.examined == 0
        assert not report.changed_anything

    def test_leases_from_a_previous_run_are_reclaimed(self) -> None:
        workspace, report = sweep([a_record(an_id("1"), OUR_PREVIOUS_RUN)])

        assert workspace.discarded == [an_id("1")]
        assert report.deleted == (an_id("1"),)
        assert report.reclaimed_bytes == 100

    def test_our_own_live_lease_is_left_alone(self) -> None:
        workspace, report = sweep([a_record(an_id("2"), OURS)])

        assert workspace.discarded == []
        assert report.left == (an_id("2"),)
        assert not report.changed_anything

    def test_another_workers_lease_is_left_alone(self) -> None:
        workspace, report = sweep([a_record(an_id("3"), SOMEBODY_ELSE)])

        assert workspace.discarded == []
        assert report.left == (an_id("3"),)

    def test_adoption_keeps_a_recoverable_lease(self) -> None:
        workspace, report = sweep(
            [a_record(an_id("4"), OUR_PREVIOUS_RUN)],
            policy=RecoveryPolicy(adopt_own_leases=True),
        )

        assert workspace.discarded == []
        assert report.adopted == (an_id("4"),)
        assert report.changed_anything

    def test_purge_mode_reclaims_everything_but_our_own_live_leases(self) -> None:
        workspace, report = sweep(
            [
                a_record(an_id("5"), OUR_PREVIOUS_RUN),
                a_record(an_id("6"), SOMEBODY_ELSE),
                a_record(an_id("7"), OURS),
                a_record("debris", None),
            ],
            policy=RecoveryPolicy(delete_every_lease=True),
        )

        assert sorted(workspace.discarded) == sorted([an_id("5"), an_id("6"), "debris"])
        assert report.left == (an_id("7"),)
        assert report.examined == 4

    def test_a_directory_with_no_manifest_waits_out_the_lease_period(self) -> None:
        workspace, report = sweep(
            [a_record("debris", None, age_seconds=30)],
            policy=RecoveryPolicy(lease_expiry_seconds=60),
        )

        assert workspace.discarded == []
        assert report.left == ("debris",)

    def test_old_debris_is_reclaimed(self) -> None:
        workspace, _ = sweep(
            [a_record("debris", None, age_seconds=61)],
            policy=RecoveryPolicy(lease_expiry_seconds=60),
        )

        assert workspace.discarded == ["debris"]

    def test_one_undeletable_lease_does_not_stop_the_sweep(self) -> None:
        workspace, report = sweep(
            [
                a_record(an_id("8"), OUR_PREVIOUS_RUN),
                a_record(an_id("9"), OUR_PREVIOUS_RUN),
            ],
            undeletable=frozenset({an_id("8")}),
        )

        assert workspace.discarded == [an_id("9")]
        assert report.failed == (an_id("8"),)
        assert report.deleted == (an_id("9"),)
        assert report.reclaimed_bytes == 100

    def test_the_sweep_is_idempotent(self) -> None:
        records = [a_record(an_id("a"), OUR_PREVIOUS_RUN)]
        workspace = FakeWorkspace(records)
        use_case = RecoverWorkspaces(
            workspace=workspace,
            policy=RecoveryPolicy(),
            owner=OURS,
            clock=FrozenClock(),
        )

        first = use_case.execute()
        second = use_case.execute()

        assert first.deleted == second.deleted
