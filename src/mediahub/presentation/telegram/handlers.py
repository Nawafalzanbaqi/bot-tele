"""One handler per intent. Each does exactly four things.

Receive, authorise, call **one** application command, format the answer.

There is no business logic here and there must never be. Every decision this
module appears to make is really a decision made elsewhere: which qualities to
offer comes from the application, whether a URL is acceptable comes from the
domain's URL policy inside the engine, whether the caller may act comes from
the access policy. What is left is translation, which is the whole job of an
adapter.

The one thing the gateway genuinely owns is *conversation*: which message to
edit, which buttons to show, and when to stop talking.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.access.use_cases.authorize_principal import AuthorizeQuery
from mediahub.application.common.cancellation import CancellationSource
from mediahub.application.common.errors import ApplicationError
from mediahub.application.delivery.ports import DeliveryTarget, TargetAddress
from mediahub.application.download.dto import (
    AcquireMediaCommand,
    GetHistoryQuery,
    ProbeSourceQuery,
)
from mediahub.application.download.quality import AUTO_KEY
from mediahub.domain.access.enums import Action
from mediahub.domain.common.errors import DomainError
from mediahub.presentation.telegram import formatters
from mediahub.presentation.telegram.keyboards import (
    CallbackAction,
    abort_keyboard,
    decode_callback,
    quality_keyboard,
)
from mediahub.presentation.telegram.progress import ProgressPresenter
from mediahub.presentation.telegram.updates import IntentKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Coroutine

    from mediahub.application.access.ports import Principal
    from mediahub.application.access.use_cases.authorize_principal import (
        AuthorizePrincipal,
    )
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
    from mediahub.presentation.telegram.api import TelegramMessenger
    from mediahub.presentation.telegram.sessions import Session, SessionStore
    from mediahub.presentation.telegram.updates import Intent

IDENTITY_SCHEME: Final[str] = "telegram"

DELIVERY_PROVIDER: Final[str] = "telegram"
"""Which delivery provider owns the destinations this gateway constructs."""

CHAT_FIELD: Final[str] = "chat"
"""Key under which a conversation is carried in a target address.

Shared by convention with
:mod:`mediahub.infrastructure.delivery.telegram.provider`, which reads it. The
two adapters must not import each other, so a contract test asserts that a
target built here is one that provider accepts.
"""

URL_PREFIXES: Final[tuple[str, ...]] = ("http://", "https://")

MAX_UPLOAD_BYTES: Final[int] = 2 * 1024 * 1024
"""Ceiling on a file the gateway will read into memory.

A cookie jar is a few kilobytes. This is generous enough that a real export
never hits it and small enough that no upload can be a memory attack on a 4 GB
device - which matters because, unlike a download, this arrives on the poll
loop rather than in a worker with a lease and a budget.
"""


@dataclass(frozen=True, slots=True)
class GatewayServices:
    """Everything the handlers are allowed to reach.

    All application use cases and one messenger. Notably absent: repositories,
    the download engine, the workspace, any policy, any domain object.
    """

    messenger: TelegramMessenger
    authorize: AuthorizePrincipal
    probe_source: ProbeSource
    acquire_media: AcquireMedia
    get_history: GetHistory
    describe_capabilities: DescribeCapabilities
    sessions: SessionStore
    install_cookies: InstallCookies
    describe_cookies: DescribeCookies
    discard_cookies: DiscardCookies
    progress_interval_seconds: float = 3.0
    history_limit: int = 10
    auto_best_quality: bool = False
    """Start immediately at the best deliverable quality, with no menu."""


class TelegramHandlers:
    """Routes intents to handlers and keeps long operations off the poll loop."""

    def __init__(self, services: GatewayServices) -> None:
        """Bind the handlers to their services."""
        self._services = services
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending_tasks(self) -> int:
        """Return how many acquisitions are still running."""
        return len(self._tasks)

    async def handle(self, intent: Intent) -> None:
        """Authorise the sender, then dispatch.

        Every failure below is rendered from an error *code*, never from an
        error message: messages may carry a URL, a path or a provider's own
        wording, and none of that belongs in a chat.
        """
        try:
            principal = await self._authorise(intent)
            await self._dispatch(intent, principal)
        except (DomainError, ApplicationError) as exc:
            await self._reply_error(intent, exc.code)
        except Exception:
            logger.opt(exception=True).error("Unhandled failure while serving an update")
            await self._reply_error(intent, "unexpected")

    async def drain(self, *, timeout: float = 30.0) -> None:
        """Wait for running acquisitions to finish, for a graceful shutdown."""
        if not self._tasks:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout)

    # -- Authorisation -------------------------------------------------------

    async def _authorise(self, intent: Intent) -> Principal:
        """Resolve the sender to a principal, or refuse."""
        return await self._services.authorize.execute(
            AuthorizeQuery(
                scheme=IDENTITY_SCHEME,
                external_id=intent.sender.user_id,
                action=_action_for(intent),
                display_name=intent.sender.display_name,
            )
        )

    # -- Dispatch ------------------------------------------------------------

    async def _dispatch(self, intent: Intent, principal: Principal) -> None:
        """Send an authorised intent to its handler."""
        if intent.kind is IntentKind.CALLBACK:
            await self._on_callback(intent, principal)
            return
        if intent.kind is IntentKind.COMMAND:
            await self._on_command(intent, principal)
            return
        if intent.kind is IntentKind.DOCUMENT:
            await self._on_document(intent, principal)
            return
        await self._on_text(intent, principal)

    async def _on_command(self, intent: Intent, principal: Principal) -> None:
        """Handle a slash command."""
        if intent.command == "start":
            await self._say(intent, formatters.render_start(principal.display_name))
        elif intent.command == "help":
            await self._say(intent, formatters.render_help())
        elif intent.command == "settings":
            capabilities = await self._services.describe_capabilities.execute()
            await self._say(intent, formatters.render_settings(capabilities))
        elif intent.command == "history":
            entries = await self._services.get_history.execute(
                GetHistoryQuery(principal=principal.identity, limit=self._services.history_limit)
            )
            await self._say(intent, formatters.render_history(entries))
        elif intent.command == "cookies":
            await self._on_cookies(intent, principal)
        else:
            await self._say(intent, formatters.render_help())

    async def _on_cookies(self, intent: Intent, principal: Principal) -> None:
        """Report or remove the stored cookie jar.

        Owner-only by the same route as uploading one: ``/cookies`` maps to
        :attr:`Action.MANAGE_CREDENTIALS`, so a member asking is refused before
        reaching here rather than being told what is installed.
        """
        if (intent.argument or "").strip().lower() in {"clear", "remove", "delete"}:
            removed = await self._services.discard_cookies.execute(
                requested_by=str(principal.identity)
            )
            await self._say(intent, formatters.render_cookies_discarded(removed=removed))
            return
        summary = await self._services.describe_cookies.execute()
        await self._say(intent, formatters.render_cookie_status(summary))

    async def _on_text(self, intent: Intent, principal: Principal) -> None:
        """Treat free text as a candidate source."""
        text = (intent.text or "").strip()
        if not text.lower().startswith(URL_PREFIXES):
            await self._say(intent, formatters.render_help())
            return

        try:
            summary = await self._services.probe_source.execute(ProbeSourceQuery(url=text))
        except (DomainError, ApplicationError) as exc:
            # Caught here rather than left to the generic handler so the reply
            # can name the *likely* cause. On the sites that refuse logged-out
            # visitors, "I could not read that link" reads as a broken bot and
            # gives the user nothing to act on.
            await self._say(intent, await self._explain_failure(exc.code, url=text))
            return

        session = self._services.sessions.create(
            owner=principal.identity, chat_id=intent.chat_id, summary=summary
        )

        offerable = bool(summary.qualities) and not summary.is_live and not summary.is_playlist
        # When the answer to "which quality?" is always "the best one that will
        # fit", asking is a tap that delays every download and can be answered
        # wrong. The keyboard is still built when the deployment wants a choice.
        automatic = offerable and self._services.auto_best_quality
        markup = (
            None
            if automatic or not offerable
            else quality_keyboard(summary.qualities, token=session.token)
        )
        message_id = await self._services.messenger.send_message(
            chat_id=intent.chat_id,
            text=formatters.render_source(summary, automatic=automatic),
            reply_markup=markup,
            reply_to_message_id=intent.message_id,
        )
        session.prompt_message_id = message_id

        if automatic:
            self._spawn(self._acquire(session, principal, AUTO_KEY))
            return
        if not offerable:
            self._services.sessions.discard(session.token)

    async def _explain_failure(self, code: str, *, url: str) -> str:
        """Render a probe failure together with what is likely behind it."""
        cookies = None
        with contextlib.suppress(Exception):
            cookies = await self._services.describe_cookies.execute()
        return formatters.render_source_failure(code, url=url, cookies=cookies)

    async def _on_document(self, intent: Intent, principal: Principal) -> None:
        """Treat an attached file as a cookie jar, and nothing else.

        Authorisation already happened: reaching here means the sender passed
        :attr:`Action.MANAGE_CREDENTIALS`, which only an owner holds.

        The uploaded message is deleted once the jar is stored. A cookie jar
        left in a conversation is a live session sitting in Telegram's history
        and in the notification of every device signed into that account, and
        the person who uploaded it has no reason to keep it there.
        """
        if intent.file_id is None:  # pragma: no cover - parser guarantees one
            return

        declared = intent.file_size or 0
        if declared > MAX_UPLOAD_BYTES:
            await self._say(intent, formatters.render_cookie_too_large(declared, MAX_UPLOAD_BYTES))
            return

        try:
            content = await self._services.messenger.download_file(
                file_id=intent.file_id, max_bytes=MAX_UPLOAD_BYTES
            )
        except Exception:
            logger.opt(exception=True).warning("Could not fetch an uploaded file")
            await self._say(intent, formatters.render_error("upload_failed"))
            return

        summary = await self._services.install_cookies.execute(
            content, requested_by=str(principal.identity)
        )
        removed = await self._forget_upload(intent)
        # Deliberately not a *reply*. The message being replied to is the upload,
        # and it has just been deleted on purpose - Telegram answers a reply to a
        # missing message with 400, so the confirmation would fail and the user
        # would be told the whole thing went wrong after it had succeeded.
        await self._services.messenger.send_message(
            chat_id=intent.chat_id,
            text=formatters.render_cookies_installed(summary, removed=removed),
            reply_to_message_id=None if removed else intent.message_id,
        )

    async def _forget_upload(self, intent: Intent) -> bool:
        """Delete the message that carried a credential, if Telegram allows."""
        if intent.message_id is None:  # pragma: no cover - messages carry one
            return False
        with contextlib.suppress(Exception):
            return await self._services.messenger.delete_message(
                chat_id=intent.chat_id, message_id=intent.message_id
            )
        return False  # pragma: no cover - suppress returns above

    async def _on_callback(self, intent: Intent, principal: Principal) -> None:
        """Handle a button press."""
        payload = decode_callback(intent.callback_data)
        if payload is None:
            await self._acknowledge(intent, "That button is no longer valid.")
            return

        session = self._services.sessions.get(payload.token, owner=principal.identity)
        if session is None:
            await self._acknowledge(intent, "That request has expired. Send the link again.")
            return

        if payload.action is CallbackAction.DISMISS:
            self._services.sessions.discard(session.token)
            await self._acknowledge(intent, "Cancelled.")
            await self._edit(session, "Cancelled.")
            return

        if payload.action is CallbackAction.ABORT:
            if session.cancellation is not None:
                session.cancellation.cancel()
            await self._acknowledge(intent, "Stopping…")
            return

        if payload.choice is None:
            await self._acknowledge(intent, "That button is no longer valid.")
            return

        await self._acknowledge(intent, "Starting…")
        self._spawn(self._acquire(session, principal, payload.choice))

    # -- Acquisition ---------------------------------------------------------

    async def _acquire(self, session: Session, principal: Principal, choice: str) -> None:
        """Run one acquisition, keeping the chat informed as it goes.

        The gateway starts this as its own task so a download that takes an
        hour does not block the poll loop. It is the only concurrency the
        gateway owns, and it disappears the day a queue and a worker take over
        - the command it issues does not change.
        """
        services = self._services
        cancellation = CancellationSource()
        session.cancellation = cancellation
        message_id = session.prompt_message_id

        presenter: ProgressPresenter | None = None
        pump: asyncio.Task[None] | None = None

        if message_id is not None:
            presenter = ProgressPresenter(
                services.messenger,
                chat_id=session.chat_id,
                message_id=message_id,
                title=session.summary.title,
                interval_seconds=services.progress_interval_seconds,
                reply_markup=abort_keyboard(session.token),
            )
            pump = asyncio.create_task(presenter.run())

        try:
            summary = await services.acquire_media.execute(
                AcquireMediaCommand(
                    url=session.summary.url,
                    quality_key=choice,
                    target=DeliveryTarget(
                        provider=DELIVERY_PROVIDER,
                        address=TargetAddress(
                            provider=DELIVERY_PROVIDER,
                            opaque={CHAT_FIELD: session.chat_id},
                        ),
                        label="this chat",
                    ),
                    requested_by=principal.identity,
                    caption=session.summary.title,
                ),
                on_progress=None if presenter is None else presenter.report,
                on_delivery_progress=None if presenter is None else presenter.report_delivery,
                cancellation=cancellation.token,
            )
        except (DomainError, ApplicationError) as exc:
            await self._finish(presenter, pump, session, formatters.render_error(exc.code))
        except Exception:
            logger.opt(exception=True).error("Acquisition failed unexpectedly")
            await self._finish(presenter, pump, session, formatters.render_error("unexpected"))
        else:
            await self._finish(presenter, pump, session, formatters.render_delivered(summary))
        finally:
            services.sessions.discard(session.token)

    async def _finish(
        self,
        presenter: ProgressPresenter | None,
        pump: asyncio.Task[None] | None,
        session: Session,
        text: str,
    ) -> None:
        """Stop reporting progress and replace the message with the outcome."""
        if presenter is not None:
            await presenter.stop()
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        await self._edit(session, text)

    def _spawn(self, coroutine: Coroutine[None, None, None]) -> None:
        """Run a coroutine in the background, keeping a reference to it.

        Without the reference the event loop may garbage-collect the task
        mid-flight, which produces downloads that stop for no visible reason.
        """
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- Replying ------------------------------------------------------------

    async def _say(self, intent: Intent, text: str) -> None:
        """Post a reply in the conversation the intent came from."""
        await self._services.messenger.send_message(
            chat_id=intent.chat_id, text=text, reply_to_message_id=intent.message_id
        )

    async def _edit(self, session: Session, text: str) -> None:
        """Replace the prompt message, falling back to a new one."""
        if session.prompt_message_id is None:
            await self._services.messenger.send_message(chat_id=session.chat_id, text=text)
            return
        try:
            await self._services.messenger.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.prompt_message_id,
                text=text,
                reply_markup=None,
            )
        except Exception:
            logger.opt(exception=True).debug("Could not edit the prompt; posting instead")
            with contextlib.suppress(Exception):
                await self._services.messenger.send_message(chat_id=session.chat_id, text=text)

    async def _acknowledge(self, intent: Intent, text: str) -> None:
        """Answer a button press so the client stops spinning."""
        if intent.callback_id is None:  # pragma: no cover - callbacks always carry one
            return
        with contextlib.suppress(Exception):
            await self._services.messenger.answer_callback(
                callback_id=intent.callback_id, text=text
            )

    async def _reply_error(self, intent: Intent, code: str) -> None:
        """Tell the user something went wrong, in their own conversation."""
        text = formatters.render_error(code)
        with contextlib.suppress(Exception):
            if intent.kind is IntentKind.CALLBACK and intent.callback_id is not None:
                await self._services.messenger.answer_callback(
                    callback_id=intent.callback_id, text=text
                )
                return
            await self._services.messenger.send_message(chat_id=intent.chat_id, text=text)


COMMAND_ACTIONS: Final[dict[str, Action]] = {
    "history": Action.VIEW_HISTORY,
    # Replacing the jar changes who the device fetches as, and reading it back
    # names the accounts it is signed in to. Both are owner-only.
    "cookies": Action.MANAGE_CREDENTIALS,
}
"""Commands needing something other than the mildest action.

A table rather than a chain of conditions, for the same reason the domain's
permissions are one: adding a command means adding a line here, where the
decision is visible, instead of an ``if`` buried in a handler.
"""


def _action_for(intent: Intent) -> Action:
    """Return the access action an intent represents.

    ``/start`` and ``/help`` map to the mildest action rather than to none at
    all: even a greeting must pass the allow-list, or an unknown sender learns
    that the bot exists and answers.
    """
    if intent.kind is IntentKind.CALLBACK:
        payload = decode_callback(intent.callback_data)
        aborting = payload is not None and payload.action is CallbackAction.ABORT
        return Action.CANCEL_ACQUISITION if aborting else Action.SUBMIT_SOURCE
    if intent.kind is IntentKind.DOCUMENT:
        # The only file this gateway accepts is a cookie jar.
        return Action.MANAGE_CREDENTIALS
    if intent.kind is IntentKind.COMMAND:
        return COMMAND_ACTIONS.get(intent.command or "", Action.VIEW_SETTINGS)
    return Action.SUBMIT_SOURCE
