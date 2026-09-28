"""History is bounded by age: entries older than the retention are forgotten at start-up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediahub.application.download.journal import JournalEntry
from mediahub.infrastructure.persistence.memory.journal import InMemoryAcquisitionJournal

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def entry(principal: str, *, days_ago: int) -> JournalEntry:
    return JournalEntry(
        principal=principal,
        url=f"https://example.com/{days_ago}",
        provider="testsite",
        title=f"{days_ago} days ago",
        quality_label="720p",
        bytes_delivered=1,
        remote_id=f"R{days_ago}",
        delivered_at=NOW - timedelta(days=days_ago),
    )


class TestInMemoryPrune:
    async def test_old_entries_go_and_recent_ones_stay(self) -> None:
        journal = InMemoryAcquisitionJournal()
        for days in (200, 91, 30, 1):  # recorded as they happened, oldest first
            await journal.record(entry("telegram:1", days_ago=days))
        await journal.record(entry("telegram:2", days_ago=400))

        removed = await journal.prune(before=NOW - timedelta(days=90))

        assert removed == 3
        titles = [e.title for e in await journal.recent("telegram:1")]
        assert titles == ["1 days ago", "30 days ago"], "newest first, nothing older than 90 d"
        assert await journal.recent("telegram:2") == ()

    async def test_nothing_to_prune_is_zero(self) -> None:
        journal = InMemoryAcquisitionJournal()
        await journal.record(entry("telegram:1", days_ago=1))

        assert await journal.prune(before=NOW - timedelta(days=90)) == 0
        assert len(await journal.recent("telegram:1")) == 1

    async def test_the_boundary_keeps_an_entry_delivered_exactly_at_the_cutoff(self) -> None:
        journal = InMemoryAcquisitionJournal()
        cutoff = NOW - timedelta(days=90)
        await journal.record(entry("telegram:1", days_ago=90))

        assert await journal.prune(before=cutoff) == 0
