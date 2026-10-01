"""Authorisation, probing, acquisition and history, with fake ports."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mediahub.application.access.ports import AuditEvent, AuditOutcome
from mediahub.application.access.use_cases.authorize_principal import (
    AuthorizePrincipal,
    AuthorizeQuery,
)
from mediahub.application.common.errors import PermissionDeniedError
from mediahub.application.delivery.errors import ArtifactTooLargeError
from mediahub.application.delivery.ports import (
    DeliveryKind,
    DeliveryTarget,
    TargetAddress,
)
from mediahub.application.download.dto import (
    AcquireMediaCommand,
    AcquisitionSummary,
    GetHistoryQuery,
    ProbeSourceQuery,
)
from mediahub.application.download.errors import (
    FormatUnavailableError,
    SizeLimitExceededError,
)
from mediahub.application.download.journal import JournalEntry
from mediahub.application.download.use_cases.acquire_media import AcquireMedia
from mediahub.application.download.use_cases.describe_capabilities import (
    DescribeCapabilities,
)
from mediahub.application.download.use_cases.get_history import GetHistory
from mediahub.application.download.use_cases.probe_source import ProbeSource
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.access.enums import Action, Role
from mediahub.domain.access.policies import AllowListPolicy, AuthorizationPolicy
from mediahub.domain.download.enums import FailureKind
from mediahub.domain.media.enums import MediaType
from mediahub.infrastructure.delivery.registry import (
    DeliveryProviderRegistry,
    ProviderRegistration,
)
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.download.ytdlp.mapping import to_metadata
from mediahub.infrastructure.persistence.memory.journal import InMemoryAcquisitionJournal
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import DownloadSettings
from tests.support.delivery_fakes import FakeDeliveryProvider
from tests.support.ytdlp_fakes import download_script, factory_for, video_info

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
URL = "https://example.com/watch?v=abc123"


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def router_over(provider: FakeDeliveryProvider) -> DeliveryProviderRegistry:
    """Wrap a provider in the router the use cases actually depend on.

    The use cases take a *router*, never a provider - that indirection is what
    lets a destination be added without changing them.
    """
    return DeliveryProviderRegistry(
        registrations=[ProviderRegistration(provider)], default_provider=provider.name
    )


def target() -> DeliveryTarget:
    return DeliveryTarget(
        provider="fake", address=TargetAddress(provider="fake", opaque={"chat": "1"})
    )


def engine(**kwargs: object) -> YtDlpDownloader:
    return YtDlpDownloader(
        DownloadSettings(enabled=True, probe_attempts=1),
        youtube_dl_factory=factory_for(**kwargs),  # type: ignore[arg-type]
    )


class TestAuthorizePrincipal:
    def _use_case(self, audit: RecordingAudit, **ids: tuple[str, ...]) -> AuthorizePrincipal:
        return AuthorizePrincipal(
            allow_list=AllowListPolicy.from_ids(scheme="telegram", **ids),
            authorization=AuthorizationPolicy(),
            audit=audit,
            clock=FrozenClock(),
        )

    async def test_allows_a_listed_owner(self) -> None:
        audit = RecordingAudit()
        use_case = self._use_case(audit, owner_ids=("42",))

        principal = await use_case.execute(
            AuthorizeQuery(
                scheme="telegram",
                external_id="42",
                action=Action.SUBMIT_SOURCE,
                display_name="Ada",
            )
        )

        assert principal.role is Role.OWNER
        assert principal.identity == "telegram:42"
        assert principal.display_name == "Ada"
        assert audit.events[-1].outcome is AuditOutcome.ALLOWED

    async def test_refuses_an_unknown_identity_and_audits_it(self) -> None:
        audit = RecordingAudit()
        use_case = self._use_case(audit, owner_ids=("42",))

        with pytest.raises(PermissionDeniedError):
            await use_case.execute(
                AuthorizeQuery(scheme="telegram", external_id="999", action=Action.SUBMIT_SOURCE)
            )

        assert audit.events[-1].outcome is AuditOutcome.DENIED
        assert audit.events[-1].actor == "telegram:999"

    async def test_refuses_an_action_the_role_lacks(self) -> None:
        audit = RecordingAudit()
        use_case = self._use_case(audit, readonly_ids=("7",))

        with pytest.raises(PermissionDeniedError):
            await use_case.execute(
                AuthorizeQuery(scheme="telegram", external_id="7", action=Action.SUBMIT_SOURCE)
            )

        assert "may not" in (audit.events[-1].reason or "")

    async def test_refusals_reveal_nothing(self) -> None:
        audit = RecordingAudit()
        unknown = self._use_case(audit, owner_ids=("1",))
        listed = self._use_case(audit, readonly_ids=("2",))

        with pytest.raises(PermissionDeniedError) as first:
            await unknown.execute(
                AuthorizeQuery(scheme="telegram", external_id="999", action=Action.SUBMIT_SOURCE)
            )
        with pytest.raises(PermissionDeniedError) as second:
            await listed.execute(
                AuthorizeQuery(scheme="telegram", external_id="2", action=Action.SUBMIT_SOURCE)
            )

        assert first.value.message == second.value.message


class TestProbeSource:
    async def test_describes_the_source_and_its_options(self) -> None:
        use_case = ProbeSource(downloader=engine(info=video_info()))

        summary = await use_case.execute(ProbeSourceQuery(url=URL))

        assert summary.title == "A Test Video"
        assert summary.provider == "testsite"
        assert summary.kind is MediaType.VIDEO
        assert summary.duration_seconds == 125.0
        assert summary.thumbnail_url == "https://cdn.example.com/big.jpg"
        assert [option.key for option in summary.qualities] == [
            "best",
            "h1080",
            "h360",
            "audio",
        ]

    async def test_the_ceiling_narrows_the_options(self) -> None:
        use_case = ProbeSource(downloader=engine(info=video_info()), max_bytes=5_000_000)

        summary = await use_case.execute(ProbeSourceQuery(url=URL))

        assert "h1080" not in [option.key for option in summary.qualities]

    async def test_an_item_taken_from_a_collection_says_so(self) -> None:
        class FromCollection:
            async def probe(self, url: str, *, timeout_seconds: float | None = None) -> Any:
                del timeout_seconds
                return replace(
                    to_metadata(video_info(), url=url, probed_at=NOW),
                    from_playlist=True,
                    entry_count=12,
                )

        use_case = ProbeSource(downloader=FromCollection())  # type: ignore[arg-type]

        summary = await use_case.execute(ProbeSourceQuery(url=URL))

        assert summary.from_playlist is True
        assert summary.playlist_size == 12
        assert summary.is_playlist is False
        assert summary.qualities, "the item itself is offered, not the list"

    async def test_an_ordinary_item_carries_no_collection_note(self) -> None:
        use_case = ProbeSource(downloader=engine(info=video_info()))

        summary = await use_case.execute(ProbeSourceQuery(url=URL))

        assert summary.from_playlist is False
        assert summary.playlist_size is None


class TestAcquireMedia:
    def _use_case(
        self,
        tmp_path: Path,
        *,
        delivery: FakeDeliveryProvider,
        journal: InMemoryAcquisitionJournal,
        script_kwargs: dict[str, object] | None = None,
    ) -> AcquireMedia:
        return AcquireMedia(
            downloader=engine(
                info=video_info(requested_downloads=[{"format_id": "18", "ext": "mp4"}]),
                script=download_script(**(script_kwargs or {"size_bytes": 4096})),  # type: ignore[arg-type]
            ),
            delivery=router_over(delivery),
            workspace=FilesystemWorkspace(tmp_path / "ws"),
            journal=journal,
            clock=FrozenClock(),
            max_item_bytes=100 * 1024 * 1024,
        )

    async def test_delivers_and_releases_the_local_copy(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        journal = InMemoryAcquisitionJournal()
        use_case = self._use_case(tmp_path, delivery=delivery, journal=journal)

        summary = await use_case.execute(
            AcquireMediaCommand(
                url=URL, quality_key="best", target=target(), requested_by="telegram:1"
            )
        )

        assert summary.bytes_delivered == 4096
        assert summary.remote_id == "fake-ref-1"
        assert summary.message_id == "1"
        assert summary.local_copy_released is True
        # The whole point: nothing is left on disk.
        assert list((tmp_path / "ws").iterdir()) == []

    async def test_reports_where_the_time_went(self, tmp_path: Path) -> None:
        """Three stages, each non-negative, together no more than the whole."""
        use_case = self._use_case(
            tmp_path, delivery=FakeDeliveryProvider(), journal=InMemoryAcquisitionJournal()
        )

        summary = await use_case.execute(
            AcquireMediaCommand(
                url=URL, quality_key="best", target=target(), requested_by="telegram:1"
            )
        )

        stages = summary.stages
        assert stages is not None
        assert stages.probe_seconds >= 0
        assert stages.download_seconds >= 0
        assert stages.deliver_seconds >= 0
        assert stages.total_seconds <= summary.elapsed_seconds + 1e-6

    async def test_records_history(self, tmp_path: Path) -> None:
        journal = InMemoryAcquisitionJournal()
        use_case = self._use_case(tmp_path, delivery=FakeDeliveryProvider(), journal=journal)

        await use_case.execute(
            AcquireMediaCommand(
                url=URL, quality_key="audio", target=target(), requested_by="telegram:1"
            )
        )

        entries = await journal.recent("telegram:1")
        assert len(entries) == 1
        assert entries[0].quality_label == "Audio only"
        assert entries[0].remote_unique_id == "fake-uniq"

    async def test_audio_choice_is_presented_as_audio(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        use_case = self._use_case(tmp_path, delivery=delivery, journal=InMemoryAcquisitionJournal())

        await use_case.execute(
            AcquireMediaCommand(
                url=URL, quality_key="audio", target=target(), requested_by="telegram:1"
            )
        )

        assert delivery.delivered[0].kind is DeliveryKind.AUDIO

    async def test_sends_the_thumbnail_alongside(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            journal=InMemoryAcquisitionJournal(),
            script_kwargs={"size_bytes": 2048, "extras": [("abc123.jpg", 100)]},
        )

        await use_case.execute(
            AcquireMediaCommand(
                url=URL, quality_key="best", target=target(), requested_by="telegram:1"
            )
        )

        thumbnail = delivery.delivered[0].thumbnail
        assert thumbnail is not None
        assert thumbnail.role is ArtifactRole.THUMBNAIL

    async def test_an_unoffered_quality_is_refused_before_downloading(self, tmp_path: Path) -> None:
        use_case = self._use_case(
            tmp_path, delivery=FakeDeliveryProvider(), journal=InMemoryAcquisitionJournal()
        )

        with pytest.raises(FormatUnavailableError):
            await use_case.execute(
                AcquireMediaCommand(
                    url=URL,
                    quality_key="h4320",
                    target=target(),
                    requested_by="telegram:1",
                )
            )

    async def test_something_too_large_for_the_destination_is_refused(self, tmp_path: Path) -> None:
        # The destination's ceiling is applied to the *download*, so an oversized
        # item is abandoned mid-stream rather than after an hour of bandwidth is
        # spent on something that could never have been sent.
        delivery = FakeDeliveryProvider(maximum_file_size=100)
        use_case = self._use_case(tmp_path, delivery=delivery, journal=InMemoryAcquisitionJournal())

        with pytest.raises((SizeLimitExceededError, ArtifactTooLargeError)) as excinfo:
            await use_case.execute(
                AcquireMediaCommand(
                    url=URL, quality_key="best", target=target(), requested_by="telegram:1"
                )
            )

        raised = excinfo.value
        assert isinstance(raised, (SizeLimitExceededError, ArtifactTooLargeError))
        assert raised.kind is FailureKind.POLICY
        assert delivery.delivered == []
        assert list((tmp_path / "ws").iterdir()) == []

    async def test_a_delivery_failure_still_releases_the_local_copy(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        delivery.fail_with = RuntimeError("boom")
        use_case = self._use_case(tmp_path, delivery=delivery, journal=InMemoryAcquisitionJournal())

        with pytest.raises(RuntimeError):
            await use_case.execute(
                AcquireMediaCommand(
                    url=URL, quality_key="best", target=target(), requested_by="telegram:1"
                )
            )

        assert list((tmp_path / "ws").iterdir()) == []


class TestDeliveryPolicy:
    """What the destination is told the file *is*, and what the person is told.

    A file that arrives and does not play is the worst outcome, because nothing
    reports that anything went wrong. So a video in a codec the chat client
    cannot decode is sent as a document, and a rung skipped for size is named.
    """

    def _use_case(
        self,
        tmp_path: Path,
        *,
        delivery: FakeDeliveryProvider,
        taken: dict[str, object],
    ) -> AcquireMedia:
        return AcquireMedia(
            downloader=engine(
                info=video_info(requested_downloads=[taken]),
                script=download_script(size_bytes=4096),
            ),
            delivery=router_over(delivery),
            workspace=FilesystemWorkspace(tmp_path / "ws"),
            journal=InMemoryAcquisitionJournal(),
            clock=FrozenClock(),
            max_item_bytes=100 * 1024 * 1024,
        )

    async def _run(self, use_case: AcquireMedia, key: str) -> AcquisitionSummary:
        return await use_case.execute(
            AcquireMediaCommand(
                url=URL, quality_key=key, target=target(), requested_by="telegram:1"
            )
        )

    async def test_h264_goes_as_an_inline_video(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "mp4a"},
        )

        summary = await self._run(use_case, "best")

        assert delivery.delivered[0].kind is DeliveryKind.VIDEO
        assert summary.sent_as_document is False

    async def test_hevc_goes_as_an_inline_video(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "x", "ext": "mp4", "vcodec": "hvc1.1.6.L93.B0", "acodec": "mp4a"},
        )

        await self._run(use_case, "best")

        assert delivery.delivered[0].kind is DeliveryKind.VIDEO

    @pytest.mark.parametrize("codec", ["vp09.00.40.08", "av01.0.08M.08"])
    async def test_vp9_or_av1_goes_as_a_document_and_says_so(
        self, tmp_path: Path, codec: str
    ) -> None:
        delivery = FakeDeliveryProvider()
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "313", "ext": "webm", "vcodec": codec, "acodec": "opus"},
        )

        summary = await self._run(use_case, "best")

        assert delivery.delivered[0].kind is DeliveryKind.DOCUMENT
        assert summary.sent_as_document is True

    async def test_an_unknown_codec_is_still_sent_as_video(self, tmp_path: Path) -> None:
        """Unknown is not known-bad; refusing to play inline needs evidence."""
        delivery = FakeDeliveryProvider()
        use_case = self._use_case(
            tmp_path, delivery=delivery, taken={"format_id": "18", "ext": "mp4"}
        )

        summary = await self._run(use_case, "best")

        assert delivery.delivered[0].kind is DeliveryKind.VIDEO
        assert summary.sent_as_document is False

    async def test_audio_is_audio_whatever_the_codec_says(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider()
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus"},
        )

        summary = await self._run(use_case, "audio")

        assert delivery.delivered[0].kind is DeliveryKind.AUDIO
        assert summary.sent_as_document is False

    async def test_auto_names_the_rung_it_skipped_for_size(self, tmp_path: Path) -> None:
        """The source offers 1080p at ~60 MB and 360p at 11 MB; the ceiling is 20 MB."""
        delivery = FakeDeliveryProvider(maximum_file_size=20_000_000)
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a"},
        )

        summary = await self._run(use_case, "auto")

        assert summary.quality_label == "360p"
        assert summary.capped_from == "1080p"

    async def test_auto_that_fits_at_the_top_is_not_called_capped(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider(maximum_file_size=100_000_000)
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "mp4a"},
        )

        summary = await self._run(use_case, "auto")

        assert summary.quality_label == "1080p"
        assert summary.capped_from is None

    async def test_the_label_names_the_frame_the_engine_took(self, tmp_path: Path) -> None:
        """Auto picks the 1080p rung; the engine takes a 640x360 file; the card says 360p."""
        delivery = FakeDeliveryProvider(maximum_file_size=100_000_000)
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1.42001E",
                "acodec": "mp4a",
                "width": 640,
                "height": 360,
            },
        )

        summary = await self._run(use_case, "auto")

        assert summary.quality_label == "360p"

    async def test_a_frame_at_the_rung_keeps_the_rung_name(self, tmp_path: Path) -> None:
        """A vertical 1080x1920 file under the 1080p rung is still "1080p", not "1920p"."""
        delivery = FakeDeliveryProvider(maximum_file_size=100_000_000)
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={
                "format_id": "137",
                "ext": "mp4",
                "vcodec": "avc1.640028",
                "acodec": "mp4a",
                "width": 1080,
                "height": 1920,
            },
        )

        summary = await self._run(use_case, "auto")

        assert summary.quality_label == "1080p"

    async def test_max_takes_the_best_rendition_whatever_the_codec(self, tmp_path: Path) -> None:
        """/max on a 4K VP9 source: the 4K file, as a document, named after its frame."""
        delivery = FakeDeliveryProvider(maximum_file_size=100_000_000)
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={
                "format_id": "313",
                "ext": "mp4",
                "vcodec": "vp09.00.51.08",
                "acodec": "opus",
                "width": 3840,
                "height": 2160,
            },
        )

        summary = await self._run(use_case, "max")

        assert summary.quality_label == "2160p"
        assert summary.sent_as_document is True
        assert summary.capped_from is None
        assert delivery.delivered[0].kind is DeliveryKind.DOCUMENT

    async def test_a_rung_the_person_chose_is_never_called_capped(self, tmp_path: Path) -> None:
        delivery = FakeDeliveryProvider(maximum_file_size=20_000_000)
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a"},
        )

        summary = await self._run(use_case, "h360")

        assert summary.capped_from is None

    async def test_auto_falling_to_audio_names_the_best_rung(self, tmp_path: Path) -> None:
        """Every video rung is too large; sound is delivered and the skip is named."""
        delivery = FakeDeliveryProvider(maximum_file_size=5_000_000)
        use_case = self._use_case(
            tmp_path,
            delivery=delivery,
            taken={"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
        )

        summary = await self._run(use_case, "auto")

        assert summary.quality_label == "Audio only"
        assert summary.capped_from == "1080p"
        assert delivery.delivered[0].kind is DeliveryKind.AUDIO


class TestHistoryAndCapabilities:
    async def test_history_is_newest_first_and_per_principal(self) -> None:
        journal = InMemoryAcquisitionJournal()
        for index in range(3):
            await journal.record(
                JournalEntry(
                    principal="telegram:1",
                    url=f"https://example.com/{index}",
                    provider="testsite",
                    title=f"Item {index}",
                    quality_label="720p",
                    bytes_delivered=100,
                    remote_id="R",
                    delivered_at=NOW,
                )
            )
        await journal.record(
            JournalEntry(
                principal="telegram:2",
                url="https://example.com/other",
                provider="testsite",
                title="Someone else",
                quality_label="720p",
                bytes_delivered=1,
                remote_id="R",
                delivered_at=NOW,
            )
        )

        entries = await GetHistory(journal=journal).execute(GetHistoryQuery(principal="telegram:1"))

        assert [entry.title for entry in entries] == ["Item 2", "Item 1", "Item 0"]

    async def test_history_limit_is_capped(self) -> None:
        journal = InMemoryAcquisitionJournal()

        entries = await GetHistory(journal=journal).execute(
            GetHistoryQuery(principal="telegram:1", limit=10_000)
        )

        assert entries == ()

    async def test_capabilities_report_the_effective_ceiling(self) -> None:
        use_case = DescribeCapabilities(
            downloader=engine(info=video_info()),
            delivery=router_over(FakeDeliveryProvider(maximum_file_size=50)),
            max_item_bytes=5000,
        )

        summary = await use_case.execute()

        assert summary.effective_max_bytes == 50
        assert summary.delivery_provider == "fake"
        assert summary.engine == "yt-dlp"
