"""Console entry point: ``python -m mediahub`` or the ``mediahub`` script.

This module is intentionally thin. It reads configuration, hands logging over
to Loguru and starts the ASGI server against the application factory. Any logic
beyond process bootstrap belongs in a layer, not here.
"""

from __future__ import annotations

import uvicorn

from mediahub.shared.config.settings import get_settings
from mediahub.shared.logging.setup import configure_logging

APP_FACTORY_PATH = "mediahub.presentation.api.app:create_app"


def main() -> None:
    """Start the HTTP server using the active :class:`Settings`.

    Uvicorn's own logging configuration is disabled (``log_config=None``) so
    that every record flows through the Loguru sinks configured here.
    """
    settings = get_settings()
    configure_logging(settings)

    uvicorn.run(
        APP_FACTORY_PATH,
        factory=True,
        host=settings.api.host,
        port=settings.api.port,
        root_path=settings.api.root_path,
        workers=settings.api.workers,
        log_config=None,
        access_log=False,  # handled by our own access-log middleware
        server_header=False,
        date_header=True,
    )


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
