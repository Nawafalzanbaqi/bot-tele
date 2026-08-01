"""The record the pipeline carries between its stages.

Two properties are worth more than the rest and are tested hardest here:
decoding never raises, and an artifact is only ever claimed for the lease it was
actually written into.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediahub.application.delivery.ports import (
    DeliveryReceipt,
    RemoteArtifactRef,
    RemoteMessageRef,
)
from mediahub.application.download.ports import (
    DownloadResult,
    FormatPreference,
    MediaMetadata,
    SelectedFormat,
)
from mediahub.application.workspace.ports import ArtifactRef, ArtifactRole
from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm
from mediahub.domain.media.enums import MediaType
from mediahub.presentation.worker.stages.state import (
    ArtifactState,
    PipelineState,
    ReceiptState,
)

pytestmark = pytest.mark.unit

DIGEST = Fingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64)


def artifact_ref(lease_id: str = "lease-1", name: str = "clip.mp4") -> ArtifactRef:
    return ArtifactRef(
        lease_id=lease_id,
        name=name,
        size_bytes=2048,
        role=ArtifactRole.PRIMARY,
        fingerprint=DIGEST,
    )


def receipt(provider: str = "fake") -> DeliveryReceipt:
    return DeliveryReceipt(
        provider=provider,
        reference=RemoteArtifactRef(
            provider=provider,
            principal="bot-1",
            remote_id="remote-1",
            remote_unique_id="unique-1",
        ),
        message=RemoteMessageRef(provider=provider, container_id="c", message_id="7"),
        size_bytes=2048,
        delivery_time=timedelta(seconds=2),
        delivered_at=datetime(2026, 1, 1, tzinfo=UTC),
        can_serve_back=True,
    )


class TestRoundTrip:
    def test_an_empty_state_survives_encoding(self) -> None:
        assert PipelineState.decode(PipelineState().encode()) == PipelineState()

    def test_a_full_state_survives_encoding(self) -> None:
        state = (
            PipelineState()
            .with_download(_download_result(), lease_id="lease-1")
            .with_verified(artifact_ref())
            .with_receipt(receipt())
        )

        assert PipelineState.decode(state.encode()) == state

    def test_the_token_is_plain_text_a_human_can_read(self) -> None:
        token = PipelineState(url="https://example.com/a.mp4").encode()

        assert '"url":"https://example.com/a.mp4"' in token


class TestDecodingNeverRaises:
    @pytest.mark.parametrize(
        "token",
        [None, "", "not json", "[]", '"a string"', "null", "{", '{"artifact": 7}'],
    )
    def test_an_unreadable_token_starts_the_pipeline_over(self, token: str | None) -> None:
        # Losing an hour of download is bad. Failing a job because a string
        # would not parse is worse.
        assert PipelineState.decode(token) == PipelineState()

    def test_unknown_fields_are_ignored(self) -> None:
        state = PipelineState.decode('{"url": "https://a.test/x", "invented_by_v2": true}')

        assert state.url == "https://a.test/x"

    def test_a_field_of_the_wrong_type_is_ignored(self) -> None:
        state = PipelineState.decode('{"url": 12, "expected_bytes": "large"}')

        assert state.url is None
        assert state.expected_bytes is None

    def test_a_boolean_is_not_mistaken_for_a_byte_count(self) -> None:
        assert PipelineState.decode('{"expected_bytes": true}').expected_bytes is None

    def test_a_receipt_without_a_reference_is_not_a_receipt(self) -> None:
        # Treating one as proof of delivery would let a resumed job skip the
        # transfer entirely.
        assert PipelineState.decode('{"receipt": {"provider": "fake"}}').receipt is None

    def test_an_unreadable_digest_is_dropped_rather_than_trusted(self) -> None:
        artifact = ArtifactState(name="a.mp4", size_bytes=1, fingerprint="sha256:zzz")

        assert artifact.digest is None

    def test_an_artifact_that_never_had_a_digest_simply_has_none(self) -> None:
        assert ArtifactState(name="a.mp4", size_bytes=1).digest is None

    def test_an_artifact_record_without_a_size_is_not_an_artifact(self) -> None:
        assert PipelineState.decode('{"artifact": {"name": "a.mp4"}}').artifact is None

    def test_an_unknown_media_kind_becomes_other(self) -> None:
        assert PipelineState(kind="hologram").media_kind is MediaType.OTHER


class TestArtifactOwnership:
    def test_an_artifact_belongs_only_to_the_lease_it_was_written_in(self) -> None:
        state = PipelineState().with_download(_download_result(), lease_id="lease-1")

        assert state.artifact_in("lease-1") is not None
        assert state.artifact_in("lease-2") is None, "a lease belongs to one attempt"

    def test_verification_does_not_carry_across_leases(self) -> None:
        state = PipelineState().with_download(_download_result(), lease_id="lease-1")
        state = state.with_verified(artifact_ref())

        assert state.is_verified_in("lease-1") is True
        assert state.is_verified_in("lease-2") is False

    def test_a_fresh_download_invalidates_the_previous_verification(self) -> None:
        state = PipelineState().with_download(_download_result(), lease_id="lease-1")
        state = state.with_verified(artifact_ref())

        state = state.with_download(_download_result(), lease_id="lease-2")

        assert state.verified is False, "these are different bytes"

    def test_releasing_forgets_the_local_artifacts_but_keeps_the_receipt(self) -> None:
        state = (
            PipelineState()
            .with_download(_download_result(), lease_id="lease-1")
            .with_receipt(receipt())
            .released_locally()
        )

        assert state.released is True
        assert state.artifact is None
        assert state.thumbnail is None
        assert state.receipt is not None, "the metadata outlives the media"


class TestSelection:
    def test_the_default_choice_is_the_best_available(self) -> None:
        assert PipelineState().selection().preference is FormatPreference.BEST

    def test_a_capped_choice_becomes_a_height_limit(self) -> None:
        selection = PipelineState(quality_key="h720", quality_height=720).selection()

        assert selection.max_height == 720

    def test_an_audio_choice_becomes_an_audio_only_selection(self) -> None:
        state = PipelineState(quality_key="audio", quality_audio_only=True)

        assert state.selection().wants_audio_only is True


class TestReceiptState:
    def test_the_confirmation_time_survives_the_round_trip(self) -> None:
        recorded = ReceiptState.of(receipt())

        assert recorded.confirmed_at == datetime(2026, 1, 1, tzinfo=UTC)

    def test_the_announcement_is_kept_so_history_can_link_to_it(self) -> None:
        assert ReceiptState.of(receipt()).message_id == "7"


def _download_result() -> DownloadResult:
    """Return the smallest thing ``with_download`` needs, without an engine."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return DownloadResult(
        url="https://example.com/a.mp4",
        provider="fakesite",
        artifacts=(
            artifact_ref(),
            ArtifactRef(
                lease_id="lease-1",
                name="poster.jpg",
                size_bytes=64,
                role=ArtifactRole.THUMBNAIL,
            ),
        ),
        metadata=MediaMetadata(
            url="https://example.com/a.mp4",
            provider="fakesite",
            title="A clip",
            kind=MediaType.VIDEO,
            probed_at=now,
        ),
        selected_format=SelectedFormat(format_id="137", width=1920, height=1080),
        total_bytes=2112,
        started_at=now,
        finished_at=now,
    )
