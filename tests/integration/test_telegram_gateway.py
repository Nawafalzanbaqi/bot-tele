"""The gateway end to end, with only the two networks faked.

Everything between a Telegram update and a delivered file is real here: the
gateway, the access policy, the application use cases, the download engine
adapter, the filesystem workspace and the Telegram delivery provider. Only
``YoutubeDL`` and the Bot API transport are doubles.

This is the test that proves the phase's claim: a Telegram message causes a
download, a delivery, a stored reference and an empty disk - and the gateway
itself did nothing but translate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mediahub.application.access.use_cases.authorize_principal import AuthorizePrincipal
from mediahub.application.credentials.use_cases.manage_cookies import (
    DescribeCookies,
    DiscardCookies,
    InstallCookies,
)
from mediahub.application.download.use_cases.acquire_media import AcquireMedia
from mediahub.application.download.use_cases.describe_capabilities import (
    DescribeCapabilities,
)
from mediahub.application.download.use_cases.get_history import GetHistory
from mediahub.application.download.use_cases.probe_source import ProbeSource
from mediahub.domain.access.policies import AllowListPolicy, AuthorizationPolicy
from mediahub.infrastructure.delivery.registry import (
    DeliveryProviderRegistry,
    ProviderRegistration,
)
from mediahub.infrastructure.delivery.telegram.provider import TelegramDeliveryProvider
from mediahub.infrastructure.download.ytdlp.downloader import YtDlpDownloader
from mediahub.infrastructure.persistence.memory.journal import InMemoryAcquisitionJournal
from mediahub.infrastructure.security.audit_sink import LoggingAuditSink
from mediahub.infrastructure.system.clock import SystemClock
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.presentation.telegram.gateway import TelegramGateway
from mediahub.presentation.telegram.handlers import (
    DELIVERY_PROVIDER,
    GatewayServices,
    TelegramHandlers,
)
from mediahub.presentation.telegram.sessions import SessionStore
from mediahub.shared.config.settings import DownloadSettings
from tests.support.telegram_fakes import (
    FakeCookieStore,
    FakeMessenger,
    FakeUploader,
    callback_update,
    message_update,
)
from tests.support.ytdlp_fakes import download_script, factory_for, video_info

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.integration

USER = 4242
URL = "https://example.com/watch?v=abc123"


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def build_stack(
    workspace_root: Path,
    *,
    batches: Sequence[Sequence[dict[str, object]]] = (),
    allowed: Sequence[int] = (USER,),
) -> tuple[TelegramGateway, FakeMessenger, FakeUploader, InMemoryAcquisitionJournal, Path]:
    """Wire the real stack with fake networks, exactly as the entry point does."""
    messenger = FakeMessenger(batches=batches)
    uploader = FakeUploader()
    journal = InMemoryAcquisitionJournal()
    workspace = FilesystemWorkspace(workspace_root)

    engine = YtDlpDownloader(
        DownloadSettings(enabled=True, probe_attempts=1, progress_interval_seconds=0.0),
        youtube_dl_factory=factory_for(
            info=video_info(requested_downloads=[{"format_id": "18", "ext": "mp4"}]),
            script=download_script(name="abc123.mp4", size_bytes=4096, chunks=(1024, 2048, 4096)),
        ),
    )
    # The use cases take a *router*, never a provider: that indirection is the
    # phase's claim, so the integration test wires it the way production does.
    delivery = DeliveryProviderRegistry(
        registrations=[
            ProviderRegistration(TelegramDeliveryProvider(uploader, bot_principal="bot-777"))
        ],
        default_provider=DELIVERY_PROVIDER,
    )

    cookie_store = FakeCookieStore()
    handlers = TelegramHandlers(
        GatewayServices(
            messenger=messenger,
            authorize=AuthorizePrincipal(
                allow_list=AllowListPolicy.from_ids(
                    scheme="telegram", owner_ids=tuple(str(user) for user in allowed)
                ),
                authorization=AuthorizationPolicy(),
                audit=LoggingAuditSink(),
                clock=SystemClock(),
            ),
            probe_source=ProbeSource(downloader=engine),
            acquire_media=AcquireMedia(
                downloader=engine,
                delivery=delivery,
                workspace=workspace,
                journal=journal,
                clock=SystemClock(),
                max_item_bytes=100 * 1024 * 1024,
            ),
            get_history=GetHistory(journal=journal),
            describe_capabilities=DescribeCapabilities(
                downloader=engine, delivery=delivery, max_item_bytes=100 * 1024 * 1024
            ),
            sessions=SessionStore(),
            install_cookies=InstallCookies(store=cookie_store),
            describe_cookies=DescribeCookies(store=cookie_store),
            discard_cookies=DiscardCookies(store=cookie_store),
            progress_interval_seconds=0.0,
        )
    )
    gateway = TelegramGateway(messenger, handlers)
    return gateway, messenger, uploader, journal, workspace_root


async def test_a_link_becomes_a_delivered_file_and_an_empty_disk(
    workspace_root: Path,
) -> None:
    """The whole phase, in one test."""
    gateway, messenger, uploader, journal, root = build_stack(
        workspace_root, batches=[[message_update(URL, update_id=1)]]
    )

    # 1. The user sends a link; the gateway probes and offers choices.
    await gateway.poll_once()
    await gateway.settle()

    prompt = messenger.sent[-1]
    assert "A Test Video" in prompt.text
    assert "2د 05ث" in prompt.text
    assert prompt.reply_markup is not None
    buttons = [
        button
        for row in prompt.reply_markup["inline_keyboard"]
        for button in row
        if button["text"] != "Cancel"
    ]
    assert str(buttons[0]["text"]).startswith("Best available")

    # 2. The user taps a quality.
    token = str(buttons[0]["callback_data"]).split("|")[1]
    messenger.batches.append([callback_update(f"q|{token}|best", update_id=2)])
    await gateway.poll_once()
    await gateway.settle()
    await gateway._handlers.drain(timeout=10)

    # 3. The file was uploaded to the right conversation.
    assert len(uploader.uploads) == 1
    upload = uploader.uploads[0]
    assert upload["chat_id"] == str(USER)
    assert upload["kind"] == "video"
    assert upload["size"] == 4096

    # 4. The reference Telegram handed back is remembered.
    entries = await journal.recent(f"telegram:{USER}")
    assert len(entries) == 1
    assert entries[0].remote_id == "FILE-ABC"
    assert entries[0].remote_unique_id == "UNIQ-ABC"
    assert entries[0].message_id == "500"
    assert entries[0].url == URL

    # 5. The user was told, and the local copy is gone.
    assert "تم الإرسال" in messenger.last_text
    assert list(root.iterdir()) == [], "no lease may survive a completed acquisition"


async def test_history_reflects_what_was_delivered(workspace_root: Path) -> None:
    gateway, messenger, _, _, _ = build_stack(
        workspace_root, batches=[[message_update(URL, update_id=1)]]
    )
    await gateway.poll_once()
    await gateway.settle()

    token = str(
        messenger.sent[-1].reply_markup["inline_keyboard"][0][0]["callback_data"]  # type: ignore[index]
    ).split("|")[1]
    messenger.batches.append([callback_update(f"q|{token}|best", update_id=2)])
    await gateway.poll_once()
    await gateway.settle()
    await gateway._handlers.drain(timeout=10)

    messenger.batches.append([message_update("/history", update_id=3)])
    await gateway.poll_once()
    await gateway.settle()

    assert "A Test Video" in messenger.last_text
    assert "Best available" in messenger.last_text


async def test_settings_reports_the_effective_ceiling(workspace_root: Path) -> None:
    gateway, messenger, _, _, _ = build_stack(
        workspace_root, batches=[[message_update("/settings", update_id=1)]]
    )

    await gateway.poll_once()
    await gateway.settle()

    # 50 MiB is Telegram's limit and lower than the engine's, so it is the one
    # that actually applies - which is exactly what a user needs to be told.
    assert "50.0 MiB" in messenger.last_text
    assert "yt-dlp" in messenger.last_text


async def test_a_stranger_is_refused_and_nothing_happens(workspace_root: Path) -> None:
    gateway, messenger, uploader, journal, root = build_stack(
        workspace_root,
        batches=[[message_update(URL, update_id=1, user_id=1234)]],
        allowed=(USER,),
    )

    await gateway.poll_once()
    await gateway.settle()

    # Silence, not a refusal: a reply would confirm that a bot answers here.
    assert messenger.sent == []
    assert messenger.edits == []
    assert uploader.uploads == []
    assert await journal.recent("telegram:1234") == ()
    assert not root.exists() or list(root.iterdir()) == []
