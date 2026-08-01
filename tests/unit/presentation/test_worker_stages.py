"""Each stage of the acquisition pipeline, driven one at a time.

Every test here runs a real handler over a real workspace lease. What is faked
is the engine and the destination, which is where the network would be.

Two behaviours recur and are the point of the design:

* a stage asked to do something already done returns without doing it again;
* a stage reached without its inputs rebuilds them, because a resumed job gets a
  brand-new lease and the previous attempt's bytes are gone rather than stale.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mediahub.application.common.cancellation import CancellationSource
from mediahub.application.delivery.errors import ArtifactTooLargeError
from mediahub.application.delivery.ports import DeliveryKind
from mediahub.application.download.errors import (
    DownloadCancelledError,
    LocalCopyNotReleasableError,
    MetadataUnavailableError,
    ProviderError,
)
from mediahub.application.download.queue import JobStage
from mediahub.domain.media.enums import MediaType
from mediahub.domain.workspace.errors import IntegrityCheckFailedError
from mediahub.presentation.worker.stages.state import PipelineState
from tests.support.delivery_fakes import FakeDeliveryProvider
from tests.support.download_fakes import RESUME_MARKER, FakeDownloader
from tests.support.pipeline_fakes import PRINCIPAL, PipelineHarness

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.unit


@pytest.fixture
def harness(tmp_path: Path) -> PipelineHarness:
    return PipelineHarness.build(tmp_path)


class TestProbeStage:
    async def test_it_records_what_the_source_is(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.PROBE, job_id=job_id, workspace=scope)

        assert state.provider == "fakesite"
        assert state.title == "A Test Video"
        assert state.media_kind is MediaType.VIDEO
        assert state.duration_seconds == 125
        assert state.is_probed is True

    async def test_it_reads_the_url_from_the_job_not_from_the_queue(
        self, harness: PipelineHarness
    ) -> None:
        # The runtime carries scheduling mechanics only; a payload duplicated
        # into it would be a second, staler copy of the job.
        job_id = await harness.worker.enqueue()
        job = await harness.worker.job(job_id)

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.PROBE, job_id=job_id, workspace=scope)

        assert state.url == str(job.source_url)

    async def test_a_queued_job_is_acquired_at_the_best_available_quality(
        self, harness: PipelineHarness
    ) -> None:
        # Nobody was looking at a list of buttons when the job was enqueued.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.PROBE, job_id=job_id, workspace=scope)

        assert state.quality_key == "best"
        assert state.quality_audio_only is False

    async def test_a_source_offering_nothing_recognisable_still_gets_a_choice(
        self, tmp_path: Path
    ) -> None:
        # Refusing before anything has been attempted is worse than letting the
        # engine make its own selection.
        harness = PipelineHarness.build(
            tmp_path,
            downloader=FakeDownloader(offers_video=False, offers_audio=False, expected_bytes=None),
        )
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.PROBE, job_id=job_id, workspace=scope)

        assert state.quality_key == "best"

    async def test_it_does_not_probe_twice(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            first = await harness.run_stage(JobStage.PROBE, job_id=job_id, workspace=scope)
            await harness.run_stage(JobStage.PROBE, job_id=job_id, workspace=scope, state=first)

        assert harness.downloader.probe_calls == 1

    async def test_a_source_that_cannot_be_described_fails_with_its_own_error(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.downloader.probe_error = MetadataUnavailableError("the video is private")

        with harness.lease() as scope, pytest.raises(MetadataUnavailableError):
            await harness.run_stage(JobStage.PROBE, job_id=job_id, workspace=scope)

    async def test_a_cancelled_job_does_not_call_the_provider(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        cancellation = CancellationSource()
        cancellation.cancel()

        with harness.lease() as scope, pytest.raises(DownloadCancelledError):
            await harness.run_stage(
                JobStage.PROBE, job_id=job_id, workspace=scope, cancellation=cancellation
            )

        assert harness.downloader.probe_calls == 0


class TestDownloadStage:
    async def test_it_writes_the_media_into_the_lease_it_was_given(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

            assert state.lease_id == scope.lease_id
            assert state.artifact is not None
            assert state.artifact.name in scope.names()
            assert state.artifact.size_bytes == harness.downloader.size_bytes

    async def test_it_probes_first_when_it_is_reached_without_one(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

        assert harness.downloader.probe_calls == 1
        assert state.is_probed is True

    async def test_it_does_not_download_again_into_a_lease_that_has_it(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            first = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)
            second = await harness.run_stage(
                JobStage.DOWNLOAD, job_id=job_id, workspace=scope, state=first
            )

        assert harness.downloader.fetch_calls == 1
        assert second.artifact == first.artifact

    async def test_a_new_lease_means_the_bytes_are_gone_not_stale(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as first_scope:
            state = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=first_scope)
        with harness.lease() as second_scope:
            resumed = await harness.run_stage(
                JobStage.DOWNLOAD, job_id=job_id, workspace=second_scope, state=state
            )

            assert harness.downloader.fetch_calls == 2
            assert resumed.lease_id == second_scope.lease_id
            assert resumed.artifact is not None
            assert resumed.artifact.name in second_scope.names()

    async def test_it_asks_the_engine_to_continue_a_partial_transfer(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

        assert harness.downloader.requests[0].resume is True

    async def test_the_ceiling_is_the_smaller_of_ours_and_the_destinations(
        self, tmp_path: Path
    ) -> None:
        # Downloading something the destination will certainly refuse wastes an
        # hour and a lease.
        destination = FakeDeliveryProvider(maximum_file_size=5_000_000)
        harness = PipelineHarness.build(tmp_path, destination=destination, max_item_bytes=9_000_000)
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

        assert harness.downloader.requests[0].max_bytes == 5_000_000

    async def test_progress_is_reported_under_the_running_stage(self, tmp_path: Path) -> None:
        engine = FakeDownloader(chunks=(1024, 2048, 4096))
        harness = PipelineHarness.build(tmp_path, downloader=engine)
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

        observed = harness.progress_for(JobStage.DOWNLOAD)
        assert [update.transferred_bytes for update in observed] == [1024, 2048, 4096, 4096]
        assert observed[-1].percentage == 100.0

    async def test_a_negative_estimate_never_fails_a_download(self, tmp_path: Path) -> None:
        # Engines occasionally report a negative estimate while they work one
        # out. Telemetry must never be the reason a transfer fails.
        engine = FakeDownloader(chunks=(512,), eta_seconds=-4.0, speed_bps=-1.0)
        harness = PipelineHarness.build(tmp_path, downloader=engine)
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

        assert state.artifact is not None
        assert harness.progress_for(JobStage.DOWNLOAD)[0].eta_seconds == 0.0

    async def test_a_transfer_that_fails_reports_the_engines_own_error(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()
        harness.downloader.fetch_error = ProviderError("the source reset the connection")

        with harness.lease() as scope, pytest.raises(ProviderError):
            await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)

    async def test_cancellation_stops_the_transfer(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()
        cancellation = CancellationSource()
        cancellation.cancel()

        with harness.lease() as scope, pytest.raises(DownloadCancelledError):
            await harness.run_stage(
                JobStage.DOWNLOAD, job_id=job_id, workspace=scope, cancellation=cancellation
            )

        assert harness.downloader.fetch_calls == 0
        assert scope.names() == ()


class TestVerifyStage:
    async def test_it_records_the_digest_of_what_was_downloaded(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.VERIFY, job_id=job_id, workspace=scope)

        assert state.verified is True
        assert state.artifact is not None
        assert state.artifact.digest is not None
        assert state.artifact.digest.algorithm.value == "sha256"

    async def test_it_downloads_first_when_the_lease_is_empty(
        self, harness: PipelineHarness
    ) -> None:
        # What a job resumed after a power cut looks like: a checkpoint that
        # says "downloaded" and a brand-new, empty lease.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.VERIFY, job_id=job_id, workspace=scope)

            assert harness.downloader.fetch_calls == 1
            assert state.artifact is not None
            assert state.artifact.name in scope.names()

    async def test_it_does_not_verify_twice(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            first = await harness.run_stage(JobStage.VERIFY, job_id=job_id, workspace=scope)
            second = await harness.run_stage(
                JobStage.VERIFY, job_id=job_id, workspace=scope, state=first
            )

        assert second == first
        assert harness.downloader.fetch_calls == 1

    async def test_a_truncated_artifact_is_refused(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            downloaded = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)
            assert downloaded.artifact is not None
            scope.path_for(downloaded.artifact.name).write_bytes(b"truncated")

            with pytest.raises(IntegrityCheckFailedError):
                await harness.run_stage(
                    JobStage.VERIFY, job_id=job_id, workspace=scope, state=downloaded
                )

    async def test_a_digest_that_disagrees_with_the_bytes_is_refused(
        self, harness: PipelineHarness
    ) -> None:
        # The digest the engine took from the stream is what the artifact is
        # checked against, so bytes that do not produce it are not the bytes
        # that were asked for.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            downloaded = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)
            assert downloaded.artifact is not None
            wrong = replace(downloaded.artifact, fingerprint=f"sha256:{'b' * 64}")

            with pytest.raises(IntegrityCheckFailedError, match="digest"):
                await harness.run_stage(
                    JobStage.VERIFY,
                    job_id=job_id,
                    workspace=scope,
                    state=replace(downloaded, artifact=wrong),
                )

    async def test_the_verified_size_is_reported_as_progress(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.VERIFY, job_id=job_id, workspace=scope)

        observed = harness.progress_for(JobStage.VERIFY)
        assert observed[-1].percentage == 100.0


class TestDeliveryStage:
    async def test_it_hands_the_media_to_the_destination(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)

        assert len(harness.destination.delivered) == 1
        assert state.receipt is not None
        assert state.receipt.remote_id == "fake-ref-1"
        assert state.receipt.can_serve_back is True

    async def test_it_presents_a_video_as_a_video(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)

        assert harness.destination.delivered[0].kind is DeliveryKind.VIDEO

    async def test_it_sends_the_poster_image_when_the_engine_produced_one(
        self, tmp_path: Path
    ) -> None:
        harness = PipelineHarness.build(tmp_path, downloader=FakeDownloader(thumbnail_bytes=128))
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)

        thumbnail = harness.destination.delivered[0].thumbnail
        assert thumbnail is not None
        assert thumbnail.size_bytes == 128

    async def test_a_poster_image_that_is_no_longer_there_is_not_offered(
        self, tmp_path: Path
    ) -> None:
        harness = PipelineHarness.build(tmp_path, downloader=FakeDownloader(thumbnail_bytes=128))
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            downloaded = await harness.run_stage(JobStage.DOWNLOAD, job_id=job_id, workspace=scope)
            assert downloaded.thumbnail is not None
            scope.remove(downloaded.thumbnail.name)

            await harness.run_stage(
                JobStage.DELIVER, job_id=job_id, workspace=scope, state=downloaded
            )

        assert harness.destination.delivered[0].thumbnail is None

    async def test_an_audio_only_source_is_presented_as_audio(self, tmp_path: Path) -> None:
        harness = PipelineHarness.build(
            tmp_path, downloader=FakeDownloader(kind=MediaType.AUDIO, offers_video=False)
        )
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            state = await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)

        assert state.quality_key == "audio"
        assert state.quality_audio_only is True
        assert harness.destination.delivered[0].kind is DeliveryKind.AUDIO

    async def test_it_records_the_acquisition_in_the_history(
        self, harness: PipelineHarness
    ) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)

        entries = await harness.journal.recent(PRINCIPAL)
        assert len(entries) == 1
        assert entries[0].title == "A Test Video"
        assert entries[0].remote_id == "fake-ref-1"
        assert entries[0].quality_label == "Best available"

    async def test_a_delivered_artifact_is_never_delivered_twice(
        self, harness: PipelineHarness
    ) -> None:
        # The receipt lives in the checkpoint, so a job reclaimed between the
        # upload and the write that records it sends nothing again.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            first = await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)
            second = await harness.run_stage(
                JobStage.DELIVER, job_id=job_id, workspace=scope, state=first
            )

        assert harness.destination.calls == 1
        assert second.receipt == first.receipt
        assert len(await harness.journal.recent(PRINCIPAL)) == 1

    async def test_something_too_large_is_refused_before_the_upload_starts(
        self, tmp_path: Path
    ) -> None:
        harness = PipelineHarness.build(
            tmp_path,
            downloader=FakeDownloader(size_bytes=4096),
            destination=FakeDeliveryProvider(maximum_file_size=1024),
        )
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope, pytest.raises(ArtifactTooLargeError) as failure:
            await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)

        assert failure.value.limit_bytes == 1024
        assert harness.destination.calls == 0, "no bytes were offered to the destination"

    async def test_upload_progress_is_reported(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)

        assert harness.progress_for(JobStage.DELIVER)[-1].percentage == 100.0

    async def test_a_cancelled_job_is_not_uploaded(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()
        cancellation = CancellationSource()
        cancellation.cancel()

        with harness.lease() as scope, pytest.raises(DownloadCancelledError):
            await harness.run_stage(
                JobStage.DELIVER, job_id=job_id, workspace=scope, cancellation=cancellation
            )

        assert harness.destination.calls == 0


class TestCleanupStage:
    async def test_it_empties_the_lease(self, tmp_path: Path) -> None:
        harness = PipelineHarness.build(tmp_path, downloader=FakeDownloader(thumbnail_bytes=64))
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            delivered = await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)
            assert scope.names() != ()

            state = await harness.run_stage(
                JobStage.CLEANUP, job_id=job_id, workspace=scope, state=delivered
            )

            assert scope.names() == ()
            assert state.released is True

    async def test_it_refuses_to_delete_anything_before_a_receipt_exists(
        self, harness: PipelineHarness
    ) -> None:
        # One failure mode is a leaked file a sweep will find. The other is
        # losing both copies. The order is chosen accordingly.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            verified = await harness.run_stage(JobStage.VERIFY, job_id=job_id, workspace=scope)

            with pytest.raises(LocalCopyNotReleasableError):
                await harness.run_stage(
                    JobStage.CLEANUP, job_id=job_id, workspace=scope, state=verified
                )

            assert scope.names() != (), "nothing was removed"

    async def test_releasing_twice_is_harmless(self, harness: PipelineHarness) -> None:
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            delivered = await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)
            once = await harness.run_stage(
                JobStage.CLEANUP, job_id=job_id, workspace=scope, state=delivered
            )
            twice = await harness.run_stage(
                JobStage.CLEANUP, job_id=job_id, workspace=scope, state=once
            )

        assert twice == once

    async def test_an_engines_leftovers_go_with_the_lease(self, harness: PipelineHarness) -> None:
        # Cleanup releases published artifacts; an in-flight temporary is not
        # something the port lets a caller name, and it goes when the lease
        # does. What must be true either way is that nothing survives the job.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            delivered = await harness.run_stage(JobStage.DELIVER, job_id=job_id, workspace=scope)
            (scope.directory() / RESUME_MARKER).write_bytes(b"left over")

            await harness.run_stage(
                JobStage.CLEANUP, job_id=job_id, workspace=scope, state=delivered
            )

            assert scope.names() == ()

        assert harness.worker.workspace_directories() == []


class TestTheHandlersThemselves:
    def test_every_stage_of_the_plan_has_exactly_one_handler(
        self, harness: PipelineHarness
    ) -> None:
        registry = harness.registry()

        assert registry.missing_for(harness.worker.plan) == ()
        assert len(registry.stages) == len(harness.worker.plan)

    async def test_a_handler_returns_the_whole_state_as_its_resume_token(
        self, harness: PipelineHarness
    ) -> None:
        # The checkpoint that records a stage and the state it produced are one
        # write, so they cannot disagree.
        job_id = await harness.worker.enqueue()

        with harness.lease() as scope:
            outcome = await harness.handler(JobStage.PROBE).execute(
                harness.context(JobStage.PROBE, job_id=job_id, workspace=scope)
            )

        assert outcome.resume_token is not None
        assert PipelineState.decode(outcome.resume_token).is_probed is True
