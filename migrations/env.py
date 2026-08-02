"""Alembic environment.

Two things make this file worth reading once:

* **The URL comes from application settings**, never from ``alembic.ini``, and
  it follows the configured backend rather than assuming PostgreSQL. One source
  of truth means migrations cannot be applied to a different database than the
  one the app is about to use - which on the appliance is a file.
* **Every model module is imported** before ``target_metadata`` is read.
  Autogenerate diffs the metadata registry against the live schema; a model
  that was never imported looks like a table that should be dropped.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from mediahub.infrastructure.persistence.sqlalchemy import models  # noqa: F401
from mediahub.infrastructure.persistence.sqlalchemy.base import Base
from mediahub.shared.config.settings import get_settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config
target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", get_settings().database.migration_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already-established synchronous connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an async engine and run the migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for a normal ``alembic upgrade``."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
