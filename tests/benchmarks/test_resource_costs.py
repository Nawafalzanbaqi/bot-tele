"""What the system costs: memory, CPU, disk, hashing, throughput, recovery.

Each measurement below answers a question an operator has to be able to answer
before leaving a device unattended for a year:

* **Memory** - does anything grow with the number of jobs run? A leak of a
  kilobyte per job is invisible for a week and fatal for a year.
* **Descriptors** - same question, asked of the resource that runs out first and
  most confusingly, because exhausting it breaks everything at once.
* **Hashing and disk** - integrity costs one pass over the file. How much?
* **Throughput** - what does the pipeline itself add per job, on top of the
  transfer?
* **Recovery and cleanup** - how long between a crash and the work resuming, and
  between a job ending and its bytes coming back?

Measured with the standard library only: ``tracemalloc`` for allocation,
``psutil`` where it happens to be installed and a descriptor probe otherwise,
and ``perf_counter`` for time.
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import os
import time
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is an optional convenience
    psutil = None

import pytest

from mediahub.application.download.errors import ProviderError
from mediahub.application.download.queue import JobStage, StageProgress
from mediahub.domain.download.enums import JobStatus
from mediahub.infrastructure.delivery.shared.measured_reader import MeasuredReader
from mediahub.infrastructure.download.ytdlp.downloader import engine_thread_stats
from mediahub.infrastructure.workspace import manifest
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.infrastructure.workspace.hashing import StreamingDigest, digest_of
from mediahub.presentation.worker.config import WorkerTimings
from mediahub.presentation.worker.progress import ProgressRegistry
from tests.conftest import FrozenClock
from tests.support.pipeline_fakes import PipelineHarness
from tests.support.worker_fakes import OTHER_WORKER

if TYPE_CHECKING:
    pass

pytestmark = [pytest.mark.benchmark, pytest.mark.integration]

MEGABYTE = 1024 * 1024
INTERRUPTED = "interrupted"
LEASE_SECONDS = 20.0
FAST = WorkerTimings(
    lease_seconds=LEASE_SECONDS,
    heartbeat_seconds=5.0,
    idle_poll_seconds=0.01,
    max_idle_poll_seconds=0.02,
    progress_interval_seconds=0.01,
)


def open_descriptors() -> int:
    """Return how many file descriptors this process holds.

    ``psutil`` when it is there, ``/proc`` when it is not, and a probe as the
    last resort - opening a file and reading back the number the kernel handed
    out is a decent proxy for "how many are in use" on any platform.
    """
    if psutil is not None:
        with contextlib.suppress(AttributeError, OSError):
            return int(psutil.Process().num_fds())
    with contextlib.suppress(OSError):
        return len(tuple(Path(f"/proc/{os.getpid()}/fd").iterdir()))
    # Last resort: the number the kernel hands out is itself a decent proxy for
    # how many are already in use.
    with Path(os.devnull).open() as probe:
        return probe.fileno()


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    pipeline = PipelineHarness.build(tmp_path)
    pipeline.worker.timings = FAST
    return pipeline


class TestMemory:
    async def test_running_many_jobs_does_not_grow_the_heap(self, harness: PipelineHarness) -> None:
        # The measurement that decides whether the process can run for a year.
        # Everything a job allocates - the lease, the progress registry, the
        # heartbeat, the pipeline state - has to be released with the job.
        loop = harness.loop()
        for _ in range(5):  # warm every lazy import and cache first
            await harness.worker.enqueue()
            await loop.run_once()

        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()

        for _ in range(40):
            await harness.worker.enqueue()
            await loop.run_once()

        gc.collect()
        after = tracemalloc.take_snapshot()
        tracemalloc.stop()
        grown = sum(entry.size_diff for entry in after.compare_to(baseline, "filename"))

        # The queue and journal legitimately retain a record per job, so this is
        # a ceiling on *unbounded* growth rather than an assertion of zero.
        assert grown < 4 * MEGABYTE, f"40 jobs grew the heap by {grown / MEGABYTE:.1f} MiB"

    async def test_progress_observations_are_coalesced_not_accumulated(self) -> None:
        # A download engine reports per chunk. Holding them would turn a long
        # transfer into a memory leak that looks like a slow one.
        registry = ProgressRegistry(clock=FrozenClock(), min_interval_seconds=3600.0)

        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()

        for index in range(50_000):
            registry.observe(
                StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=index, total_bytes=10**9)
            )

        gc.collect()
        after = tracemalloc.take_snapshot()
        tracemalloc.stop()
        grown = sum(entry.size_diff for entry in after.compare_to(baseline, "filename"))

        assert grown < MEGABYTE, f"50k observations retained {grown / 1024:.0f} KiB"
        assert registry.pending is not None, "the newest one is still there"

    def test_hashing_a_large_file_is_bounded_by_the_chunk_not_the_file(
        self, tmp_path: Path
    ) -> None:
        # The whole reason the digest is streamed. Reading a 2 GB download into
        # memory to hash it would OOM the device it is meant to run on.
        payload = tmp_path / "large.bin"
        payload.write_bytes(b"x" * (16 * MEGABYTE))

        gc.collect()
        tracemalloc.start()
        digest_of(payload)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        assert peak < 8 * MEGABYTE, f"hashing 16 MiB peaked at {peak / MEGABYTE:.1f} MiB"


class TestDescriptors:
    async def test_running_many_jobs_leaks_no_descriptors(self, harness: PipelineHarness) -> None:
        # Descriptor exhaustion is the worst failure mode this system has: it
        # arrives all at once, months in, and breaks the logging that would
        # explain it.
        loop = harness.loop()
        for _ in range(3):
            await harness.worker.enqueue()
            await loop.run_once()

        gc.collect()
        before = open_descriptors()

        for _ in range(30):
            await harness.worker.enqueue()
            await loop.run_once()

        gc.collect()
        after = open_descriptors()

        assert after - before <= 5, f"30 jobs leaked {after - before} descriptors"

    def test_a_lease_closes_every_handle_it_opened(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        gc.collect()
        before = open_descriptors()

        for _ in range(50):
            with (
                workspace.lease(label="job") as scope,
                scope.open_artifact(extension="bin") as writer,
            ):
                writer.write(b"x" * 4096)

        gc.collect()
        after = open_descriptors()

        assert after - before <= 5, f"50 leases leaked {after - before} descriptors"

    def test_an_aborted_write_closes_its_handle_too(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        gc.collect()
        before = open_descriptors()

        for _ in range(50):
            with (  # noqa: PT012 - the whole block is what leaks a descriptor or does not
                pytest.raises(RuntimeError, match=INTERRUPTED),
                workspace.lease(label="job") as scope,
                scope.open_artifact(extension="bin") as writer,
            ):
                writer.write(b"x" * 4096)
                raise RuntimeError(INTERRUPTED)

        gc.collect()
        after = open_descriptors()

        assert after - before <= 5, f"50 aborted writes leaked {after - before} descriptors"

    def test_the_engine_thread_ledger_starts_clean(self) -> None:
        stats = engine_thread_stats()

        assert not stats.is_leaking, (
            f"{stats.abandoned} engine threads were abandoned during this test session; "
            "each one holds a pool slot and its descriptors for the life of the process"
        )


class TestHashing:
    def test_streaming_a_digest_matches_reading_the_file_back(self, tmp_path: Path) -> None:
        payload = b"mediahub" * 100_000
        path = tmp_path / "payload.bin"
        path.write_bytes(payload)

        streamed = StreamingDigest()
        for offset in range(0, len(payload), 65_536):
            streamed.update(payload[offset : offset + 65_536])

        assert streamed.fingerprint() == digest_of(path)
        assert streamed.fingerprint().digest == hashlib.sha256(payload).hexdigest()

    def test_hashing_throughput_is_worth_measuring(self, tmp_path: Path) -> None:
        # Verification costs one pass over the artifact. On an SD card that pass
        # is the dominant cost of the verify stage, and it is not interruptible
        # - which is why the drain grace has to exceed it.
        payload = tmp_path / "large.bin"
        payload.write_bytes(os.urandom(8 * MEGABYTE))

        started = time.perf_counter()
        digest_of(payload)
        elapsed = time.perf_counter() - started

        rate = (8 * MEGABYTE) / max(elapsed, 1e-6) / MEGABYTE
        assert rate > 5.0, f"hashing managed only {rate:.1f} MiB/s"

    def test_a_streamed_digest_costs_nothing_extra_to_read(self) -> None:
        # Snapshotting rather than finalising, so progress can ask mid-stream.
        digest = StreamingDigest()
        digest.update(b"x" * MEGABYTE)

        started = time.perf_counter()
        for _ in range(100):
            digest.fingerprint()
        elapsed = time.perf_counter() - started

        assert elapsed < 1.0, f"100 mid-stream snapshots took {elapsed:.2f}s"


class TestDiskIo:
    def test_opening_and_releasing_a_lease_is_cheap(self, tmp_path: Path) -> None:
        # Every job pays this twice. It includes two fsyncs on the manifest,
        # which is the durability the recovery path depends on.
        workspace = FilesystemWorkspace(tmp_path / "workspace")

        started = time.perf_counter()
        for _ in range(50):
            with workspace.lease(label="job"):
                pass
        elapsed = time.perf_counter() - started

        assert elapsed < 10.0, f"50 lease cycles took {elapsed:.2f}s"
        assert list((tmp_path / "workspace").iterdir()) == []

    def test_a_durable_manifest_write_is_not_free_but_is_bounded(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            directory = scope.directory().parent
            lease = manifest.read(directory)
            assert lease is not None

            started = time.perf_counter()
            for _ in range(50):
                manifest.write(directory, lease)
            elapsed = time.perf_counter() - started

        assert elapsed < 10.0, f"50 durable manifest writes took {elapsed:.2f}s"

    def test_measuring_a_lease_does_not_walk_the_world(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            for index in range(100):
                scope.path_for(f"file{index}.bin").write_bytes(b"x" * 1024)

            started = time.perf_counter()
            for _ in range(20):
                scope.used_bytes()
            elapsed = time.perf_counter() - started

        assert elapsed < 5.0, f"20 size measurements over 100 files took {elapsed:.2f}s"


class TestThroughput:
    async def test_the_pipeline_adds_little_per_job(self, harness: PipelineHarness) -> None:
        # Everything except the transfer itself: claim, lease, five stages, five
        # checkpoints, delivery, settle, release.
        loop = harness.loop()
        await harness.worker.enqueue()
        await loop.run_once()  # warm

        jobs = 25
        for _ in range(jobs):
            await harness.worker.enqueue()

        started = time.perf_counter()
        for _ in range(jobs):
            await loop.run_once()
        elapsed = time.perf_counter() - started

        per_job = elapsed / jobs
        assert per_job < 0.5, f"{per_job * 1000:.0f} ms of overhead per job"

    async def test_progress_writes_are_throttled_hard(self, harness: PipelineHarness) -> None:
        # The number that decides whether the SD card survives. An engine
        # reporting per chunk must not become a durable write per chunk.
        clock = FrozenClock()
        registry = ProgressRegistry(clock=clock, min_interval_seconds=5.0, min_percent_step=5.0)

        writes = 0
        for index in range(10_000):
            registry.observe(
                StageProgress(stage=JobStage.DOWNLOAD, transferred_bytes=index, total_bytes=10_000)
            )
            if registry.take_due() is not None:
                writes += 1

        assert writes <= 25, f"{writes} durable writes for 10,000 observations"

    def test_the_measured_reader_adds_little_over_a_plain_read(self, tmp_path: Path) -> None:
        payload = tmp_path / "payload.bin"
        payload.write_bytes(os.urandom(4 * MEGABYTE))

        started = time.perf_counter()
        with payload.open("rb") as handle:
            while handle.read(MEGABYTE):
                pass
        plain = time.perf_counter() - started

        started = time.perf_counter()
        with payload.open("rb") as handle:
            reader = MeasuredReader(handle, total_bytes=4 * MEGABYTE)
            while reader.read(MEGABYTE):
                pass
        measured = time.perf_counter() - started

        # Hashing dominates, and it replaces a second pass that would otherwise
        # be needed anyway.
        assert measured < plain + 5.0, f"measuring cost {measured - plain:.2f}s extra"


class TestRecoverySpeed:
    async def test_recovering_a_crashed_job_is_a_single_sweep(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.handler(JobStage.DOWNLOAD).error = _Death()
        with pytest.raises(_Death):
            await harness.loop().run_once()
        harness.handler(JobStage.DOWNLOAD).error = None
        harness.worker.clock.advance(int(LEASE_SECONDS) + 1)

        started = time.perf_counter()
        recovery = await harness.worker.services.recover_leases.execute()
        elapsed = time.perf_counter() - started

        assert recovery.reclaimed == (job_id.value,)
        assert elapsed < 1.0, f"the sweep took {elapsed:.2f}s"

    async def test_a_sweep_over_many_jobs_stays_quick(self, harness: PipelineHarness) -> None:
        for _ in range(50):
            job_id = await harness.worker.enqueue()
            await harness.worker.services.claim.execute(worker=OTHER_WORKER)
            del job_id
        harness.worker.clock.advance(int(LEASE_SECONDS) + 1)

        started = time.perf_counter()
        recovery = await harness.worker.services.recover_leases.execute()
        elapsed = time.perf_counter() - started

        assert recovery.total >= 1
        assert elapsed < 5.0, f"sweeping 50 jobs took {elapsed:.2f}s"

    async def test_a_restarted_worker_recovers_without_waiting_for_the_lease(
        self, harness: PipelineHarness
    ) -> None:
        # The difference between a restart costing a poll interval and costing a
        # full lease period, which on the default settings is two minutes of
        # dead time per restart.
        job_id = await harness.worker.enqueue()
        await harness.worker.services.claim.execute(worker=harness.runtime().worker_id)

        recovery = await harness.runtime().start()

        assert recovery.reclaimed == (job_id.value,)
        assert (await harness.worker.job(job_id)).status is JobStatus.QUEUED


class TestCleanupLatency:
    async def test_a_finished_job_gives_its_bytes_back_immediately(
        self, harness: PipelineHarness
    ) -> None:
        harness.downloader.size_bytes = 2 * MEGABYTE
        await harness.worker.enqueue()

        await harness.loop().run_once()

        assert harness.worker.workspace_directories() == []
        assert harness.worker.workspace.usage().used_bytes == 0

    def test_releasing_a_large_lease_is_prompt(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job") as scope:
            for index in range(200):
                scope.path_for(f"file{index}.bin").write_bytes(b"x" * (32 * 1024))

            started = time.perf_counter()
            reclaimed = scope.close()
            elapsed = time.perf_counter() - started

        assert reclaimed > 0
        assert elapsed < 5.0, f"releasing 200 files took {elapsed:.2f}s"

    async def test_cleanup_happens_even_when_the_job_failed(self, harness: PipelineHarness) -> None:
        harness.downloader.size_bytes = MEGABYTE
        harness.downloader.fetch_error = ProviderError("connection reset")
        await harness.worker.enqueue()

        await harness.loop().run_once()

        assert harness.worker.workspace.usage().used_bytes == 0
        assert not harness.worker.workspace.usage().is_leaking


class _Death(BaseException):
    """A crash that gets past the worker's safety nets."""
