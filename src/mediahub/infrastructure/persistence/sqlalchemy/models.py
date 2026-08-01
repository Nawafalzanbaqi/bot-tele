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
"""

from __future__ import annotations

# `datetime` and `UUID` are imported at runtime, not under TYPE_CHECKING: the
# declarative mapper resolves `Mapped[...]` annotations when the class is built.
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The scheduler's claim query: queued work, highest priority, oldest first.
        Index(
            "ix_download_jobs_queue",
            "status",
            text("priority_weight DESC"),
            "created_at",
        ),
        # At most one queued/running job per media item.
        Index(
            "uq_download_jobs_active_media",
            "media_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )
