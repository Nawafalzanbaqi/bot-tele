"""The bookkeeping that makes a leak visible before it is fatal.

Three small pieces, each of which exists because the thing it counts fails
silently and late:

* the **engine thread ledger**, because an abandoned thread holds a pool slot
  for the life of the process and nothing else would ever say so;
* the **descriptor discipline** in the Telegram client, because one leaked
  handle per delivery is invisible for months and then breaks everything;
* the **buffering ceiling** in the measured reader, because the alternative to
  refusing is the kernel refusing, and it does that by killing the process.
"""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.delivery.errors import ArtifactTooLargeError
from mediahub.domain.workspace.value_objects import LeaseOwner
from mediahub.infrastructure.delivery.shared.measured_reader import MeasuredReader
from mediahub.infrastructure.delivery.telegram import client as telegram_client
from mediahub.infrastructure.di.container import Container
from mediahub.infrastructure.download.ytdlp.downloader import (
    _THREADS,
    EngineThreadStats,
    engine_thread_stats,
    reset_engine_thread_stats,
)
from mediahub.infrastructure.downloader.null_downloader import NullDownloader
from mediahub.infrastructure.messaging.logging_event_publisher import LoggingEventPublisher
from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
from mediahub.infrastructure.system.clock import SystemClock
from mediahub.infrastructure.system.id_generator import Uuid4Generator
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import (
    DatabaseSettings,
    Environment,
    PersistenceBackend,
    Settings,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.unit

UPLOAD_FAILED = "upload failed"


@pytest.fixture
def clean_ledger() -> Iterator[None]:
    """Isolate the process-wide thread ledger from other tests."""
    reset_engine_thread_stats()
    yield
    reset_engine_thread_stats()


class TestEngineThreadLedger:
    def test_a_fresh_process_is_not_leaking(self, clean_ledger: None) -> None:
        stats = engine_thread_stats()

        assert stats == EngineThreadStats(running=0, abandoned=0)
        assert not stats.is_leaking

    def test_an_abandoned_thread_is_counted_and_never_forgotten(self, clean_ledger: None) -> None:
        # Never decreasing is the point: nobody knows whether an abandoned
        # thread finished, so the count is evidence rather than a gauge.
        _THREADS.started()
        _THREADS.abandoned()
        _THREADS.finished()

        stats = engine_thread_stats()
        assert stats.abandoned == 1
        assert stats.running == 0
        assert stats.is_leaking

    def test_running_threads_are_tracked_up_and_down(self, clean_ledger: None) -> None:
        _THREADS.started()
        _THREADS.started()
        assert engine_thread_stats().running == 2

        _THREADS.finished()
        assert engine_thread_stats().running == 1

    def test_the_running_count_never_goes_negative(self, clean_ledger: None) -> None:
        # A double-retire would otherwise make the ledger claim a negative
        # number of threads, which is worse than useless in a log line.

        _THREADS.finished()
        _THREADS.finished()

        assert engine_thread_stats().running == 0


class TestThumbnailDescriptor:
    def test_a_thumbnail_handle_is_closed_after_the_upload(self, tmp_path: Path) -> None:
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"jpeg")

        with telegram_client._opened(poster) as handle:
            assert handle is not None
            assert not handle.closed

        assert handle.closed, "one leaked descriptor per delivery is months to failure"

    def test_a_thumbnail_handle_is_closed_even_when_the_upload_fails(self, tmp_path: Path) -> None:
        poster = tmp_path / "poster.jpg"
        poster.write_bytes(b"jpeg")
        captured: list[io.BufferedReader] = []

        with (  # noqa: PT012 - the block, not one call, is what must still close
            pytest.raises(RuntimeError, match=UPLOAD_FAILED),
            telegram_client._opened(poster) as handle,
        ):
            assert handle is not None
            captured.append(handle)
            raise RuntimeError(UPLOAD_FAILED)

        assert captured[0].closed

    def test_no_thumbnail_yields_nothing_and_opens_nothing(self) -> None:
        with telegram_client._opened(None) as handle:
            assert handle is None


class TestBufferingCeiling:
    def test_reading_the_whole_stream_refuses_past_the_ceiling(self) -> None:
        reader = MeasuredReader(io.BytesIO(b"x" * 5000), chunk_bytes=1000, max_buffer_bytes=2000)

        with pytest.raises(ArtifactTooLargeError) as refusal:
            reader.read()

        assert refusal.value.limit_bytes == 2000

    def test_no_ceiling_means_no_refusal(self) -> None:
        reader = MeasuredReader(io.BytesIO(b"x" * 5000), chunk_bytes=1000)

        assert len(reader.read()) == 5000

    def test_readall_honours_the_ceiling_too(self) -> None:
        # `readall` is what `io.RawIOBase` dispatches to for some callers, so a
        # ceiling that only guarded `read` would be a ceiling with a hole in it.
        reader = MeasuredReader(io.BytesIO(b"x" * 5000), chunk_bytes=1000, max_buffer_bytes=2000)

        with pytest.raises(ArtifactTooLargeError):
            reader.readall()

    def test_readinto_is_bounded_by_the_caller_s_buffer(self) -> None:
        reader = MeasuredReader(io.BytesIO(b"x" * 5000), max_buffer_bytes=10)
        buffer = bytearray(100)

        assert reader.readinto(memoryview(buffer)) == 100
        assert reader.bytes_read == 100

    def test_the_refusal_names_the_provider(self) -> None:
        reader = MeasuredReader(
            io.BytesIO(b"x" * 5000),
            chunk_bytes=1000,
            max_buffer_bytes=2000,
            provider="telegram",
        )

        with pytest.raises(ArtifactTooLargeError) as refusal:
            reader.read()

        assert refusal.value.provider == "telegram"


class TestWorkspaceOrphans:
    def test_a_healthy_workspace_reports_no_orphans(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")

        with workspace.lease(label="job"):
            assert workspace.orphans() == (), "an open lease is held, not leaked"

        assert workspace.orphans() == ()
        assert not workspace.usage().is_leaking

    def test_another_process_s_lease_is_not_ours_to_call_an_orphan(self, tmp_path: Path) -> None:
        # The conservative half of the recovery rule, applied to leak detection:
        # a directory owned by a process that may still be running is somebody
        # else's live work, not our leak.

        workspace = FilesystemWorkspace(tmp_path / "workspace")
        with workspace.lease(label="job"):
            pass

        stranger = FilesystemWorkspace(
            tmp_path / "workspace", owner=LeaseOwner(identity="other-host", process_id=999)
        )

        assert stranger.orphans() == ()

    def test_usage_reports_the_orphan_count(self, tmp_path: Path) -> None:
        workspace = FilesystemWorkspace(tmp_path / "workspace")
        usage = workspace.usage()

        assert usage.orphan_leases == 0
        assert not usage.is_leaking


class TestWorkspaceReadiness:
    def test_a_missing_workspace_root_is_not_ready(self, tmp_path: Path) -> None:
        container = _container_with_workspace(_RootOnly(tmp_path / "gone"))

        assert container.check_workspace() is False

    def test_a_writable_workspace_root_is_ready(self, tmp_path: Path) -> None:
        root = tmp_path / "workspace"
        root.mkdir()

        assert _container_with_workspace(_RootOnly(root)).check_workspace() is True

    def test_no_workspace_at_all_is_ready(self) -> None:
        # An instance with the engine disabled has nothing to write and nothing
        # to be unready about.
        assert _container_with_workspace(None).check_workspace() is True


class _RootOnly:
    """The one attribute the readiness check asks a workspace for."""

    def __init__(self, root: Path) -> None:
        self.root = root


def _container_with_workspace(workspace: Any) -> Container:
    """Return a container carrying ``workspace`` and nothing else that matters."""
    container = Container(
        settings=Settings(
            _env_file=None,
            environment=Environment.TESTING,
            database=DatabaseSettings(backend=PersistenceBackend.MEMORY),
        ),
        clock=SystemClock(),
        uuid_generator=Uuid4Generator(),
        event_publisher=LoggingEventPublisher(),
        unit_of_work=InMemoryUnitOfWorkFactory(),
        downloader=NullDownloader(),
    )
    return replace(container, workspace=workspace)
