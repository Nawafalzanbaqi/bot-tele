"""Use case: read a principal's recent acquisitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from mediahub.application.download.dto import HistoryEntrySummary

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.download.dto import GetHistoryQuery
    from mediahub.application.download.journal import AcquisitionJournal

MAX_LIMIT: Final[int] = 50


class GetHistory:
    """Return what a principal has acquired, newest first.

    A principal sees only their own history. On a shared household device one
    person's viewing is not another's business, and enforcing that here rather
    than in an interface means every interface enforces it.
    """

    def __init__(self, *, journal: AcquisitionJournal) -> None:
        """Wire the use case to the journal."""
        self._journal = journal

    async def execute(self, request: GetHistoryQuery) -> tuple[HistoryEntrySummary, ...]:
        """Return the caller's recent acquisitions.

        Args:
            request: Whose history, and how much of it.

        Returns:
            Up to ``limit`` entries, newest first, capped at
            :data:`MAX_LIMIT` so no caller can ask for an unbounded read.
        """
        limit = max(1, min(request.limit, MAX_LIMIT))
        entries = await self._journal.recent(request.principal, limit=limit)
        return tuple(
            HistoryEntrySummary(
                title=entry.title,
                url=entry.url,
                provider=entry.provider,
                quality_label=entry.quality_label,
                bytes_delivered=entry.bytes_delivered,
                delivered_at=entry.delivered_at,
                message_id=entry.message_id,
            )
            for entry in entries
        )
