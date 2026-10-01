"""The gateway's handlers, the progress presenter and the poll loop.

Every application use case is a fake here, which is the point: it proves the
gateway does nothing but translate. If a test needed a download engine or a
database to pass, the adapter would be doing too much.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger

from mediahub.application.access.ports import Principal
from mediahub.application.common.errors import PermissionDeniedError
from mediahub.application.credentials.use_cases.manage_cookies import (
    DescribeCookies,
    DiscardCookies,
    InstallCookies,
)
from mediahub.application.delivery.ports import DeliveryProgress, DeliveryStage
from mediahub.application.download.dto import (
    AcquisitionSummary,
    CapabilitiesSummary,
    HistoryEntrySummary,
    QualityOption,
    SourceSummary,
)
from mediahub.application.download.errors import (
    MetadataUnavailableError,
    NoPlayableMediaError,
)
from mediahub.application.download.ports import DownloadProgress, DownloadStage
from mediahub.domain.access.enums import Action, Role
from mediahub.domain.media.enums import MediaType
from mediahub.presentation.telegram import formatters
from mediahub.presentation.telegram.gateway import TelegramGateway
from mediahub.presentation.telegram.handlers import (
    COMMAND_ACTIONS,
    GatewayServices,
    TelegramHandlers,
)
from mediahub.presentation.telegram.keyboards import (
    CallbackAction,
    CallbackPayload,
    decode_callback,
)
from mediahub.presentation.telegram.progress import ProgressPresenter
from mediahub.presentation.telegram.sessions import SessionStore
from mediahub.presentation.telegram.updates import parse_update
from tests.support.telegram_fakes import (
    FakeCookieStore,
    FakeMessenger,
    callback_update,
    document_update,
    message_update,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.delivery.ports import DeliveryProgressCallback
    from mediahub.application.download.dto import (
        AcquireMediaCommand,
        GetHistoryQuery,
        ProbeSourceQuery,
    )
    from mediahub.application.download.ports import ProgressCallback

pytestmark = pytest.mark.unit

COOKIE_JAR = (
    b"# Netscape HTTP Cookie File\n"
    b"x.com\tTRUE\t/\tTRUE\t2000000000\tauth_token\tsecret-value-here\n"
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
URL = "https://example.com/watch?v=abc"


# --------------------------------------------------------------------------- #
# Fake use cases                                                               #
# --------------------------------------------------------------------------- #


class FakeAuthorize:
    def __init__(self, *, allowed: bool = True, role: Role = Role.OWNER) -> None:
        self.allowed = allowed
        self.role = role
        self.calls: list[Any] = []

    async def execute(self, request: Any) -> Principal:
        self.calls.append(request)
        if not self.allowed:
            message = "You are not authorised to use this service."
            raise PermissionDeniedError(message)
        return Principal(
            identity=f"telegram:{request.external_id}",
            role=self.role,
            display_name=request.display_name,
        )


class FakeProbe:
    def __init__(
        self, *, result: SourceSummary | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or _summary()
        self.error = error
        self.calls: list[str] = []

    async def execute(self, request: ProbeSourceQuery) -> SourceSummary:
        self.calls.append(request.url)
        if self.error is not None:
            raise self.error
        return self.result


class FakeAcquire:
    def __init__(self, *, error: Exception | None = None, emit_delivery: bool = False) -> None:
        self.error = error
        # The two progress channels are emitted one at a time, because the
        # presenter keeps a single latest-state slot: reporting both in one
        # synchronous run would leave which one gets rendered up to scheduling.
        self.emit_delivery = emit_delivery
        self.commands: list[AcquireMediaCommand] = []
        self.progress_updates = 0
        self.delivery_updates = 0
        self.observed_cancel = False
        self.gate = asyncio.Event()
        self.hold = False

    async def execute(
        self,
        request: AcquireMediaCommand,
        *,
        on_progress: ProgressCallback | None = None,
        on_delivery_progress: DeliveryProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AcquisitionSummary:
        self.commands.append(request)
        if self.emit_delivery:
            if on_delivery_progress is not None:
                on_delivery_progress(
                    DeliveryProgress(
                        stage=DeliveryStage.UPLOADING,
                        sent_bytes=768,
                        total_bytes=1024,
                        provider="telegram",
                    )
                )
                self.delivery_updates += 1
        elif on_progress is not None:
            on_progress(
                DownloadProgress(
                    stage=DownloadStage.DOWNLOADING,
                    downloaded_bytes=512,
                    total_bytes=1024,
                )
            )
            self.progress_updates += 1
        if self.hold:
            await self.gate.wait()
        if cancellation is not None and cancellation.cancelled:
            self.observed_cancel = True
        if self.error is not None:
            raise self.error
        return AcquisitionSummary(
            url=request.url,
            provider="testsite",
            title="A Test Video",
            quality_label="720p",
            bytes_delivered=1024,
            elapsed_seconds=1.5,
            remote_id="R1",
            message_id="9",
            delivered_at=NOW,
        )


class FakeHistory:
    def __init__(self, entries: tuple[HistoryEntrySummary, ...] = ()) -> None:
        self.entries = entries
        self.calls: list[GetHistoryQuery] = []

    async def execute(self, request: GetHistoryQuery) -> tuple[HistoryEntrySummary, ...]:
        self.calls.append(request)
        return self.entries


class FakeCapabilities:
    async def execute(self, request: Any = None) -> CapabilitiesSummary:
        del request
        return CapabilitiesSummary(
            engine="yt-dlp",
            engine_version="2026.1.1",
            max_item_bytes=2_000_000_000,
            delivery_provider="telegram",
            delivery_max_bytes=52_428_800,
            effective_max_bytes=52_428_800,
            supports_audio_only=True,
            allow_live=False,
            allow_playlist=False,
        )


def _summary(**overrides: object) -> SourceSummary:
    defaults: dict[str, object] = {
        "url": URL,
        "provider": "testsite",
        "title": "A Test Video",
        "kind": MediaType.VIDEO,
        "is_live": False,
        "is_playlist": False,
        "qualities": (
            QualityOption(key="best", label="Best available"),
            QualityOption(key="h720", label="720p", height=720),
        ),
        "duration_seconds": 100.0,
    }
    defaults.update(overrides)
    return SourceSummary(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def messenger() -> FakeMessenger:
    return FakeMessenger()


class FakeEgress:
    """Stands in for the engine's egress routes."""

    def __init__(
        self,
        *,
        configured: bool = True,
        routed: tuple[str, ...] = ("blocked.example",),
        proton: tuple[str, ...] = (),
    ) -> None:
        self.configured = configured
        self._routed: dict[str, str] = dict.fromkeys(routed, "warp")
        self.pinned: list[tuple[str, str]] = []
        self.proton = proton

    @property
    def is_configured(self) -> bool:
        return self.configured

    @property
    def proton_countries(self) -> tuple[str, ...]:
        return self.proton

    def pin(self, host: str, tier: str = "warp") -> bool:
        self.pinned.append((host, tier))
        if self._routed.get(host) == tier:
            return False
        self._routed[host] = tier
        return True

    def routed(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._routed.items()))


def build(
    messenger: FakeMessenger,
    *,
    authorize: FakeAuthorize | None = None,
    probe: FakeProbe | None = None,
    acquire: FakeAcquire | None = None,
    history: FakeHistory | None = None,
    auto: bool = False,
    max_concurrent: int = 1,
    egress: FakeEgress | None = None,
) -> tuple[TelegramHandlers, GatewayServices]:
    cookie_store = FakeCookieStore()
    services = GatewayServices(
        messenger=messenger,
        authorize=authorize or FakeAuthorize(),  # type: ignore[arg-type]
        probe_source=probe or FakeProbe(),  # type: ignore[arg-type]
        acquire_media=acquire or FakeAcquire(),  # type: ignore[arg-type]
        get_history=history or FakeHistory(),  # type: ignore[arg-type]
        describe_capabilities=FakeCapabilities(),  # type: ignore[arg-type]
        sessions=SessionStore(),
        install_cookies=InstallCookies(store=cookie_store),
        describe_cookies=DescribeCookies(store=cookie_store),
        discard_cookies=DiscardCookies(store=cookie_store),
        progress_interval_seconds=0.01,
        auto_best_quality=auto,
        max_concurrent=max_concurrent,
        egress=egress,
    )
    return TelegramHandlers(services), services


async def handle(handlers: TelegramHandlers, update: dict[str, Any]) -> None:
    intent = parse_update(update)
    assert intent is not None
    await handlers.handle(intent)


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #


class TestCommands:
    async def test_start_greets_by_name(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, message_update("/start"))

        assert "أهلًا" in messenger.last_text
        assert "ada" in messenger.last_text

    async def test_help_lists_the_commands(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, message_update("/help"))

        for command in ("/start", "/help", "/settings", "/history"):
            assert command in messenger.last_text

    async def test_settings_reports_capabilities(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, message_update("/settings"))

        assert "yt-dlp" in messenger.last_text

    async def test_history_asks_only_for_the_callers_own(self, messenger: FakeMessenger) -> None:
        history = FakeHistory(
            (
                HistoryEntrySummary(
                    title="Earlier",
                    url=URL,
                    provider="testsite",
                    quality_label="720p",
                    bytes_delivered=1024,
                    delivered_at=NOW,
                ),
            )
        )
        handlers, _ = build(messenger, history=history)

        await handle(handlers, message_update("/history"))

        assert history.calls[0].principal == "telegram:4242"
        assert "Earlier" in messenger.last_text

    async def test_an_unknown_command_gets_help(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, message_update("/wat"))

        assert "/help" in messenger.last_text

    async def test_help_mentions_cancel(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, message_update("/help"))

        assert "/cancel" in messenger.last_text


class TestCancelCommand:
    async def test_cancel_with_nothing_running_says_so(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, message_update("/cancel"))

        assert "لا يوجد تحميل" in messenger.last_text

    async def test_cancel_stops_the_callers_running_acquisition(
        self, messenger: FakeMessenger
    ) -> None:
        acquire = FakeAcquire()
        acquire.hold = True
        handlers, _ = build(messenger, acquire=acquire, auto=True)

        await handle(handlers, message_update(URL))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert handlers.pending_tasks == 1

        await handle(handlers, message_update("/cancel", update_id=2, message_id=8))

        assert "جارٍ الإيقاف" in messenger.sent[-1].text
        acquire.gate.set()
        await handlers.drain(timeout=1.0)
        assert acquire.observed_cancel

    async def test_cancel_never_reaches_another_users_download(
        self, messenger: FakeMessenger
    ) -> None:
        acquire = FakeAcquire()
        acquire.hold = True
        handlers, _ = build(messenger, acquire=acquire, auto=True)

        await handle(handlers, message_update(URL, user_id=4242, chat_id=4242))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await handle(
            handlers,
            message_update("/cancel", update_id=2, user_id=5151, chat_id=5151, message_id=8),
        )

        assert "لا يوجد تحميل" in messenger.sent[-1].text
        acquire.gate.set()
        await handlers.drain(timeout=1.0)
        assert not acquire.observed_cancel


# --------------------------------------------------------------------------- #
# The URL flow                                                                 #
# --------------------------------------------------------------------------- #


class TestUrlFlow:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.netflix.com/watch/81234567",
            "https://shahid.mbc.net/ar/movies/x/movie-1",
            "https://open.spotify.com/track/abc",
            "https://tv.apple.com/show/x",
        ],
    )
    async def test_a_drm_service_is_refused_clearly_and_without_a_probe(
        self, messenger: FakeMessenger, url: str
    ) -> None:
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update(url))

        assert probe.calls == [], "the answer is known before any request is made"
        assert "DRM" in messenger.last_text

    async def test_a_link_is_probed_and_answered_with_buttons(
        self, messenger: FakeMessenger
    ) -> None:
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update(URL))

        assert probe.calls == [URL]
        posted = messenger.sent[-1]
        assert "A Test Video" in posted.text
        assert posted.reply_markup is not None
        labels = [
            button["text"] for row in posted.reply_markup["inline_keyboard"] for button in row
        ]
        assert labels == ["Best available", "720p", "إلغاء"]

    async def test_non_links_get_help_without_probing(self, messenger: FakeMessenger) -> None:
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update("hello there"))

        assert probe.calls == []
        assert "/help" in messenger.last_text

    async def test_a_live_source_offers_no_buttons(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger, probe=FakeProbe(result=_summary(is_live=True)))

        await handle(handlers, message_update(URL))

        assert messenger.sent[-1].reply_markup is None

    async def test_a_probe_failure_is_reported_without_internals(
        self, messenger: FakeMessenger
    ) -> None:
        error = MetadataUnavailableError(f"'{URL}' cannot be retrieved: private video")
        handlers, _ = build(messenger, probe=FakeProbe(error=error))

        await handle(handlers, message_update(URL))

        assert URL not in messenger.last_text
        assert "خاص" in messenger.last_text


# --------------------------------------------------------------------------- #
# Buttons and acquisition                                                      #
# --------------------------------------------------------------------------- #


async def start_session(
    handlers: TelegramHandlers, services: GatewayServices, messenger: FakeMessenger
) -> str:
    """Post a link and return the session token its buttons carry."""
    await handle(handlers, message_update(URL))
    markup = messenger.sent[-1].reply_markup
    assert markup is not None
    raw = markup["inline_keyboard"][0][0]["callback_data"]
    return str(raw).split("|")[1]


class TestButtons:
    async def test_choosing_a_quality_acquires_and_confirms(self, messenger: FakeMessenger) -> None:
        acquire = FakeAcquire()
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|h720"))
        await handlers.drain(timeout=5)

        assert acquire.commands[0].quality_key == "h720"
        assert acquire.commands[0].url == URL
        assert acquire.commands[0].requested_by == "telegram:4242"
        assert "تم الإرسال" in messenger.last_text

    async def test_the_delivery_target_names_this_chat(self, messenger: FakeMessenger) -> None:
        acquire = FakeAcquire()
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|best"))
        await handlers.drain(timeout=5)

        target = acquire.commands[0].target
        assert target.provider == "telegram"
        assert target.address.opaque["chat"] == "4242"

    async def test_progress_is_reported_while_running(self, messenger: FakeMessenger) -> None:
        acquire = FakeAcquire()
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|best"))
        await handlers.drain(timeout=5)

        assert acquire.progress_updates == 1
        assert any("50%" in edit.text for edit in messenger.edits)

    async def test_upload_progress_is_shown_as_sending(self, messenger: FakeMessenger) -> None:
        # The second half of the operation is the slow one on a domestic
        # connection, so it gets its own wording rather than a frozen "100%".
        acquire = FakeAcquire(emit_delivery=True)
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|best"))
        await handlers.drain(timeout=5)

        assert acquire.delivery_updates == 1
        assert any("إرسال · " in edit.text and "75%" in edit.text for edit in messenger.edits)

    async def test_cancel_dismisses_the_prompt(self, messenger: FakeMessenger) -> None:
        handlers, services = build(messenger)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"d|{token}|x"))

        assert messenger.edits[-1].text == "تمّ الإلغاء."
        assert services.sessions.get(token, owner="telegram:4242") is None

    async def test_stop_reaches_the_running_acquisition(self, messenger: FakeMessenger) -> None:
        acquire = FakeAcquire()
        acquire.hold = True
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|best"))
        await asyncio.sleep(0)  # let the task start
        await handle(handlers, callback_update(f"a|{token}|", callback_id="cb-2"))
        acquire.gate.set()
        await handlers.drain(timeout=5)

        assert acquire.observed_cancel

    async def test_an_expired_session_fails_cleanly(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, callback_update("q|deadbeef|best"))

        assert messenger.answers[-1][1] is not None
        assert "انتهت صلاحية" in messenger.answers[-1][1]

    async def test_a_malformed_payload_is_ignored(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, callback_update("!!!"))

        assert "لم يعد صالح" in (messenger.answers[-1][1] or "")

    async def test_another_principal_cannot_drive_the_session(
        self, messenger: FakeMessenger
    ) -> None:
        acquire = FakeAcquire()
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|best", user_id=9999))
        await handlers.drain(timeout=5)

        assert acquire.commands == []

    async def test_an_acquisition_failure_is_reported_without_internals(
        self, messenger: FakeMessenger
    ) -> None:
        acquire = FakeAcquire(error=MetadataUnavailableError("/workspace/lease/abc.mp4 is corrupt"))
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|best"))
        await handlers.drain(timeout=5)

        assert "/workspace" not in messenger.last_text
        assert "corrupt" not in messenger.last_text

    async def test_an_unexpected_failure_is_generic(self, messenger: FakeMessenger) -> None:
        acquire = FakeAcquire(error=RuntimeError("secret internal detail"))
        handlers, services = build(messenger, acquire=acquire)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"q|{token}|best"))
        await handlers.drain(timeout=5)

        assert "secret internal detail" not in messenger.last_text
        assert "خطأ" in messenger.last_text


# --------------------------------------------------------------------------- #
# Authorisation                                                                #
# --------------------------------------------------------------------------- #


class TestAuthorisation:
    async def test_a_denied_sender_gets_nothing_at_all(self, messenger: FakeMessenger) -> None:
        """Not a refusal: a reply confirms a bot answers here. The audit log has the denial."""
        probe = FakeProbe()
        handlers, _ = build(messenger, authorize=FakeAuthorize(allowed=False), probe=probe)

        await handle(handlers, message_update(URL))

        assert probe.calls == [], "a denied sender must not reach any use case"
        assert messenger.sent == []
        assert messenger.edits == []

    async def test_every_intent_is_authorised(self, messenger: FakeMessenger) -> None:
        authorize = FakeAuthorize()
        handlers, _ = build(messenger, authorize=authorize)

        await handle(handlers, message_update("/help"))
        await handle(handlers, message_update(URL))
        await handle(handlers, callback_update("q|deadbeef|best"))

        assert len(authorize.calls) == 3
        assert {call.scheme for call in authorize.calls} == {"telegram"}


# --------------------------------------------------------------------------- #
# Progress presenter                                                           #
# --------------------------------------------------------------------------- #


class TestProgressPresenter:
    async def test_edits_at_most_once_per_interval(self, messenger: FakeMessenger) -> None:
        presenter = ProgressPresenter(
            messenger, chat_id="1", message_id=5, title="A", interval_seconds=0.05
        )
        pump = asyncio.create_task(presenter.run())

        for index in range(1, 400):
            presenter.report(
                DownloadProgress(
                    stage=DownloadStage.DOWNLOADING,
                    downloaded_bytes=index * 1024,
                    total_bytes=400 * 1024,
                )
            )
        await asyncio.sleep(0.12)
        await presenter.stop()
        pump.cancel()

        assert len(messenger.edits) <= 5, "a chunk-rate edit would trip flood limits"

    async def test_identical_text_is_not_re_sent(self, messenger: FakeMessenger) -> None:
        presenter = ProgressPresenter(
            messenger, chat_id="1", message_id=5, title="A", interval_seconds=0.0
        )
        progress = DownloadProgress(
            stage=DownloadStage.DOWNLOADING, downloaded_bytes=10, total_bytes=100
        )

        presenter.report(progress)
        await presenter.stop()
        presenter.report(progress)
        await presenter.stop()

        assert len(messenger.edits) == 1

    async def test_upload_progress_renders_through_the_same_message(
        self, messenger: FakeMessenger
    ) -> None:
        # One message tells the whole story, so the presenter has to switch
        # renderers on what it was handed rather than on which method was called.
        presenter = ProgressPresenter(
            messenger, chat_id="1", message_id=5, title="A", interval_seconds=0.0
        )

        presenter.report_delivery(
            DeliveryProgress(
                stage=DeliveryStage.UPLOADING, sent_bytes=50, total_bytes=100, provider="telegram"
            )
        )
        await presenter.stop()

        assert len(messenger.edits) == 1
        assert "إرسال · " in messenger.edits[0].text
        assert "50%" in messenger.edits[0].text

    async def test_a_failing_edit_never_propagates(self, messenger: FakeMessenger) -> None:
        messenger.edit_error = RuntimeError("flood wait")
        presenter = ProgressPresenter(messenger, chat_id="1", message_id=5, title="A")

        presenter.report(DownloadProgress(stage=DownloadStage.DOWNLOADING, downloaded_bytes=1))
        await presenter.stop()

        assert messenger.edits == []

    async def test_reporting_before_the_pump_starts_is_safe(self, messenger: FakeMessenger) -> None:
        presenter = ProgressPresenter(messenger, chat_id="1", message_id=5, title="A")

        await presenter.stop()

        assert messenger.edits == []


# --------------------------------------------------------------------------- #
# /max                                                                         #
# --------------------------------------------------------------------------- #


class TestMaxCommand:
    async def test_a_link_is_probed_and_fetched_at_max_without_a_keyboard(self) -> None:
        messenger = FakeMessenger()
        probe = FakeProbe()
        acquire = FakeAcquire()
        handlers, _ = build(messenger, probe=probe, acquire=acquire, auto=False)

        await handle(handlers, message_update("/max https://example.com/watch?v=1"))
        await handlers.drain(timeout=1)

        assert probe.calls == ["https://example.com/watch?v=1"]
        assert [command.quality_key for command in acquire.commands] == ["max"]
        assert all(call.reply_markup is None for call in messenger.sent), "no quality keyboard"

    async def test_without_a_link_the_help_is_shown(self) -> None:
        messenger = FakeMessenger()
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update("/max"))

        assert "/max" in messenger.sent[0].text
        assert probe.calls == []

    def test_max_is_a_source_submission_for_access_purposes(self) -> None:
        assert COMMAND_ACTIONS["max"] is Action.SUBMIT_SOURCE
        assert "/max" in formatters.render_help()


# --------------------------------------------------------------------------- #
# /vpn                                                                         #
# --------------------------------------------------------------------------- #


class TestVpnCommand:
    async def test_a_link_pins_its_host_and_is_then_handled_like_any_link(self) -> None:
        messenger = FakeMessenger()
        egress = FakeEgress()
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe, egress=egress)

        await handle(handlers, message_update("/vpn https://Video.Example.com/watch?v=1"))

        assert egress.pinned == [("video.example.com", "warp")], "pinned by host, lower-cased"
        assert probe.calls == ["https://Video.Example.com/watch?v=1"], "then probed as usual"
        assert "video.example.com" in messenger.sent[0].text
        assert "عبر WARP" in messenger.sent[0].text
        assert "اختر الجودة" in messenger.sent[1].text, "the ordinary source card follows"

    async def test_a_host_already_routed_is_said_to_be(self) -> None:
        messenger = FakeMessenger()
        egress = FakeEgress(routed=("example.com",))
        handlers, _ = build(messenger, egress=egress)

        await handle(handlers, message_update("/vpn https://example.com/a"))

        assert "أصلًا" in messenger.sent[0].text
        assert egress.routed() == (("example.com", "warp"),)

    async def test_without_a_link_the_routes_are_listed(self) -> None:
        messenger = FakeMessenger()
        probe = FakeProbe()
        handlers, _ = build(
            messenger, probe=probe, egress=FakeEgress(routed=("b.example", "a.example"))
        )

        await handle(handlers, message_update("/vpn"))

        text = messenger.sent[0].text
        assert "• a.example — WARP\n• b.example — WARP" in text
        assert "ملف" in text, "says the list lives in an editable file"
        assert probe.calls == []

    async def test_an_empty_list_says_so(self) -> None:
        messenger = FakeMessenger()
        handlers, _ = build(messenger, egress=FakeEgress(routed=()))

        await handle(handlers, message_update("/vpn"))

        assert "لا توجد مواقع" in messenger.sent[0].text

    async def test_something_that_is_not_a_link_gets_the_help(self) -> None:
        messenger = FakeMessenger()
        egress = FakeEgress()
        handlers, _ = build(messenger, egress=egress)

        await handle(handlers, message_update("/vpn please"))

        assert "/vpn" in messenger.sent[0].text, "the help names the command"
        assert egress.pinned == []

    async def test_without_an_egress_the_command_says_so(self) -> None:
        messenger = FakeMessenger()
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update("/vpn https://example.com/a"))

        assert "لا يوجد نفق" in messenger.sent[0].text
        assert probe.calls == []

    async def test_an_unconfigured_egress_counts_as_none(self) -> None:
        messenger = FakeMessenger()
        handlers, _ = build(messenger, egress=FakeEgress(configured=False))

        await handle(handlers, message_update("/vpn"))

        assert "لا يوجد نفق" in messenger.sent[0].text

    def test_vpn_is_a_source_submission_for_access_purposes(self) -> None:
        assert COMMAND_ACTIONS["vpn"] is Action.SUBMIT_SOURCE
        assert "/vpn" in formatters.render_help()


class TestVpnCountry:
    async def test_a_country_word_pins_the_host_to_that_proton_exit(self) -> None:
        messenger = FakeMessenger()
        egress = FakeEgress(proton=("nl", "pl", "ro"))
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe, egress=egress)

        await handle(handlers, message_update("/vpn NL https://example.com/a"))

        assert egress.pinned == [("example.com", "proton:nl")]
        assert probe.calls == ["https://example.com/a"]
        assert "Proton" in messenger.sent[0].text
        assert "هولندا" in messenger.sent[0].text

    async def test_a_country_without_the_third_tier_is_refused_clearly(self) -> None:
        messenger = FakeMessenger()
        egress = FakeEgress()
        probe = FakeProbe()
        handlers, _ = build(messenger, probe=probe, egress=egress)

        await handle(handlers, message_update("/vpn pl https://example.com/a"))

        assert "Proton" in messenger.sent[0].text
        assert egress.pinned == []
        assert probe.calls == []

    async def test_a_country_word_without_a_link_gets_the_help(self) -> None:
        messenger = FakeMessenger()
        handlers, _ = build(messenger, egress=FakeEgress(proton=("nl",)))

        await handle(handlers, message_update("/vpn nl"))

        assert "/vpn" in messenger.sent[0].text

    async def test_the_route_list_shows_tiers_and_the_countries_on_offer(self) -> None:
        messenger = FakeMessenger()
        egress = FakeEgress(proton=("nl", "pl", "ro"))
        egress.pin("geo.example", "proton:ro")
        handlers, _ = build(messenger, egress=egress)

        await handle(handlers, message_update("/vpn"))

        text = messenger.sent[0].text
        assert "• blocked.example — WARP" in text
        assert "• geo.example — Proton (رومانيا)" in text
        assert "nl هولندا" in text


# --------------------------------------------------------------------------- #
# A failed probe leaves a trace                                                #
# --------------------------------------------------------------------------- #


class TestProbeFailuresAreLogged:
    """The reply is the only record otherwise, and it lives in one chat."""

    async def test_the_code_and_the_host_are_logged_and_the_url_is_not(self) -> None:
        written: list[str] = []
        handle = logger.add(written.append, format="{message} {extra}", level="WARNING")
        try:
            messenger = FakeMessenger()
            handlers, _ = build(
                messenger,
                probe=FakeProbe(error=NoPlayableMediaError("nothing here")),
            )

            intent = parse_update(message_update("https://example.com/watch?v=secret-id-123"))
            assert intent is not None
            await handlers.handle(intent)
        finally:
            logger.remove(handle)

        lines = [line for line in written if "Probe failed" in line]
        assert len(lines) == 1
        assert "'code': 'no_playable_media'" in lines[0]
        assert "'host': 'example.com'" in lines[0]
        assert "'stage': 'probe'" in lines[0]
        assert "secret-id-123" not in lines[0], "the URL itself never reaches the log"
        # And the person still got their explanation.
        assert messenger.sent, "the reply is unchanged by the log line"

    async def test_a_successful_probe_logs_no_failure(self) -> None:
        written: list[str] = []
        handle = logger.add(written.append, format="{message}", level="WARNING")
        try:
            handlers, _ = build(FakeMessenger())
            intent = parse_update(message_update("https://example.com/a"))
            assert intent is not None
            await handlers.handle(intent)
        finally:
            logger.remove(handle)

        assert not any("Probe failed" in line for line in written)


# --------------------------------------------------------------------------- #
# The poll loop                                                                #
# --------------------------------------------------------------------------- #


class TestGatewayLoop:
    async def test_dispatches_a_batch_and_advances_the_offset(self) -> None:
        messenger = FakeMessenger(batches=[[message_update("/help", update_id=11)]])
        handlers, _ = build(messenger)
        gateway = TelegramGateway(messenger, handlers)

        handled = await gateway.poll_once()
        await gateway.poll_once()

        assert handled == 1
        assert messenger.poll_calls == [None, 12]

    async def test_a_redelivered_update_is_handled_once(self) -> None:
        duplicate = message_update("/help", update_id=11)
        messenger = FakeMessenger(batches=[[duplicate], [duplicate]])
        handlers, _ = build(messenger)
        gateway = TelegramGateway(messenger, handlers)

        await gateway.poll_once()
        await gateway.poll_once()
        await gateway.settle()

        assert len(messenger.sent) == 1

    async def test_an_unparsable_update_still_advances_the_offset(self) -> None:
        messenger = FakeMessenger(batches=[[{"update_id": 41, "channel_post": {}}]])
        handlers, _ = build(messenger)
        gateway = TelegramGateway(messenger, handlers)

        handled = await gateway.poll_once()
        await gateway.poll_once()

        assert handled == 0
        assert messenger.poll_calls[-1] == 42, "a poison update must not be retried forever"

    async def test_a_transport_failure_does_not_stop_the_loop(self) -> None:
        class BrokenThenFine(FakeMessenger):
            def __init__(self) -> None:
                super().__init__(batches=[[message_update("/help", update_id=1)]])
                self.failed = False

            async def get_updates(
                self, *, offset: int | None = None, timeout: int = 30
            ) -> Sequence[Mapping[str, Any]]:
                if not self.failed:
                    self.failed = True
                    message = "network down"
                    raise ConnectionError(message)
                return await super().get_updates(offset=offset, timeout=timeout)

        messenger = BrokenThenFine()
        handlers, _ = build(messenger)
        gateway = TelegramGateway(messenger, handlers)

        task = asyncio.create_task(gateway.run())
        await asyncio.sleep(0.05)
        gateway.stop()
        await asyncio.wait_for(task, timeout=6)

        assert messenger.failed

    async def test_stopping_drains_in_flight_work(self) -> None:
        messenger = FakeMessenger()
        handlers, _ = build(messenger)
        gateway = TelegramGateway(messenger, handlers)
        gateway.stop()

        await asyncio.wait_for(gateway.run(), timeout=5)

        assert handlers.pending_tasks == 0


@pytest.mark.parametrize("action", list(CallbackAction))
def test_callback_payloads_the_gateway_builds_are_ones_it_accepts(
    action: CallbackAction,
) -> None:
    """Encoding and decoding are each other's inverse for every action."""
    payload = CallbackPayload(action=action, token="abc123", choice="best")

    assert decode_callback(payload.encode()) == payload


class TestCookieUpload:
    """An owner keeps the bot signed in by sending it a file.

    The alternative is an SSH session every few weeks, which is why this exists
    at all: a jar that is inconvenient to refresh is a jar that expires and
    stays expired.
    """

    async def test_an_uploaded_jar_is_installed(self) -> None:
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        handlers, _ = build(messenger)

        await handle(handlers, document_update())

        assert "تم تحديث الكوكيز" in messenger.last_text
        assert "x.com" in messenger.last_text

    async def test_the_upload_is_deleted_from_the_conversation(self) -> None:
        """A jar left in a chat is a live session in Telegram's history."""
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        handlers, _ = build(messenger)

        await handle(handlers, document_update(message_id=9))

        assert messenger.deleted == [("4242", 9)]

    async def test_when_telegram_refuses_the_deletion_the_user_is_told(self) -> None:
        """Bots may only delete for a limited window; silence would be worse."""
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        messenger.delete_allowed = False
        handlers, _ = build(messenger)

        await handle(handlers, document_update())

        assert "احذفه بنفسك" in messenger.last_text

    async def test_a_file_that_is_not_a_jar_is_refused_with_a_usable_message(self) -> None:
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = b"a screenshot, probably"
        handlers, _ = build(messenger)

        await handle(handlers, document_update())

        assert "Netscape" in messenger.last_text

    async def test_an_oversized_file_is_refused_before_it_is_fetched(self) -> None:
        """The declared size is checked first, so a big upload costs no bytes."""
        messenger = FakeMessenger()
        handlers, _ = build(messenger)

        await handle(handlers, document_update(file_size=50 * 1024 * 1024))

        assert "الحد المسموح" in messenger.last_text
        assert messenger.files == {}, "nothing should have been fetched"

    async def test_the_jar_is_never_echoed_back(self) -> None:
        """The reply describes the jar; it must never quote one."""
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        handlers, _ = build(messenger)

        await handle(handlers, document_update())

        assert "secret-value-here" not in " ".join(messenger.texts())

    async def test_cookies_command_reports_what_is_stored(self) -> None:
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        handlers, _ = build(messenger)
        await handle(handlers, document_update())

        await handle(handlers, message_update("/cookies", update_id=20))

        assert "الكوكيز المحفوظة" in messenger.last_text

    async def test_cookies_clear_removes_them(self) -> None:
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        handlers, _ = build(messenger)
        await handle(handlers, document_update())

        await handle(handlers, message_update("/cookies clear", update_id=21))

        assert "حُذفت" in messenger.last_text


class TestFailureExplainsItself:
    """A refusal from a site that needs a session must say so.

    "I could not read that link" reads as a broken bot and gives the user
    nothing to act on, when thirty seconds of exporting cookies would fix it.
    """

    async def test_a_session_site_failure_suggests_cookies(self) -> None:
        messenger = FakeMessenger()
        probe = FakeProbe(error=MetadataUnavailableError("nope"))
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update("https://x.com/someone/status/123"))

        assert "لا توجد كوكيز محفوظة" in messenger.last_text

    async def test_with_cookies_stored_it_suggests_they_lapsed(self) -> None:
        """A different cause needs a different action."""
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        probe = FakeProbe(error=MetadataUnavailableError("nope"))
        handlers, _ = build(messenger, probe=probe)
        await handle(handlers, document_update())

        await handle(handlers, message_update("https://x.com/someone/status/123", update_id=30))

        assert "وليس x.com" in messenger.last_text or "توقّفت عن العمل" in messenger.last_text

    async def test_a_jar_that_covers_the_site_but_holds_no_session_says_so(self) -> None:
        """The failure that otherwise has no explanation at all.

        The jar lists x.com, reports a healthy cookie count and contains
        nothing that says who you are - which is what an export that skipped
        httpOnly cookies produces. Everything looks right and nothing works, and
        no other message in the product would tell you why.
        """
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = (
            b"# Netscape HTTP Cookie File\n"
            b"x.com\tTRUE\t/\tTRUE\t2000000000\tguest_id\tv1%3A123456789\n"
        )
        probe = FakeProbe(error=MetadataUnavailableError("nope"))
        handlers, _ = build(messenger, probe=probe)
        await handle(handlers, document_update())

        await handle(handlers, message_update("https://x.com/someone/status/123", update_id=31))

        assert "لا تحتوي على جلسة دخول" in messenger.last_text
        assert "httpOnly" in messenger.last_text

    async def test_a_photo_only_post_is_not_blamed_on_cookies(self) -> None:
        """A signed-in jar means the post really is just photos - say that."""
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        probe = FakeProbe(error=NoPlayableMediaError("no video could be found in this tweet"))
        handlers, _ = build(messenger, probe=probe)
        await handle(handlers, document_update())

        await handle(handlers, message_update("https://x.com/someone/status/123", update_id=32))

        text = messenger.last_text.lower()
        assert "صور فقط" in text
        assert "cookies.txt" not in text, "a signed-in session makes cookie advice noise"

    async def test_an_ordinary_site_gets_no_cookie_advice(self) -> None:
        """Advice that appears everywhere is advice nobody reads."""
        messenger = FakeMessenger()
        probe = FakeProbe(error=MetadataUnavailableError("nope"))
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update("https://example.com/talk.mp4"))

        assert "cookies" not in messenger.last_text.lower()

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.tiktok.com/@a/video/1",
            "https://vm.tiktok.com/ABC/",
            "https://twitter.com/a/status/1",
            "https://www.instagram.com/reel/abc/",
        ],
        ids=["tiktok", "tiktok-short", "twitter", "instagram"],
    )
    async def test_the_other_session_sites_are_recognised(self, url: str) -> None:
        messenger = FakeMessenger()
        probe = FakeProbe(error=MetadataUnavailableError("nope"))
        handlers, _ = build(messenger, probe=probe)

        await handle(handlers, message_update(url))

        assert "cookies.txt" in messenger.last_text

    async def test_the_confirmation_is_not_a_reply_to_the_deleted_upload(self) -> None:
        """Telegram answers a reply to a missing message with 400.

        The upload is deleted on purpose, so replying to it made the
        confirmation fail and told the user everything had gone wrong after it
        had in fact succeeded.
        """
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        handlers, _ = build(messenger)

        await handle(handlers, document_update(message_id=9))

        confirmation = messenger.sent[-1]
        assert "تم تحديث الكوكيز" in confirmation.text
        assert confirmation.reply_to_message_id is None

    async def test_it_still_replies_when_the_upload_survived(self) -> None:
        """With the message still there, threading the answer to it is useful."""
        messenger = FakeMessenger()
        messenger.files["FILE-1"] = COOKIE_JAR
        messenger.delete_allowed = False
        handlers, _ = build(messenger)

        await handle(handlers, document_update(message_id=9))

        assert messenger.sent[-1].reply_to_message_id == 9


class TestConcurrencyIsBounded:
    """Five links pasted in a row must not start five downloads.

    Nothing else bounds this: each acquisition is its own task, and each one
    holds a download, an ffmpeg merge and an upload. On a four-core board that
    is also running other people's containers, unbounded means the other
    containers suffer for a queue nobody asked to be parallel.
    """

    async def test_only_one_acquisition_runs_at_a_time(self) -> None:
        messenger = FakeMessenger()
        acquire = FakeAcquire()
        acquire.hold = True
        handlers, _ = build(messenger, acquire=acquire, auto=True)

        for index in range(3):
            await handle(handlers, message_update(URL, update_id=100 + index))
        await asyncio.sleep(0.05)

        assert len(acquire.commands) == 1, "the second and third must wait"

        acquire.gate.set()
        await asyncio.sleep(0.05)
        assert len(acquire.commands) == 3, "waiting work must still run"

    async def test_a_waiting_request_says_so(self) -> None:
        """Silence is indistinguishable from having dropped the message."""
        messenger = FakeMessenger()
        acquire = FakeAcquire()
        acquire.hold = True
        handlers, _ = build(messenger, acquire=acquire, auto=True)

        await handle(handlers, message_update(URL, update_id=200))
        await handle(handlers, message_update(URL, update_id=201))
        await asyncio.sleep(0.05)

        assert any("في الانتظار" in text for text in messenger.texts())

        acquire.gate.set()
        await asyncio.sleep(0.05)
