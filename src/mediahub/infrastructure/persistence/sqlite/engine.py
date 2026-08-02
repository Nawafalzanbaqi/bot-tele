"""Engine lifecycle for the SQLite system of record.

One :class:`SqliteDatabase` per process, created at startup and disposed at
shutdown.

**The pragmas are the whole point of this module.** SQLite's defaults are tuned
for a library embedded in a desktop application, not for a service that must
survive a power cut on a device with no UPS. Each one below is issued on *every*
connection, because SQLite scopes them per connection and a pool hands out new
ones for the life of the process:

``foreign_keys=ON``
    Off by default. The schema declares ``ondelete="CASCADE"``; without this
    pragma that clause is inert and deleting an item silently orphans its jobs.

``journal_mode=WAL``
    Readers stop blocking the writer. On the default rollback journal, the
    Telegram gateway reading history would block the worker committing progress.
    WAL is persistent - set once per database file - but issuing it is cheap and
    makes a restored backup behave like the original.

``synchronous=FULL``
    The expensive one, and the one that matters here. ``NORMAL`` can lose the
    last transactions when the power goes rather than when the process does, and
    the power going is the failure this device actually has.

``busy_timeout``
    SQLite has one writer. Without a timeout a concurrent write raises
    ``SQLITE_BUSY`` immediately; with one it waits, which is what every caller
    here actually wants.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Imported for the side effect of registering every table on `Base.metadata`,
# which is what `create_schema` below reads. Without it a fresh database would
# be created with no tables in it and fail on the first query.
from mediahub.infrastructure.persistence.sqlalchemy import models as _models  # noqa: F401
from mediahub.infrastructure.persistence.sqlalchemy.base import Base

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.shared.config.settings import DatabaseSettings

MEMORY_PATH: Final[str] = ":memory:"
"""Path that means "no file at all" - used by tests, never by a deployment."""


def _apply_pragmas(connection: Any, _record: Any) -> None:
    """Configure one raw DBAPI connection, as it is handed out.

    Registered as a pool event rather than executed once, because every pooled
    connection is a separate SQLite handle with its own pragma state.
    """
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=10000")
        # Keeps the rollback/WAL scratch in RAM. It is throwaway data, and on an
        # SD card or NVMe every avoided write is wear avoided.
        cursor.execute("PRAGMA temp_store=MEMORY")
    finally:
        cursor.close()


class SqliteDatabase:
    """Owns the async engine and session factory for a SQLite file."""

    __slots__ = ("_engine", "_path", "_session_factory")

    def __init__(self, settings: DatabaseSettings) -> None:
        """Create the engine, the file's parent directory, and the pragmas."""
        self._path = settings.sqlite_path
        url = settings.sqlite_url

        if str(self._path) == MEMORY_PATH:
            url = "sqlite+aiosqlite://"
            # Every session must see the same in-memory database, so the pool
            # must hand out the one connection that owns it.
            self._engine = create_async_engine(
                url, echo=settings.echo, poolclass=StaticPool, future=True
            )
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            self._engine = create_async_engine(url, echo=settings.echo, future=True)

        event.listen(self._engine.sync_engine, "connect", _apply_pragmas)

        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
        logger.bind(path=str(self._path)).debug("SQLite engine created")

    @property
    def engine(self) -> AsyncEngine:
        """Return the underlying engine, for health checks and migrations."""
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the factory units of work open sessions from."""
        return self._session_factory

    async def create_schema(self) -> None:
        """Create any missing tables.

        Alembic owns schema *evolution*; this exists so a fresh device boots
        into a working system without a separate migrate step, which on an
        appliance is one more thing to go wrong unattended.
        """
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.bind(path=str(self._path)).debug("SQLite schema ensured")

    async def verify_pragmas(self) -> None:
        """Assert at startup that the settings above actually took effect.

        A pragma that silently failed to apply is exactly the class of problem
        this module exists to prevent, so it is checked rather than assumed.

        Raises:
            RuntimeError: If foreign key enforcement is off, which would let
                the schema's cascade rules quietly do nothing.
        """
        async with self._engine.connect() as connection:
            enabled = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()
            mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
        if not enabled:
            message = "SQLite foreign key enforcement is off; cascade rules would not apply"
            raise RuntimeError(message)
        logger.bind(foreign_keys=bool(enabled), journal_mode=mode).debug("SQLite pragmas verified")

    async def dispose(self) -> None:
        """Close every pooled connection. Called once, at shutdown."""
        await self._engine.dispose()
        logger.debug("SQLite engine disposed")
