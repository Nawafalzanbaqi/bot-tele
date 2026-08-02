"""Durable history and durable scheduling.

Two tables that let the system survive losing the process that was running it:
``journal_entries`` is what ``/history`` reads after a restart, and ``job_queue``
holds the lease, checkpoint and cancellation flag that turn a power cut into
"resume from verify" rather than "start again".

Both were introduced alongside the SQLite backend, where a fresh device gets its
schema from ``SqliteDatabase.create_schema``. This migration is what keeps a
database that already exists - a Raspberry Pi with history in it - able to reach
the same shape without being recreated.

**Every step here is conditional, and that is the point.** The device it has to
upgrade was built by ``create_all``, so some of these objects are already
present; a plain ``CREATE TABLE`` would abort the upgrade on the one database
that most needs it. Applying this to a database that already has the tables must
be a no-op, not an error.

Revision ID: 0002
Revises: 0001
Created: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the acquisition journal and the job queue."""
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal", sa.String(length=128), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("quality_label", sa.String(length=64), nullable=False),
        sa.Column("bytes_delivered", sa.BigInteger(), nullable=False),
        sa.Column("remote_id", sa.String(length=256), nullable=False),
        sa.Column("remote_unique_id", sa.String(length=256), nullable=True),
        sa.Column("message_id", sa.String(length=64), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_entries")),
        if_not_exists=True,
    )
    op.create_index(
        "ix_journal_entries_principal_delivered",
        "journal_entries",
        ["principal", sa.text("delivered_at DESC")],
        unique=False,
        if_not_exists=True,
    )

    op.create_table(
        "job_queue",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        # Lease: who holds the job, and until when.
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        # Checkpoint: what it has already finished.
        sa.Column("completed_stages", sa.String(length=255), nullable=True),
        sa.Column("resume_token", sa.Text(), nullable=True),
        sa.Column("checkpoint_updated_at", sa.DateTime(timezone=True), nullable=True),
        # Scheduling.
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        # Last observation.
        sa.Column("progress_stage", sa.String(length=32), nullable=True),
        sa.Column("progress_transferred_bytes", sa.BigInteger(), nullable=True),
        sa.Column("progress_total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("progress_speed_bps", sa.Float(), nullable=True),
        sa.Column("progress_eta_seconds", sa.Float(), nullable=True),
        sa.Column("progress_observed_at", sa.DateTime(timezone=True), nullable=True),
        # Outcome, kept in columns so triage is a SELECT.
        sa.Column("failure_kind", sa.String(length=32), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("failure_stage", sa.String(length=32), nullable=True),
        sa.Column("failure_retry_after_seconds", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["download_jobs.id"],
            name=op.f("fk_job_queue_job_id_download_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_job_queue")),
        if_not_exists=True,
    )
    op.create_index(
        "ix_job_queue_lease_expires_at",
        "job_queue",
        ["lease_expires_at"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_job_queue_lease_owner",
        "job_queue",
        ["lease_owner"],
        unique=False,
        if_not_exists=True,
    )

    # The partial index from 0001 was declared for PostgreSQL only, so on SQLite
    # it was silently dropped: no index, no error, and the "one active job per
    # item" invariant resting on an application check that loses exactly the
    # race the index exists to win.
    if op.get_bind().dialect.name == "sqlite":
        op.create_index(
            "uq_download_jobs_active_media",
            "download_jobs",
            ["media_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('queued', 'running')"),
            if_not_exists=True,
        )


def downgrade() -> None:
    """Drop the job queue and the acquisition journal."""
    op.drop_index("ix_job_queue_lease_owner", table_name="job_queue", if_exists=True)
    op.drop_index("ix_job_queue_lease_expires_at", table_name="job_queue", if_exists=True)
    op.drop_table("job_queue", if_exists=True)

    op.drop_index(
        "ix_journal_entries_principal_delivered",
        table_name="journal_entries",
        if_exists=True,
    )
    op.drop_table("journal_entries", if_exists=True)
