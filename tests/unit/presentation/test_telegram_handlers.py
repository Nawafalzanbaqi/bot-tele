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

from mediahub.application.access.ports import Principal
from mediahub.application.common.errors import PermissionDeniedError
from mediahub.application.delivery.ports import DeliveryProgress, DeliveryStage
from mediahub.application.download.dto import (
    AcquisitionSummary,
    CapabilitiesSummary,
    HistoryEntrySummary,
    QualityOption,
    SourceSummary,
)
from mediahub.application.download.errors import MetadataUnavailableError
from mediahub.application.download.ports import DownloadProgress, DownloadStage
from mediahub.domain.access.enums import Role
from mediahub.domain.media.enums import MediaType
from mediahub.presentation.telegram.gateway import TelegramGateway
from mediahub.presentation.telegram.handlers import GatewayServices, TelegramHandlers
from mediahub.presentation.telegram.keyboards import (
    CallbackAction,
    CallbackPayload,
    decode_callback,
)
from mediahub.presentation.telegram.progress import ProgressPresenter
from mediahub.presentation.telegram.sessions import SessionStore
from mediahub.presentation.telegram.updates import parse_update
from tests.support.telegram_fakes import FakeMessenger, callback_update, message_update

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


def build(
    messenger: FakeMessenger,
    *,
    authorize: FakeAuthorize | None = None,
    probe: FakeProbe | None = None,
    acquire: FakeAcquire | None = None,
    history: FakeHistory | None = None,
) -> tuple[TelegramHandlers, GatewayServices]:
    services = GatewayServices(
        messenger=messenger,
        authorize=authorize or FakeAuthorize(),  # type: ignore[arg-type]
        probe_source=probe or FakeProbe(),  # type: ignore[arg-type]
        acquire_media=acquire or FakeAcquire(),  # type: ignore[arg-type]
        get_history=history or FakeHistory(),  # type: ignore[arg-type]
        describe_capabilities=FakeCapabilities(),  # type: ignore[arg-type]
        sessions=SessionStore(),
        progress_interval_seconds=0.01,
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

        assert "Hello" in messenger.last_text
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


# --------------------------------------------------------------------------- #
# The URL flow                                                                 #
# --------------------------------------------------------------------------- #


class TestUrlFlow:
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
        assert labels == ["Best available", "720p", "Cancel"]

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
        assert "private" in messenger.last_text.lower()


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
        assert "Sent" in messenger.last_text

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
        assert any("Sending · " in edit.text and "75%" in edit.text for edit in messenger.edits)

    async def test_cancel_dismisses_the_prompt(self, messenger: FakeMessenger) -> None:
        handlers, services = build(messenger)
        token = await start_session(handlers, services, messenger)

        await handle(handlers, callback_update(f"d|{token}|x"))

        assert messenger.edits[-1].text == "Cancelled."
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
        assert "expired" in messenger.answers[-1][1]

    async def test_a_malformed_payload_is_ignored(self, messenger: FakeMessenger) -> None:
        handlers, _ = build(messenger)

        await handle(handlers, callback_update("!!!"))

        assert "no longer valid" in (messenger.answers[-1][1] or "")

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
        assert "went wrong" in messenger.last_text


# --------------------------------------------------------------------------- #
# Authorisation                                                                #
# --------------------------------------------------------------------------- #


class TestAuthorisation:
    async def test_a_denied_sender_gets_one_refusal_and_nothing_else(
        self, messenger: FakeMessenger
    ) -> None:
        probe = FakeProbe()
        handlers, _ = build(messenger, authorize=FakeAuthorize(allowed=False), probe=probe)

        await handle(handlers, message_update(URL))

        assert probe.calls == [], "a denied sender must not reach any use case"
        assert "not authorised" in messenger.last_text

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
        assert "Sending · " in messenger.edits[0].text
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
