"""The progress bridge's guards, and the probe retry helper."""

from __future__ import annotations

import time

import pytest

from mediahub.application.common.cancellation import (
    CancellationReason,
    CancellationSource,
)
from mediahub.application.download.errors import (
    DownloadError,
    MetadataUnavailableError,
    ProviderError,
)
from mediahub.application.download.ports import DownloadProgress, DownloadStage
from mediahub.infrastructure.download.shared.retry import RetrySchedule, retry_async
from mediahub.infrastructure.download.ytdlp.progress import (
    EngineAbort,
    ProgressBridge,
    SizeCeilingExceeded,
)

pytestmark = pytest.mark.unit


class TestProgressBridgeGuards:
    def test_cancellation_aborts_on_the_next_tick(self) -> None:
        source = CancellationSource()
        bridge = ProgressBridge(cancellation=source.token)
        source.cancel(CancellationReason.SHUTDOWN)

        with pytest.raises(EngineAbort) as excinfo:
            bridge.on_download_hook({"status": "downloading", "downloaded_bytes": 1})

        assert excinfo.value.reason is CancellationReason.SHUTDOWN

    def test_deadline_aborts(self) -> None:
        bridge = ProgressBridge(deadline=time.monotonic() - 1)

        with pytest.raises(EngineAbort) as excinfo:
            bridge.check_guards()

        assert excinfo.value.reason is CancellationReason.TIMEOUT

    def test_ceiling_aborts_with_the_observed_size(self) -> None:
        bridge = ProgressBridge(max_bytes=100)

        with pytest.raises(SizeCeilingExceeded) as excinfo:
            bridge.on_download_hook({"status": "downloading", "downloaded_bytes": 500})

        assert excinfo.value.limit_bytes == 100
        assert excinfo.value.observed_bytes == 500

    def test_guards_run_even_without_a_callback(self) -> None:
        bridge = ProgressBridge(callback=None, max_bytes=10)

        with pytest.raises(SizeCeilingExceeded):
            bridge.on_download_hook({"status": "downloading", "downloaded_bytes": 11})

    def test_postprocessor_hook_reports_a_stage(self) -> None:
        seen: list[DownloadProgress] = []
        bridge = ProgressBridge(callback=seen.append, min_interval_seconds=0.0)

        bridge.on_postprocessor_hook({"status": "started"})

        assert seen[-1].stage is DownloadStage.POSTPROCESSING


class TestProgressBridgeThrottling:
    def test_stage_changes_are_always_emitted(self) -> None:
        seen: list[DownloadProgress] = []
        bridge = ProgressBridge(callback=seen.append, min_interval_seconds=3600)

        bridge.stage(DownloadStage.VALIDATING)
        bridge.stage(DownloadStage.DOWNLOADING)
        bridge.stage(DownloadStage.VERIFYING)

        assert [update.stage for update in seen] == [
            DownloadStage.VALIDATING,
            DownloadStage.DOWNLOADING,
            DownloadStage.VERIFYING,
        ]

    def test_chunk_updates_are_coalesced(self) -> None:
        seen: list[DownloadProgress] = []
        bridge = ProgressBridge(callback=seen.append, min_interval_seconds=3600)

        for downloaded in range(1, 200):
            bridge.on_download_hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": downloaded,
                    "total_bytes": 1000,
                }
            )

        assert len(seen) <= 1, "a chunk-rate callback would flood the consumer"

    def test_the_final_update_is_always_emitted(self) -> None:
        seen: list[DownloadProgress] = []
        bridge = ProgressBridge(callback=seen.append, min_interval_seconds=3600)

        bridge.on_download_hook(
            {"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100}
        )
        bridge.on_download_hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})

        assert seen[-1].downloaded_bytes == 100

    def test_byte_counts_never_go_backwards(self) -> None:
        seen: list[DownloadProgress] = []
        bridge = ProgressBridge(callback=seen.append, min_interval_seconds=0.0)

        bridge.on_download_hook({"status": "finished", "downloaded_bytes": 500})
        bridge.on_download_hook({"status": "finished", "downloaded_bytes": 100})

        assert [update.downloaded_bytes for update in seen] == [500, 500]

    def test_estimated_totals_are_flagged(self) -> None:
        seen: list[DownloadProgress] = []
        bridge = ProgressBridge(callback=seen.append, min_interval_seconds=0.0)

        bridge.on_download_hook(
            {
                "status": "finished",
                "downloaded_bytes": 10,
                "total_bytes_estimate": 1000.0,
            }
        )

        assert seen[-1].total_bytes == 1000
        assert seen[-1].total_is_estimate is True

    def test_hostile_hook_payloads_are_coerced(self) -> None:
        seen: list[DownloadProgress] = []
        bridge = ProgressBridge(callback=seen.append, min_interval_seconds=0.0)

        bridge.on_download_hook(
            {
                "status": "finished",
                "downloaded_bytes": "lots",
                "total_bytes": -5,
                "speed": None,
                "eta": "soon",
                "filename": 42,
            }
        )

        update = seen[-1]
        assert update.downloaded_bytes == 0
        assert update.total_bytes is None
        assert update.speed_bps is None
        assert update.filename is None


class TestRetrySchedule:
    def test_first_attempt_is_never_delayed(self) -> None:
        assert RetrySchedule().delay_for(1) == 0.0

    def test_delay_grows_and_is_capped(self) -> None:
        schedule = RetrySchedule(base_delay_seconds=10, max_delay_seconds=25, jitter_ratio=0)

        assert schedule.delay_for(2) == 10
        assert schedule.delay_for(3) == 20
        assert schedule.delay_for(4) == 25

    def test_jitter_stays_within_bounds(self) -> None:
        schedule = RetrySchedule(base_delay_seconds=10, max_delay_seconds=10, jitter_ratio=0.2)

        delays = [schedule.delay_for(2) for _ in range(50)]

        assert all(8.0 <= delay <= 12.0 for delay in delays)


class TestRetryAsync:
    async def test_returns_the_first_success(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        result = await retry_async(
            operation, schedule=RetrySchedule(attempts=3), description="probe"
        )

        assert result == "ok"
        assert calls == 1

    async def test_retries_transient_failures_then_succeeds(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                message = "flaky"
                raise ProviderError(message)
            return "ok"

        result = await retry_async(
            operation,
            schedule=RetrySchedule(attempts=3, base_delay_seconds=0.0, jitter_ratio=0.0),
            description="probe",
        )

        assert result == "ok"
        assert calls == 3

    async def test_permanent_failures_are_not_retried(self) -> None:
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            message = "gone"
            raise MetadataUnavailableError(message)

        with pytest.raises(MetadataUnavailableError):
            await retry_async(
                operation,
                schedule=RetrySchedule(attempts=5, base_delay_seconds=0.0),
                description="probe",
            )

        assert calls == 1

    async def test_the_last_failure_is_raised(self) -> None:
        async def operation() -> str:
            message = "still down"
            raise ProviderError(message)

        with pytest.raises(DownloadError) as excinfo:
            await retry_async(
                operation,
                schedule=RetrySchedule(attempts=2, base_delay_seconds=0.0, jitter_ratio=0.0),
                description="probe",
            )

        assert "still down" in excinfo.value.message

    async def test_provider_supplied_delay_is_obeyed(self) -> None:
        started = time.monotonic()
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                message = "slow down"
                raise ProviderError(message, retry_after_seconds=0.05)
            return "ok"

        await retry_async(
            operation,
            schedule=RetrySchedule(attempts=2, base_delay_seconds=10.0),
            description="probe",
        )

        elapsed = time.monotonic() - started
        assert elapsed < 1.0, "the provider's own delay must win over the schedule"
