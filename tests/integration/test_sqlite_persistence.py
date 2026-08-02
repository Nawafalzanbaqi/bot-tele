"""The SQLite adapter, tested against a real SQLite file.

Every test here corresponds to a *silent* defect named in
``docs/architecture/20-architecture-decision-review.md`` §§4.7-4.10: behaviour
that differs from PostgreSQL without raising anything. A unit test against the
in-memory adapter cannot catch any of them, which is the whole reason this file
exists and why it uses a file on disk rather than ``:memory:``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError

from mediahub.application.download.journal import JournalEntry
from mediahub.domain.download.enums import JobPriority
from mediahub.infrastructure.persistence.sqlalchemy.models import (
    DownloadJobModel,
    JobQueueModel,
    JournalEntryModel,
    MediaItemModel,
)
from mediahub.infrastructure.persistence.sqlalchemy.unit_of_work import SqlAlchemyUnitOfWorkFactory
from mediahub.infrastructure.persistence.sqlite.engine import SqliteDatabase
from mediahub.infrastructure.persistence.sqlite.journal import SqliteAcquisitionJournal
from mediahub.shared.config.settings import DatabaseSettings, PersistenceBackend
from tests.support.worker_fakes import queued_job

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A real SQLite file, schema created, disposed afterwards."""
    settings = DatabaseSettings(
        backend=PersistenceBackend.SQLITE, sqlite_path=tmp_path / "mediahub.db"
    )
    db = SqliteDatabase(settings)
    await db.create_schema()
    yield db
    await db.dispose()


def _media(**overrides: Any) -> MediaItemModel:
    now = datetime.now(UTC)
    fields = {
        "id": uuid4(),
        "source_url": f"https://example.com/{uuid4()}",
        "title": "A talk",
        "media_type": "video",
        "status": "pending",
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return MediaItemModel(**fields)


def _job(media_id: UUID, status: str = "queued", **overrides: Any) -> DownloadJobModel:
    now = datetime.now(UTC)
    fields = {
        "id": uuid4(),
        "media_id": media_id,
        "source_url": "https://example.com/talk.mp4",
        "status": status,
        "priority": "normal",
        "priority_weight": 100,
        "downloaded_bytes": 0,
        "attempts": 0,
        "max_attempts": 3,
        "backoff_seconds": 30,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return DownloadJobModel(**fields)


# -- §4.7 timezone loss ------------------------------------------------------


async def test_a_timestamp_survives_the_round_trip_with_its_timezone(
    database: SqliteDatabase,
) -> None:
    """The defect: SQLite returns naive datetimes and every aggregate load raises."""
    stored = datetime(2026, 3, 1, 12, 30, 45, tzinfo=UTC)
    item = _media(created_at=stored, updated_at=stored)

    async with database.session_factory() as session, session.begin():
        session.add(item)

    async with database.session_factory() as session:
        loaded = (
            await session.execute(select(MediaItemModel).where(MediaItemModel.id == item.id))
        ).scalar_one()

    assert loaded.created_at.tzinfo is not None, "a naive datetime would fail ensure_utc"
    assert loaded.created_at == stored


async def test_a_non_utc_timestamp_is_normalised_rather_than_stored_as_given(
    database: SqliteDatabase,
) -> None:
    """Two clients in different zones must produce comparable rows.

    A fixed offset rather than a named zone deliberately: the assertion is about
    normalisation, and depending on the tz database would make this test fail on
    a machine that has no ``tzdata`` for a reason unrelated to what it checks.
    """
    riyadh = timezone(timedelta(hours=3))
    local_noon = datetime(2026, 3, 1, 19, 0, 0, tzinfo=riyadh)
    item = _media(created_at=local_noon, updated_at=local_noon)

    async with database.session_factory() as session, session.begin():
        session.add(item)

    async with database.session_factory() as session:
        loaded = (
            await session.execute(select(MediaItemModel).where(MediaItemModel.id == item.id))
        ).scalar_one()

    assert loaded.created_at == datetime(2026, 3, 1, 16, 0, 0, tzinfo=UTC)


async def test_a_naive_timestamp_is_refused_rather_than_guessed(database: SqliteDatabase) -> None:
    """Guessing a zone here is how "off by three hours" bugs are born."""
    naive = datetime(2026, 3, 1, 12, 0, 0)  # noqa: DTZ001 - being naive is the point
    item = _media(created_at=naive, updated_at=datetime.now(UTC))

    with pytest.raises(Exception, match="naive datetime"):
        async with database.session_factory() as session, session.begin():
            session.add(item)


# -- §4.8 foreign keys -------------------------------------------------------


async def test_foreign_keys_are_actually_enforced(database: SqliteDatabase) -> None:
    """The defect: SQLite ignores FKs unless the pragma is on, per connection."""
    orphan = _job(media_id=uuid4())  # no such media item

    with pytest.raises(IntegrityError):
        async with database.session_factory() as session, session.begin():
            session.add(orphan)


async def _insert(database: SqliteDatabase, *rows: object) -> None:
    """Commit rows in their own transaction.

    One transaction per table, in dependency order, because that is how the
    application actually writes: registering an item and queueing a job are
    separate use cases with separate units of work.
    """
    for row in rows:
        async with database.session_factory() as session, session.begin():
            session.add(row)


async def test_deleting_an_item_cascades_to_its_jobs(database: SqliteDatabase) -> None:
    """`ondelete="CASCADE"` is inert without the pragma - silently orphaning rows."""
    item = _media()
    await _insert(database, item, _job(media_id=item.id))

    async with database.session_factory() as session, session.begin():
        # Through the ORM so the UUID is coerced the way the column stores it;
        # raw SQL with the wrong representation would delete nothing and the
        # test would pass for the wrong reason.
        await session.execute(delete(MediaItemModel).where(MediaItemModel.id == item.id))

    async with database.session_factory() as session:
        remaining = (await session.execute(select(DownloadJobModel))).scalars().all()

    assert remaining == []


async def test_the_pragma_check_passes_on_a_real_connection(database: SqliteDatabase) -> None:
    """Startup asserts this rather than assuming it."""
    await database.verify_pragmas()


# -- §4.9 the partial unique index -------------------------------------------


async def test_the_partial_unique_index_exists_on_sqlite(database: SqliteDatabase) -> None:
    """A `postgresql_where` alone is dropped on SQLite - no index, no error."""
    async with database.engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: [i["name"] for i in inspect(sync).get_indexes("download_jobs")]
        )

    assert "uq_download_jobs_active_media" in names


async def test_a_second_active_job_for_one_item_is_rejected(database: SqliteDatabase) -> None:
    """The invariant itself, not the declaration of it."""
    item = _media()
    await _insert(database, item, _job(media_id=item.id, status="queued"))

    with pytest.raises(IntegrityError):
        await _insert(database, _job(media_id=item.id, status="running"))


async def test_a_finished_job_does_not_block_a_new_one(database: SqliteDatabase) -> None:
    """The index is partial for a reason: history must not prevent a retry."""
    item = _media()
    await _insert(database, item, _job(media_id=item.id, status="completed"))
    await _insert(database, _job(media_id=item.id, status="queued"))

    async with database.session_factory() as session:
        jobs = (await session.execute(select(DownloadJobModel))).scalars().all()

    assert len(jobs) == 2


# -- The journal -------------------------------------------------------------


def _entry(principal: str, *, title: str, delivered_at: datetime) -> JournalEntry:
    return JournalEntry(
        principal=principal,
        url="https://example.com/talk",
        provider="youtube",
        title=title,
        quality_label="720p",
        bytes_delivered=1024,
        remote_id="file-abc",
        delivered_at=delivered_at,
        remote_unique_id="uniq-abc",
        message_id="42",
    )


async def test_history_survives_a_restart(database: SqliteDatabase, tmp_path: Path) -> None:
    """The whole point of replacing the in-memory journal."""
    now = datetime.now(UTC)
    journal = SqliteAcquisitionJournal(database.session_factory)
    await journal.record(_entry("telegram:1", title="Kept", delivered_at=now))

    # A new process opening the same file.
    settings = DatabaseSettings(
        backend=PersistenceBackend.SQLITE, sqlite_path=tmp_path / "mediahub.db"
    )
    reopened = SqliteDatabase(settings)
    try:
        entries = await SqliteAcquisitionJournal(reopened.session_factory).recent("telegram:1")
    finally:
        await reopened.dispose()

    assert [entry.title for entry in entries] == ["Kept"]


async def test_a_principal_sees_only_their_own_history(database: SqliteDatabase) -> None:
    """On a shared device, one person's viewing is not another's business."""
    now = datetime.now(UTC)
    journal = SqliteAcquisitionJournal(database.session_factory)
    await journal.record(_entry("telegram:1", title="Mine", delivered_at=now))
    await journal.record(_entry("telegram:2", title="Theirs", delivered_at=now))

    entries = await journal.recent("telegram:1")

    assert [entry.title for entry in entries] == ["Mine"]


async def test_history_is_newest_first_and_honours_the_limit(database: SqliteDatabase) -> None:
    now = datetime.now(UTC)
    journal = SqliteAcquisitionJournal(database.session_factory)
    for index in range(5):
        await journal.record(
            _entry("telegram:1", title=f"#{index}", delivered_at=now + timedelta(minutes=index))
        )

    entries = await journal.recent("telegram:1", limit=3)

    assert [entry.title for entry in entries] == ["#4", "#3", "#2"]


async def test_pruning_removes_only_what_is_older_than_the_cutoff(database: SqliteDatabase) -> None:
    now = datetime.now(UTC)
    journal = SqliteAcquisitionJournal(database.session_factory)
    await journal.record(_entry("telegram:1", title="Old", delivered_at=now - timedelta(days=120)))
    await journal.record(_entry("telegram:1", title="Recent", delivered_at=now))

    removed = await journal.prune(before=now - timedelta(days=90))

    assert removed == 1
    assert [entry.title for entry in await journal.recent("telegram:1")] == ["Recent"]


async def test_an_entry_round_trips_every_field(database: SqliteDatabase) -> None:
    """A receipt is what would let an item be re-acquired; none of it may be lost."""
    now = datetime.now(UTC).replace(microsecond=0)
    journal = SqliteAcquisitionJournal(database.session_factory)
    original = _entry("telegram:1", title="Full", delivered_at=now)
    await journal.record(original)

    (restored,) = await journal.recent("telegram:1")

    assert restored == original


async def test_the_journal_table_is_created_by_the_schema(database: SqliteDatabase) -> None:
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())

    assert JournalEntryModel.__tablename__ in tables


async def test_the_job_queue_table_is_created_by_the_schema(database: SqliteDatabase) -> None:
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())

    assert JobQueueModel.__tablename__ in tables


async def test_an_item_and_its_job_can_be_written_in_one_transaction(
    database: SqliteDatabase,
) -> None:
    """Foreign keys are enforced here, so insert order is not a free choice.

    Without a ``relationship`` to sort by, SQLAlchemy emits the two inserts in
    mapper order - ``download_jobs`` first - and this fails on a constraint that
    is perfectly satisfied. The repositories flush so that the order is the
    caller's instead. Registering an item and queueing work for it together is
    exactly what a single-screen web submission would do.
    """
    item, job = queued_job(4242, priority=JobPriority.NORMAL, now=datetime.now(UTC))
    unit_of_work = SqlAlchemyUnitOfWorkFactory(database.session_factory)

    async with unit_of_work() as uow:
        await uow.media.add(item)
        await uow.download_jobs.add(job)
        await uow.commit()

    async with unit_of_work() as uow:
        assert await uow.download_jobs.get(job.id) is not None
        assert await uow.media.get(item.id) is not None


async def test_a_rolled_back_unit_of_work_leaves_nothing_behind(
    database: SqliteDatabase,
) -> None:
    """Flushing early must not mean committing early."""
    item, job = queued_job(4343, priority=JobPriority.NORMAL, now=datetime.now(UTC))
    unit_of_work = SqlAlchemyUnitOfWorkFactory(database.session_factory)

    async with unit_of_work() as uow:
        await uow.media.add(item)
        await uow.download_jobs.add(job)
        # Left without a commit: `__aexit__` rolls back.

    async with unit_of_work() as uow:
        assert await uow.download_jobs.get(job.id) is None
        assert await uow.media.get(item.id) is None
