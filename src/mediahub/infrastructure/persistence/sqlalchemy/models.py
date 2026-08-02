"""The physical database schema.

These classes describe *storage*, not behaviour. They hold no business rules,
no validation and no relationships that would let a caller wander the object
graph - translation to and from aggregates is explicit, in
:mod:`~mediahub.infrastructure.persistence.sqlalchemy.mappers`.

Two deliberate choices worth remembering:

* **Enums are stored as short strings**, not as native PostgreSQL enums.
  Adding a value to a native enum requires a migration and a lock; adding one
  to a ``VARCHAR`` requires nothing. The domain is the authority on which
  values are legal.
* **Priority is denormalised into ``priority_weight``.** The scheduler must
  order by priority, and an index on an integer is the only way to do that
  without a CASE expression in every query.

Timestamps use :class:`~mediahub.infrastructure.persistence.sqlite.types.UtcDateTime`
rather than SQLAlchemy's ``DateTime(timezone=True)``. The two are identical on
PostgreSQL; on SQLite the plain type returns naive datetimes and every aggregate
load raises. See that module for the full account.
"""

from __future__ import annotations

# `datetime` and `UUID` are imported at runtime, not under TYPE_CHECKING: the
# declarative mapper resolves `Mapped[...]` annotations when the class is built.
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mediahub.infrastructure.persistence.sqlalchemy.base import Base
from mediahub.infrastructure.persistence.sqlite.types import UtcDateTime


class MediaItemModel(Base):
    """Row shape of a catalogued media item.

    The unique index on ``source_url`` is what actually prevents duplicate
    registrations under concurrency; the check in the use case is a friendlier
    first line of defence, not the guarantee.
    """

    __tablename__ = "media_items"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum_algorithm: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checksum_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        # Supports the default listing: newest first, optionally filtered.
        Index("ix_media_items_created_at_desc", text("created_at DESC")),
    )


class DownloadJobModel(Base):
    """Row shape of a download job.

    The partial unique index enforces "at most one active job per media item"
    in the database itself. That invariant protects an artefact from two
    concurrent writers, so it must not depend on application-level checks
    winning a race.
    """

    __tablename__ = "download_jobs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    media_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(32), nullable=False)
    priority_weight: Mapped[int] = mapped_column(Integer, nullable=False)
    downloaded_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    backoff_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        # The scheduler's claim query: queued work, highest priority, oldest first.
        Index(
            "ix_download_jobs_queue",
            "status",
            text("priority_weight DESC"),
            "created_at",
        ),
        # At most one queued/running job per media item.
        #
        # Both dialect keywords are required. A `postgresql_where` alone is
        # *silently dropped* on SQLite - no index, no error - which leaves the
        # invariant resting on an application check that loses exactly the race
        # the index exists to win.
        Index(
            "uq_download_jobs_active_media",
            "media_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )


class JournalEntryModel(Base):
    """Row shape of one completed acquisition.

    A few hundred bytes that outlive a file of a few hundred megabytes: after
    delivery the bytes are the destination's, and this is all that remains. It
    is what ``/history`` reads, and it is what would let an item be re-acquired
    if the destination ever lost it.
    """

    __tablename__ = "journal_entries"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    principal: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    quality_label: Mapped[str] = mapped_column(String(64), nullable=False)
    bytes_delivered: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    remote_id: Mapped[str] = mapped_column(String(256), nullable=False)
    remote_unique_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        # The only query this table serves: one principal's history, newest
        # first. A principal sees only their own - on a shared household device
        # one person's viewing is not another's business.
        Index("ix_journal_entries_principal_delivered", "principal", text("delivered_at DESC")),
    )
