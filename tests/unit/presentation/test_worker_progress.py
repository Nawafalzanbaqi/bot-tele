"""Progress is coalesced in memory and written sparingly.

The throttle is not a nicety. A download engine reports per chunk; writing each
one to a database on an SD card wears the card out, and the product runs on
devices where that is the failure that ends the deployment
(``docs/architecture/05-component-communication.md`` §5.9).
"""

from __future__ import annotations

import pytest

from mediahub.application.download.queue import JobStage, StageProgress
from mediahub.presentation.worker.progress import ProgressRegistry
from tests.conftest import FrozenClock

pytestmark = pytest.mark.unit


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def registry(clock: FrozenClock) -> ProgressRegistry:
    return ProgressRegistry(clock=clock, min_interval_seconds=5.0, min_percent_step=5.0)


def observation(
    *,
    transferred: int,
    total: int | None = 1000,
    stage: JobStage = JobStage.DOWNLOAD,
) -> StageProgress:
    return StageProgress(stage=stage, transferred_bytes=transferred, total_bytes=total)


class TestCoalescing:
    def test_nothing_pending_writes_nothing(self, registry: ProgressRegistry) -> None:
        assert registry.take_due() is None
        assert registry.take_final() is None

    def test_only_the_newest_observation_survives(self, registry: ProgressRegistry) -> None:
        registry.observe(observation(transferred=100))
        registry.observe(observation(transferred=200))
        registry.observe(observation(transferred=300))

        taken = registry.take_due()

        assert taken is not None
        assert taken.transferred_bytes == 300
        assert registry.pending is None

    def test_taking_clears_what_was_taken(self, registry: ProgressRegistry) -> None:
        registry.observe(observation(transferred=100))
        registry.take_due()

        assert registry.take_due() is None


class TestThrottling:
    def test_the_first_observation_is_always_written(self, registry: ProgressRegistry) -> None:
        registry.observe(observation(transferred=1))

        assert registry.take_due() is not None

    def test_a_small_step_soon_after_is_held_back(
        self, registry: ProgressRegistry, clock: FrozenClock
    ) -> None:
        registry.observe(observation(transferred=100))
        registry.take_due()

        clock.advance(1)
        registry.observe(observation(transferred=110))

        assert registry.take_due() is None
        assert registry.pending is not None

    def test_time_alone_earns_a_write(self, registry: ProgressRegistry, clock: FrozenClock) -> None:
        registry.observe(observation(transferred=100))
        registry.take_due()

        clock.advance(5)
        registry.observe(observation(transferred=101))

        assert registry.take_due() is not None

    def test_a_big_jump_earns_a_write_immediately(self, registry: ProgressRegistry) -> None:
        registry.observe(observation(transferred=100))
        registry.take_due()

        registry.observe(observation(transferred=200))

        assert registry.take_due() is not None

    def test_a_new_stage_always_earns_a_write(self, registry: ProgressRegistry) -> None:
        registry.observe(observation(transferred=1000))
        registry.take_due()

        registry.observe(observation(transferred=0, stage=JobStage.DELIVER))

        assert registry.take_due() is not None

    def test_without_a_total_only_time_can_trigger_a_write(
        self, registry: ProgressRegistry, clock: FrozenClock
    ) -> None:
        registry.observe(observation(transferred=100, total=None))
        registry.take_due()

        registry.observe(observation(transferred=10_000_000, total=None))
        assert registry.take_due() is None

        clock.advance(5)
        registry.observe(observation(transferred=20_000_000, total=None))
        assert registry.take_due() is not None


class TestFinalFlush:
    def test_the_last_observation_is_never_dropped(self, registry: ProgressRegistry) -> None:
        registry.observe(observation(transferred=100))
        registry.take_due()
        registry.observe(observation(transferred=101))

        assert registry.take_due() is None
        final = registry.take_final()

        assert final is not None
        assert final.transferred_bytes == 101

    def test_a_final_flush_still_counts_as_a_write(
        self, registry: ProgressRegistry, clock: FrozenClock
    ) -> None:
        registry.observe(observation(transferred=100))
        registry.take_final()

        clock.advance(1)
        registry.observe(observation(transferred=101))

        assert registry.take_due() is None
