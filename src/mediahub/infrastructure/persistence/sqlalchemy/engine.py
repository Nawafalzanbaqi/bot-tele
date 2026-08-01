"""Engine and session-factory lifecycle.

One :class:`Database` per process. It owns the connection pool, so it must be
created once at startup and disposed once at shutdown - creating engines per
request silently leaks connections until Postgres refuses new ones.

``expire_on_commit=False`` is deliberate: without it, every attribute access
after a commit triggers a refresh query, which in an async session raises
instead of quietly working. Since aggregates are detached copies anyway (see
:mod:`~mediahub.infrastructure.persistence.sqlalchemy.mappers`), nothing needs
the identity map after commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.shared.config.settings import DatabaseSettings


class Database:
    """Owns the async engine and the session factory built from it."""

    __slots__ = ("_engine", "_session_factory")

    def __init__(self, settings: DatabaseSettings) -> None:
        """Create the engine and session factory from configuration."""
        self._engine: AsyncEngine = create_async_engine(
            settings.dsn,
            echo=settings.echo,
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_timeout=settings.pool_timeout_seconds,
            pool_pre_ping=True,
            future=True,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
        logger.bind(dsn=settings.safe_dsn, pool_size=settings.pool_size).debug(
            "Database engine created"
        )

    @property
    def engine(self) -> AsyncEngine:
        """Return the underlying engine, for health checks and migrations."""
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the factory units of work use to open sessions."""
        return self._session_factory

    async def dispose(self) -> None:
        """Close every pooled connection. Called once, at shutdown."""
        await self._engine.dispose()
        logger.debug("Database engine disposed")
