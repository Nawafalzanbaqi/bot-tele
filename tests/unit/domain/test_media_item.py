"""The media aggregate enforces its lifecycle and records what happened."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from mediahub.domain.common.errors import InvariantViolationError
from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm
from mediahub.domain.media.entities import MediaItem
from mediahub.domain.media.enums import MediaStatus, MediaType
from mediahub.domain.media.errors import InvalidMediaTransitionError
from mediahub.domain.media.value_objects import (
    FileSize,
    MediaId,
    MediaTitle,
    SourceUrl,
    StorageKey,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)


def make_item(status: MediaStatus = MediaStatus.PENDING) -> MediaItem:
    """Build an item directly in ``status``, bypassing the lifecycle."""
    return MediaItem(
        media_id=MediaId(UUID(int=1)),
        source_url=SourceUrl("https://example.com/a.mp4"),
        title=MediaTitle("A"),
        media_type=MediaType.VIDEO,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


class TestRegistration:
    def test_starts_pending_and_records_event(self) -> None:
        item = MediaItem.register(
            media_id=MediaId(UUID(int=1)),
            source_url=SourceUrl("https://example.com/a.mp4"),
            title=MediaTitle("A talk"),
            media_type=MediaType.VIDEO,
            now=NOW,
        )

        assert item.status is MediaStatus.PENDING
        assert item.created_at == NOW
        assert [event.name for event in item.events] == ["MediaRegistered"]

    def test_rejects_naive_timestamps(self) -> None:
        with pytest.raises(InvariantViolationError):
            MediaItem.register(
                media_id=MediaId(UUID(int=1)),
                source_url=SourceUrl("https://example.com/a.mp4"),
                title=MediaTitle("A talk"),
                media_type=MediaType.VIDEO,
                now=datetime(2026, 1, 1, 12, 0),  # noqa: DTZ001 - the point of the test
            )

    def test_pull_events_drains_once(self) -> None:
        item = make_item()
        item.archive(now=LATER)

        assert len(item.pull_events()) == 1
        assert item.pull_events() == ()


class TestLifecycle:
    def test_becomes_available_with_storage_details(self) -> None:
        item = make_item()
        item.mark_available(
            storage_key=StorageKey("video/a.mp4"),
            size=FileSize(1024),
            checksum=Fingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
            now=LATER,
        )

        assert item.is_available
        assert item.storage_key is not None
        assert str(item.storage_key) == "video/a.mp4"
        assert item.updated_at == LATER
        assert [event.name for event in item.events] == ["MediaBecameAvailable"]

    def test_failure_reason_is_recorded_and_bounded(self) -> None:
        item = make_item()
        item.mark_failed(reason="x" * 5000, now=LATER)

        assert item.status is MediaStatus.FAILED
        assert item.failure_reason is not None
        assert len(item.failure_reason) == 1000

    def test_failed_item_can_be_retried(self) -> None:
        item = make_item(MediaStatus.FAILED)
        item.reset_to_pending(now=LATER)

        assert item.status is MediaStatus.PENDING
        assert item.failure_reason is None

    def test_archiving_is_terminal(self) -> None:
        item = make_item(MediaStatus.AVAILABLE)
        item.archive(now=LATER)

        with pytest.raises(InvalidMediaTransitionError):
            item.archive(now=LATER)

    def test_archived_item_cannot_be_renamed(self) -> None:
        item = make_item(MediaStatus.ARCHIVED)

        with pytest.raises(InvalidMediaTransitionError):
            item.rename(title=MediaTitle("New"), now=LATER)

    def test_available_item_cannot_go_back_to_pending(self) -> None:
        item = make_item(MediaStatus.AVAILABLE)

        with pytest.raises(InvalidMediaTransitionError):
            item.reset_to_pending(now=LATER)


class TestRenaming:
    def test_records_event_when_changed(self) -> None:
        item = make_item()
        item.rename(title=MediaTitle("Renamed"), now=LATER)

        assert str(item.title) == "Renamed"
        assert [event.name for event in item.events] == ["MediaRenamed"]

    def test_is_a_no_op_when_unchanged(self) -> None:
        item = make_item()
        item.rename(title=MediaTitle("A"), now=LATER)

        assert item.updated_at == NOW
        assert item.events == ()


class TestIdentity:
    def test_equality_is_by_identifier(self) -> None:
        one = make_item()
        two = make_item(MediaStatus.ARCHIVED)

        assert one == two
        assert len({one, two}) == 1
