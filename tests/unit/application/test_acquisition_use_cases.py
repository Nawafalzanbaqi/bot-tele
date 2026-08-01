"""Authorisation, probing, acquisition and history, with fake ports."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
