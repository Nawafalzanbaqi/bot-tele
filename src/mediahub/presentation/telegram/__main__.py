"""Entry point for the Telegram gateway process.

``python -m mediahub.presentation.telegram``

This is a composition root - one of the few modules allowed to name concrete
adapters - and it is deliberately the only place where the Telegram client, the
delivery provider and the gateway meet. Everything below it receives ports.

It runs as its own process (``docs/architecture/01-product-architecture.md``
§1.3) so that a chatty, rate-limited third-party protocol can crash, back off
and restart without touching the API or an in-flight download.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.infrastructure.delivery.telegram.client import PythonTelegramBotClient
from mediahub.infrastructure.delivery.telegram.provider import TelegramDeliveryProvider
from mediahub.infrastructure.di.container import build_container
from mediahub.presentation.telegram.gateway import TelegramGateway
from mediahub.presentation.telegram.handlers import GatewayServices, TelegramHandlers
from mediahub.presentation.telegram.sessions import SessionStore
from mediahub.shared.config.settings import get_settings
from mediahub.shared.logging.setup import configure_logging

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.shared.config.settings import Settings


def main() -> None:
    """Start the gateway, or refuse to start with a reason."""
    settings = get_settings()
    configure_logging(settings)

    if not settings.telegram.enabled:
        logger.error("telegram.enabled is false; nothing to run")
        raise SystemExit(1)
    if not settings.download.enabled:
        logger.error("download.enabled is false; the gateway would have nothing to do")
        raise SystemExit(1)

    asyncio.run(_run(settings))


async def _run(settings: Settings) -> None:  # pragma: no cover - process wiring
    """Wire everything together and poll until asked to stop."""
    container = build_container(settings)
    await container.prepare()
    telegram = settings.telegram

    client = PythonTelegramBotClient(
        telegram.bot_token.get_secret_value(), api_base_url=telegram.api_base_url
    )
    await client.start()

    provider = TelegramDeliveryProvider(
        client,
        bot_principal=telegram.bot_id,
        local_api_server=telegram.uses_local_api_server,
    )
    router = container.delivery_router(provider)

    handlers = TelegramHandlers(
        GatewayServices(
            messenger=client,
            authorize=container.authorize_principal_use_case(),
            probe_source=container.probe_source_use_case(),
            acquire_media=container.acquire_media_use_case(router),
            get_history=container.get_history_use_case(),
            describe_capabilities=container.describe_capabilities_use_case(router),
            sessions=SessionStore(ttl_seconds=telegram.session_ttl_seconds),
            install_cookies=container.install_cookies_use_case(),
            describe_cookies=container.describe_cookies_use_case(),
            discard_cookies=container.discard_cookies_use_case(),
            progress_interval_seconds=telegram.progress_interval_seconds,
            history_limit=telegram.history_limit,
            auto_best_quality=telegram.auto_best_quality,
            max_concurrent=telegram.max_concurrent_acquisitions,
        )
    )
    gateway = TelegramGateway(
        client,
        handlers,
        poll_timeout_seconds=telegram.poll_timeout_seconds,
        heartbeat_path=telegram.heartbeat_file,
    )

    _install_signal_handlers(gateway)
    logger.bind(
        bot_id=telegram.bot_id,
        allowed=len(container.allow_list.roles),
        destinations=list(router.provider_names),
    ).info("Telegram gateway ready")

    try:
        await gateway.run()
    finally:
        await client.close()
        await container.shutdown()


def _install_signal_handlers(
    gateway: TelegramGateway,
) -> None:  # pragma: no cover - process wiring
    """Ask the gateway to drain on SIGTERM and SIGINT.

    Draining matters: a deploy in the middle of a download should let it finish
    and be delivered, not abandon it half-uploaded.
    """
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        signal_number = getattr(signal, name, None)
        if signal_number is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal_number, gateway.stop)


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
