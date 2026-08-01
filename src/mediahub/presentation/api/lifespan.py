"""Application startup and shutdown.

Everything expensive - the connection pool, the container - is created once
here and torn down deterministically, rather than lazily on first use. Lazy
initialisation hides failures until the first request; doing it at startup
means a misconfigured deployment fails immediately and visibly.

The container is stored on ``app.state`` so dependencies can reach it without a
module-level global, which is what allows several applications to coexist in
one test process.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from loguru import logger

from mediahub import __version__
from mediahub.infrastructure.di.container import build_container

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI

    from mediahub.infrastructure.di.container import Container
    from mediahub.shared.config.settings import Settings

    type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def build_lifespan(settings: Settings, container: Container | None = None) -> Lifespan:
    """Return a lifespan context manager bound to ``settings``.

    Args:
        settings: The validated configuration for this process.
        container: A pre-built container. Tests pass one to inject in-memory
            adapters; production leaves it ``None`` so the lifespan builds and
            owns it.

    Returns:
        An async context manager suitable for ``FastAPI(lifespan=...)``.
    """
    owns_container = container is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create resources, serve, then release them."""
        active = container if container is not None else build_container(settings)
        app.state.container = active
        app.state.settings = settings
        app.state.debug = settings.is_debug

        logger.bind(
            version=__version__,
            environment=settings.environment.value,
            backend=settings.database.backend.value,
        ).info("MediaHub started")
        try:
            yield
        finally:
            if owns_container:
                await active.shutdown()
            logger.info("MediaHub stopped")

    return lifespan
