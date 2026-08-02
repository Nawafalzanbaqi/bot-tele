"""Security controls on the Telegram surface.

Three threats from ``docs/architecture/14-security-architecture.md`` §14.2 meet
here: T6 (an unauthorised sender issuing commands), T9 (no record of who asked
for what) and the general rule that an error must never disclose internals.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from mediahub.application.access.ports import AuditEvent, AuditOutcome
from mediahub.application.access.use_cases.authorize_principal import (
    AuthorizePrincipal,
    AuthorizeQuery,
)
from mediahub.application.common.errors import PermissionDeniedError
from mediahub.application.credentials.use_cases.manage_cookies import (
    DescribeCookies,
    DiscardCookies,
    InstallCookies,
)
from mediahub.application.download.dto import SourceSummary
from mediahub.domain.access.enums import Action
from mediahub.domain.access.policies import AllowListPolicy, AuthorizationPolicy
from mediahub.domain.media.enums import MediaType
from mediahub.presentation.telegram import formatters
from mediahub.presentation.telegram.handlers import GatewayServices, TelegramHandlers
from mediahub.presentation.telegram.sessions import SessionStore
from mediahub.presentation.telegram.updates import parse_update
from tests.support.telegram_fakes import (
    FakeCookieStore,
    FakeMessenger,
    callback_update,
    message_update,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = [pytest.mark.security, pytest.mark.unit]

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ALLOWED_USER = 4242
STRANGER = 9999


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


class ExplodingUseCase:
    """Any call is a failure of the allow-list."""

    def __init__(self) -> None:
        self.called = False

    async def execute(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        self.called = True
        message = "an unauthorised sender reached an application use case"
        raise AssertionError(message)


def build(
    audit: RecordingAudit, allowed: Sequence[int]
) -> tuple[TelegramHandlers, FakeMessenger, ExplodingUseCase]:
    """Build a gateway whose use cases refuse to be called."""
    messenger = FakeMessenger()
    tripwire = ExplodingUseCase()
    authorize = AuthorizePrincipal(
        allow_list=AllowListPolicy.from_ids(
            scheme="telegram", owner_ids=tuple(str(user) for user in allowed)
        ),
        authorization=AuthorizationPolicy(),
        audit=audit,
        clock=FrozenClock(),
    )
    cookie_store = FakeCookieStore()
    handlers = TelegramHandlers(
        GatewayServices(
            messenger=messenger,
            authorize=authorize,
            probe_source=tripwire,  # type: ignore[arg-type]
            acquire_media=tripwire,  # type: ignore[arg-type]
            get_history=tripwire,  # type: ignore[arg-type]
            describe_capabilities=tripwire,  # type: ignore[arg-type]
            sessions=SessionStore(),
            install_cookies=InstallCookies(store=cookie_store),
            describe_cookies=DescribeCookies(store=cookie_store),
            discard_cookies=DiscardCookies(store=cookie_store),
        )
    )
    return handlers, messenger, tripwire


async def send(handlers: TelegramHandlers, update: dict[str, Any]) -> None:
    intent = parse_update(update)
    assert intent is not None
    await handlers.handle(intent)


class TestAllowList:
    @pytest.mark.parametrize(
        "update",
        [
            message_update("/start", user_id=STRANGER),
            message_update("/history", user_id=STRANGER),
            message_update("/settings", user_id=STRANGER),
            message_update("https://example.com/a", user_id=STRANGER),
            callback_update("q|abc123|best", user_id=STRANGER),
        ],
    )
    async def test_a_stranger_reaches_no_use_case(self, update: dict[str, Any]) -> None:
        audit = RecordingAudit()
        handlers, _messenger, tripwire = build(audit, [ALLOWED_USER])

        await send(handlers, update)

        assert not tripwire.called
        assert audit.events[-1].outcome is AuditOutcome.DENIED

    async def test_a_stranger_gets_one_refusal_and_no_hints(self) -> None:
        audit = RecordingAudit()
        handlers, messenger, _ = build(audit, [ALLOWED_USER])

        await send(handlers, message_update("/help", user_id=STRANGER))

        assert len(messenger.sent) == 1
        text = messenger.sent[0].text
        assert "غير مصرّح" in text
        assert "/history" not in text, "a refusal must not advertise the command set"

    async def test_an_empty_allow_list_admits_nobody(self) -> None:
        audit = RecordingAudit()
        handlers, _, tripwire = build(audit, [])

        await send(handlers, message_update("/help", user_id=ALLOWED_USER))

        assert not tripwire.called

    async def test_denials_are_audited_with_the_actor_and_action(self) -> None:
        audit = RecordingAudit()
        handlers, _, _ = build(audit, [ALLOWED_USER])

        await send(handlers, message_update("https://example.com/a", user_id=STRANGER))

        event = audit.events[-1]
        assert event.actor == f"telegram:{STRANGER}"
        assert event.action == Action.SUBMIT_SOURCE.value
        assert event.reason
        assert event.occurred_at == NOW

    async def test_bots_cannot_talk_to_the_gateway(self) -> None:
        audit = RecordingAudit()
        _handlers, messenger, _ = build(audit, [ALLOWED_USER])
        update = message_update("/help", user_id=ALLOWED_USER, is_bot=True)

        assert parse_update(update) is None
        assert messenger.sent == []


class TestAuditContent:
    async def test_audit_entries_carry_no_secrets(self) -> None:
        audit = RecordingAudit()
        use_case = AuthorizePrincipal(
            allow_list=AllowListPolicy.from_ids(scheme="telegram", owner_ids=("1",)),
            authorization=AuthorizationPolicy(),
            audit=audit,
            clock=FrozenClock(),
        )

        await use_case.execute(
            AuthorizeQuery(
                scheme="telegram",
                external_id="1",
                action=Action.SUBMIT_SOURCE,
                display_name="Ada",
            )
        )
        with pytest.raises(PermissionDeniedError):
            await use_case.execute(
                AuthorizeQuery(scheme="telegram", external_id="2", action=Action.SUBMIT_SOURCE)
            )

        for event in audit.events:
            rendered = f"{event.actor} {event.action} {event.reason} {event.detail}"
            assert "token" not in rendered.lower()
            assert "/" not in (event.reason or "")


class TestDisclosure:
    def test_no_user_facing_message_carries_internals(self) -> None:
        for code in [*formatters.ERROR_MESSAGES, "unmapped_code"]:
            message = formatters.render_error(code)
            assert "Traceback" not in message
            assert "://" not in message, "a message must never echo a URL"
            assert "\\" not in message
            assert "yt-dlp" not in message, "the engine's identity is not the user's problem"

    def test_titles_cannot_inject_formatting(self) -> None:
        # A source title is attacker-controlled and reaches every message.
        hostile = SourceSummary(
            url="https://example.com/a",
            provider="p",
            title="*](https://evil.example)`",
            kind=MediaType.VIDEO,
            is_live=False,
            is_playlist=False,
            qualities=(),
        )

        rendered = formatters.render_source(hostile)

        assert "`" not in rendered
        assert "](" not in rendered
