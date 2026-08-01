"""Initial schema: media items and download jobs.

Revision ID: 0001
Revises:
Created: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the media library and download queue tables."""
    op.create_table(
        "media_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_algorithm", sa.String(length=32), nullable=True),
        sa.Column("checksum_digest", sa.String(length=128), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_items")),
        sa.UniqueConstraint("source_url", name=op.f("uq_media_items_source_url")),
    )
    op.create_index(
        op.f("ix_media_items_media_type"), "media_items", ["media_type"], unique=False
    )
    op.create_index(op.f("ix_media_items_status"), "media_items", ["status"], unique=False)
    op.create_index(
        "ix_media_items_created_at_desc",
        "media_items",
        [sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "download_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("media_id", sa.Uuid(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=32), nullable=False),
        sa.Column("priority_weight", sa.Integer(), nullable=False),
        sa.Column("downloaded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("backoff_seconds", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["media_id"],
            ["media_items.id"],
            name=op.f("fk_download_jobs_media_id_media_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_download_jobs")),
    )
    op.create_index(
        op.f("ix_download_jobs_media_id"), "download_jobs", ["media_id"], unique=False
    )
    op.create_index(op.f("ix_download_jobs_status"), "download_jobs", ["status"], unique=False)
    op.create_index(
        "ix_download_jobs_queue",
        "download_jobs",
        ["status", sa.text("priority_weight DESC"), "created_at"],
        unique=False,
    )
    # Enforces "at most one active job per media item" in the database, so the
    # guarantee does not depend on the application winning a race.
    op.create_index(
        "uq_download_jobs_active_media",
        "download_jobs",
        ["media_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    """Drop the download queue and media library tables."""
    op.drop_index("uq_download_jobs_active_media", table_name="download_jobs")
    op.drop_index("ix_download_jobs_queue", table_name="download_jobs")
    op.drop_index(op.f("ix_download_jobs_status"), table_name="download_jobs")
    op.drop_index(op.f("ix_download_jobs_media_id"), table_name="download_jobs")
    op.drop_table("download_jobs")

    op.drop_index("ix_media_items_created_at_desc", table_name="media_items")
    op.drop_index(op.f("ix_media_items_status"), table_name="media_items")
    op.drop_index(op.f("ix_media_items_media_type"), table_name="media_items")
    op.drop_table("media_items")
