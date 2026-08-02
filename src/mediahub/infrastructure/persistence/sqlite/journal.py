"""Durable acquisition journal.

Implements :class:`~mediahub.application.download.journal.AcquisitionJournal`
against the system of record, so ``/history`` still answers after a restart -
which on a device that loses power is the normal case, not the exceptional one.

The in-memory adapter it replaces bounds itself per principal to avoid unbounded
growth. This one bounds itself by *age* instead, in :meth:`prune`, because a
table on disk can afford to keep more than a deque in a long-lived process and
because "the last 90 days" is a rule an operator can reason about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast
from uuid import uuid4

from sqlalchemy import delete, select

from mediahub.application.download.journal import JournalEntry
from mediahub.infrastructure.persistence.sqlalchemy.models import JournalEntryModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT_RETENTION_DAYS: Final[int] = 90


class SqliteAcquisitionJournal:
    """Appends completed acquisitions to the database and reads them back."""

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the journal to the session factory of the system of record."""
        self._session_factory = session_factory

    async def record(self, entry: JournalEntry) -> None:
        """Append one entry, in its own transaction.

        Deliberately a separate transaction from the acquisition itself: the
        history entry is a record of something that has *already* happened at
        the destination, and failing to write it must not be able to roll back
        a delivery that Telegram has already accepted.
        """
        async with self._session_factory() as session, session.begin():
            session.add(
                JournalEntryModel(
                    id=uuid4(),
                    principal=entry.principal,
                    url=entry.url,
                    provider=entry.provider,
                    title=entry.title,
                    quality_label=entry.quality_label,
                    bytes_delivered=entry.bytes_delivered,
                    remote_id=entry.remote_id,
                    remote_unique_id=entry.remote_unique_id,
                    message_id=entry.message_id,
                    delivered_at=entry.delivered_at,
                )
            )

    async def recent(self, principal: str, *, limit: int = 10) -> Sequence[JournalEntry]:
        """Return a principal's most recent entries, newest first."""
        statement = (
            select(JournalEntryModel)
            .where(JournalEntryModel.principal == principal)
            .order_by(JournalEntryModel.delivered_at.desc())
            .limit(max(1, limit))
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        return tuple(_to_entry(row) for row in rows)

    async def prune(self, *, before: datetime) -> int:
        """Delete entries older than ``before``, returning how many went.

        Called by the scheduler. Retention is a decision, not a default, which
        is why the cutoff is a parameter rather than a constant read in here.
        """
        async with self._session_factory() as session, session.begin():
            # `execute` is typed as returning the read-oriented `Result`; a DML
            # statement actually returns a `CursorResult`, which is the only
            # thing that carries a row count.
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    delete(JournalEntryModel).where(JournalEntryModel.delivered_at < before)
                ),
            )
        return int(result.rowcount or 0)


def _to_entry(row: JournalEntryModel) -> JournalEntry:
    """Translate a row into the application's entry."""
    return JournalEntry(
        principal=row.principal,
        url=row.url,
        provider=row.provider,
        title=row.title,
        quality_label=row.quality_label,
        bytes_delivered=row.bytes_delivered,
        remote_id=row.remote_id,
        delivered_at=row.delivered_at,
        remote_unique_id=row.remote_unique_id,
        message_id=row.message_id,
    )
