"""Update parsing, keyboards, sessions and formatting.

All four are pure, and all four sit directly on the boundary with an untrusted
system, so they are tested against malformed and hostile input rather than
happy paths alone.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from mediahub.application.download.dto import (
    AcquisitionSummary,
    CapabilitiesSummary,
    HistoryEntrySummary,
    QualityOption,
    SourceSummary,
)
from mediahub.application.download.ports import DownloadProgress, DownloadStage
from mediahub.domain.media.enums import MediaType
from mediahub.presentation.telegram import formatters
from mediahub.presentation.telegram.keyboards import (
    MAX_CALLBACK_BYTES,
    CallbackAction,
    CallbackPayload,
    abort_keyboard,
    decode_callback,
    quality_keyboard,
)
from mediahub.presentation.telegram.sessions import SessionStore
from mediahub.presentation.telegram.updates import IntentKind, parse_update
from tests.support.telegram_fakes import callback_update, message_update

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def summary(**overrides: object) -> SourceSummary:
    defaults: dict[str, object] = {
        "url": "https://example.com/a",
        "provider": "testsite",
        "title": "A Test Video",
        "kind": MediaType.VIDEO,
        "is_live": False,
        "is_playlist": False,
        "qualities": (
            QualityOption(key="best", label="Best available", approx_bytes=12_000_000),
            QualityOption(key="h720", label="720p", height=720),
        ),
        "duration_seconds": 125.0,
    }
    defaults.update(overrides)
    return SourceSummary(**defaults)  # type: ignore[arg-type]


class TestUpdateParsing:
    def test_parses_a_command(self) -> None:
        intent = parse_update(message_update("/help"))

        assert intent is not None
        assert intent.kind is IntentKind.COMMAND
        assert intent.command == "help"
        assert intent.sender.user_id == "4242"
        assert intent.chat_id == "4242"

    def test_strips_the_bot_suffix_from_a_group_command(self) -> None:
        intent = parse_update(message_update("/history@MediaHubBot 5"))

        assert intent is not None
        assert intent.command == "history"
        assert intent.argument == "5"

    def test_parses_plain_text(self) -> None:
        intent = parse_update(message_update("https://example.com/a"))

        assert intent is not None
        assert intent.kind is IntentKind.TEXT
        assert intent.text == "https://example.com/a"

    def test_parses_a_callback(self) -> None:
        intent = parse_update(callback_update("q|abc123|h720"))

        assert intent is not None
        assert intent.kind is IntentKind.CALLBACK
        assert intent.callback_data == "q|abc123|h720"
        assert intent.callback_id == "cb-1"

    def test_ignores_messages_from_bots(self) -> None:
        assert parse_update(message_update("hello", is_bot=True)) is None

    @pytest.mark.parametrize(
        "update",
        [
            {},
            {"update_id": "not-an-int", "message": {}},
            {"update_id": 1},
            {"update_id": 1, "message": {"text": "hi"}},
            {"update_id": 1, "message": {"from": {"id": 1}, "text": "hi"}},
            {"update_id": 1, "edited_message": {"text": "hi"}},
            {"update_id": 1, "channel_post": {"text": "hi"}},
            {"update_id": 1, "message": {"from": {}, "chat": {}, "text": ""}},
        ],
    )
    def test_unparsable_updates_are_ignored_not_fatal(self, update: dict[str, object]) -> None:
        assert parse_update(update) is None

    def test_oversized_text_is_capped(self) -> None:
        intent = parse_update(message_update("x" * 100_000))

        assert intent is not None
        assert intent.text is not None
        assert len(intent.text) <= 4096

    def test_a_boolean_id_is_not_an_id(self) -> None:
        update = message_update("hi")
        update["update_id"] = True

        assert parse_update(update) is None


class TestKeyboards:
    def test_round_trips_a_payload(self) -> None:
        payload = CallbackPayload(
            action=CallbackAction.CHOOSE_QUALITY, token="abc123", choice="h720"
        )

        decoded = decode_callback(payload.encode())

        assert decoded == payload

    def test_refuses_to_build_an_oversized_payload(self) -> None:
        payload = CallbackPayload(
            action=CallbackAction.CHOOSE_QUALITY, token="a" * 80, choice="b" * 80
        )

        with pytest.raises(ValueError, match="64"):
            payload.encode()

    def test_every_generated_button_fits(self) -> None:
        options = tuple(
            QualityOption(key=f"h{height}", label=f"{height}p", approx_bytes=10**9)
            for height in (2160, 1440, 1080, 720, 480, 360)
        )

        markup = quality_keyboard(options, token="abcdef012345")

        for row in markup["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode()) <= MAX_CALLBACK_BYTES

    def test_a_cancel_button_is_always_offered(self) -> None:
        markup = quality_keyboard((QualityOption(key="best", label="Best"),), token="t1")

        labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
        assert "إلغاء" in labels

    def test_abort_keyboard_has_one_button(self) -> None:
        markup = abort_keyboard("t1")

        assert len(markup["inline_keyboard"]) == 1
        assert markup["inline_keyboard"][0][0]["text"] == "إيقاف"

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "garbage",
            "q|abc",
            "q|abc|h720|extra",
            "z|abc|h720",
            "q||h720",
            "q|abc/../|h720",
            "q|abc|../../etc/passwd",
            "q|abc|" + "x" * 60,
        ],
    )
    def test_malformed_payloads_are_ignored(self, raw: str | None) -> None:
        assert decode_callback(raw) is None

    def test_size_hints_are_rendered_compactly(self) -> None:
        markup = quality_keyboard(
            (QualityOption(key="best", label="Best", approx_bytes=2_000_000_000),),
            token="t1",
        )

        assert "GB" in markup["inline_keyboard"][0][0]["text"]


class TestSessionStore:
    def test_creates_and_retrieves(self) -> None:
        store = SessionStore()

        session = store.create(owner="telegram:1", chat_id="1", summary=summary())

        assert store.get(session.token, owner="telegram:1") is session

    def test_another_principal_cannot_use_the_token(self) -> None:
        store = SessionStore()
        session = store.create(owner="telegram:1", chat_id="1", summary=summary())

        assert store.get(session.token, owner="telegram:2") is None

    def test_expired_sessions_are_forgotten(self) -> None:
        store = SessionStore(ttl_seconds=0.01)
        session = store.create(owner="telegram:1", chat_id="1", summary=summary())
        time.sleep(0.02)

        assert store.get(session.token, owner="telegram:1") is None

    def test_unknown_tokens_return_nothing(self) -> None:
        assert SessionStore().get("nope", owner="telegram:1") is None

    def test_capacity_is_bounded(self) -> None:
        store = SessionStore(capacity=5)

        for _ in range(50):
            store.create(owner="telegram:1", chat_id="1", summary=summary())

        assert len(store) <= 5

    def test_tokens_are_unguessable_and_alphanumeric(self) -> None:
        store = SessionStore()

        tokens = {
            store.create(owner="telegram:1", chat_id="1", summary=summary()).token
            for _ in range(20)
        }

        assert len(tokens) == 20
        assert all(token.isalnum() for token in tokens)

    def test_discard_is_idempotent(self) -> None:
        store = SessionStore()
        session = store.create(owner="telegram:1", chat_id="1", summary=summary())

        store.discard(session.token)
        store.discard(session.token)

        assert store.get(session.token, owner="telegram:1") is None


class TestFormatters:
    def test_renders_a_source_card(self) -> None:
        text = formatters.render_source(summary())

        assert "A Test Video" in text
        assert "testsite" in text
        assert "2د 05ث" in text
        assert "اختر الجودة" in text

    def test_a_live_source_says_so(self) -> None:
        text = formatters.render_source(summary(is_live=True))

        assert "بث مباشر" in text
        assert "اختر الجودة" not in text

    def test_a_playlist_says_so(self) -> None:
        text = formatters.render_source(summary(is_playlist=True))

        assert "قائمة" in text

    def test_titles_are_shown_verbatim_as_plain_text(self) -> None:
        """No parse mode is ever set, so nothing needs escaping - or wrapping.

        The old ``*title*`` header was rendered by Telegram exactly as typed,
        asterisks included, on every message the bot ever sent.
        """
        title = "*bold* _under_ `code` [link]"
        text = formatters.render_source(summary(title=title))

        assert text.startswith(title)
        assert not text.startswith("*" + title)

    def test_no_message_wraps_its_header_in_asterisks(self) -> None:
        texts = [
            formatters.render_source(summary(title="Plain")),
            formatters.render_queued("Plain"),
            formatters.render_progress(
                DownloadProgress(stage=DownloadStage.DOWNLOADING, downloaded_bytes=1), title="Plain"
            ),
            formatters.render_history(()),
        ]

        assert all("*Plain*" not in text for text in texts)

    def test_long_titles_are_clipped(self) -> None:
        text = formatters.render_source(summary(title="x" * 500))

        assert len(text) < 400

    def test_renders_progress_with_a_bar(self) -> None:
        text = formatters.render_progress(
            DownloadProgress(
                stage=DownloadStage.DOWNLOADING,
                downloaded_bytes=512,
                total_bytes=1024,
                speed_bps=2048,
                eta_seconds=30,
            ),
            title="A",
        )

        assert "50%" in text
        assert "█" in text
        assert "يتبقّى ~30ث" in text

    def test_progress_without_a_total_still_renders(self) -> None:
        text = formatters.render_progress(
            DownloadProgress(stage=DownloadStage.DOWNLOADING, downloaded_bytes=1024),
            title="A",
        )

        assert "1.0 KiB" in text

    def test_renders_a_delivery_confirmation(self) -> None:
        text = formatters.render_delivered(
            AcquisitionSummary(
                url="https://example.com/a",
                provider="testsite",
                title="A Test Video",
                quality_label="720p",
                bytes_delivered=5_000_000,
                elapsed_seconds=42.0,
                remote_id="R",
                delivered_at=NOW,
            )
        )

        assert "تم الإرسال" in text
        assert "720p" in text
        assert "42ث" in text
        # The extra lines are said only when they happened.
        assert "بدل" not in text
        assert "كملف" not in text
        assert "نفق" not in text

    def _delivered(self, **overrides: object) -> AcquisitionSummary:
        base: dict[str, object] = {
            "url": "https://example.com/a",
            "provider": "testsite",
            "title": "A Test Video",
            "quality_label": "720p",
            "bytes_delivered": 5_000_000,
            "elapsed_seconds": 42.0,
            "remote_id": "R",
            "delivered_at": NOW,
        }
        base.update(overrides)
        return AcquisitionSummary(**base)  # type: ignore[arg-type]

    def test_says_when_a_better_rung_was_skipped_for_size(self) -> None:
        text = formatters.render_delivered(self._delivered(capped_from="1080p"))

        assert "720p بدل 1080p" in text

    def test_says_when_the_video_went_as_a_file(self) -> None:
        text = formatters.render_delivered(self._delivered(sent_as_document=True))

        assert "أُرسل كملف" in text
        assert "VP9/AV1" in text

    def test_says_when_the_proxy_was_used(self) -> None:
        text = formatters.render_delivered(self._delivered(via_proxy=True))

        assert "نفق الخروج" in text

    def test_delivery_confirmation_has_no_markdown(self) -> None:
        text = formatters.render_delivered(
            self._delivered(capped_from="1080p", sent_as_document=True, via_proxy=True)
        )

        assert "*" not in text
        assert "_" not in text
        assert "`" not in text

    def test_renders_empty_history(self) -> None:
        assert "لم تحمّل شيئًا" in formatters.render_history(())

    def test_renders_history_entries(self) -> None:
        text = formatters.render_history(
            (
                HistoryEntrySummary(
                    title="A",
                    url="https://example.com/a",
                    provider="testsite",
                    quality_label="720p",
                    bytes_delivered=1024,
                    delivered_at=NOW,
                ),
            )
        )

        assert "2026-01-01" in text

    def test_renders_settings(self) -> None:
        text = formatters.render_settings(
            CapabilitiesSummary(
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
        )

        assert "yt-dlp" in text
        assert "50.0 MiB" in text

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            # Each pair is a code and a phrase that must survive translation:
            # the message has to name the cause, not merely report a failure.
            ("metadata_unavailable", "خاصًّا"),
            ("artifact_too_large", "أكبر"),
            ("permission_denied", "غير مصرّح"),
            ("download_cancelled", "تمّ الإلغاء"),
            ("no_playable_media", "صور فقط"),
            ("connection_blocked", "حجب في الشبكة"),
        ],
    )
    def test_known_error_codes_get_useful_text(self, code: str, expected: str) -> None:
        assert expected in formatters.render_error(code)

    def test_unknown_error_codes_get_a_generic_message(self) -> None:
        assert formatters.render_error("something_new") == formatters.GENERIC_ERROR

    def test_no_error_message_leaks_internals(self) -> None:
        # Every curated message is checked, because this is the one place a
        # path, a URL or a provider's own wording could reach a chat.
        forbidden = ("://", "\\", "/data", "/workspace", "yt-dlp", "Traceback", "Exception")
        for message in formatters.ERROR_MESSAGES.values():
            assert not any(token in message for token in forbidden), message
