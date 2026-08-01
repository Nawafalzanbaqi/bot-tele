"""The FastAPI application factory.

``create_app`` is a *factory*, not a module-level ``app`` object, and that is a
deliberate architectural choice: a global application is built at import time,
which means tests cannot vary its configuration, two applications cannot
coexist in one process, and importing any module drags in a database
connection. A factory has none of those properties.

Assembly order matters and is fixed here:

1. Configure logging, so even startup failures are formatted correctly.
2. Build the app with a lifespan that owns the container.
3. Install middleware - correlation id outermost, then access log, then CORS.
4. Register exception handlers, so no route needs ``try``/``except``.
5. Mount routers: unversioned health first, then ``/api/v1``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from mediahub import __version__
from mediahub.presentation.api.errors import register_exception_handlers
from mediahub.presentation.api.lifespan import build_lifespan
from mediahub.presentation.api.middleware.access_log import AccessLogMiddleware
from mediahub.presentation.api.middleware.correlation import CorrelationIdMiddleware
from mediahub.presentation.api.routers import health
from mediahub.presentation.api.v1.router import api_v1_router
from mediahub.shared.config.settings import get_settings
from mediahub.shared.logging.setup import configure_logging

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.infrastructure.di.container import Container
    from mediahub.shared.config.settings import Settings

API_DESCRIPTION = """
Self-hosted media hub.

Catalogue media items, queue acquisitions and follow their progress.

**Note:** download execution is not implemented yet. Jobs are accepted, stored
and cancellable, but nothing transfers bytes until a download engine is
configured.
"""


def create_app(
    settings: Settings | None = None,
    container: Container | None = None,
) -> FastAPI:
    """Build a fully wired FastAPI application.

    Args:
        settings: Configuration to use. Defaults to the process environment.
        container: A pre-built container. Tests pass one to inject in-memory
            adapters; production leaves it ``None``.

    Returns:
        The assembled application, ready to serve.
    """
    active_settings = settings or get_settings()
    configure_logging(active_settings)

    app = FastAPI(
        title=active_settings.api.title,
        version=__version__,
        description=API_DESCRIPTION,
        root_path=active_settings.api.root_path,
        docs_url="/docs" if active_settings.api.docs_enabled else None,
        redoc_url="/redoc" if active_settings.api.docs_enabled else None,
        openapi_url="/openapi.json" if active_settings.api.docs_enabled else None,
        lifespan=build_lifespan(active_settings, container),
    )

    # Correlation is added last so it runs first: every other layer, including
    # the access log, then sees a bound correlation id.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    if active_settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=active_settings.api.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_v1_router)

    return app
