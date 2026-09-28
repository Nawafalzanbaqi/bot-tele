"""A record of what was acquired and where it ended up.

**Interim.** The durable home for this is the Catalogue's ``MediaAsset`` with
its custody state and remote references
(``docs/architecture/06-domain-model.md`` §6.3), which arrives with the custody
rework listed as item 2 of the Phase 02 remediation backlog. Until then the
journal gives ``/history`` something true to show without misusing
``MediaItem.storage_key`` - a field that means "the bytes are local", which
after delivery they are not.

Keeping it behind a port means the swap is a container change: nothing that
reads history knows where it is stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One completed acquisition, as remembered after the bytes are gone.

    Everything here is metadata: a few hundred bytes that outlive a file of a
    few hundred megabytes.

    Attributes:
        principal: Who asked for it.
        url: Canonical source, kept so the item can be re-acquired if the
            destination ever loses it.
        provider: Which platform it came from.
        title: What it was called.
        quality_label: Which rendition was taken.
        bytes_delivered: How large it was.
        remote_id: The destination's reference to the stored bytes.
        remote_unique_id: A stable identifier that survives credential changes.
        message_id: Where the destination announced it.
        delivered_at: When the destination confirmed (UTC).
    """

    principal: str
    url: str
    provider: str
    title: str
    quality_label: str
    bytes_delivered: int
    remote_id: str
    delivered_at: datetime
    remote_unique_id: str | None = None
    message_id: str | None = None


class AcquisitionJournal(Protocol):
    """Append-only record of completed acquisitions, bounded by age."""

    async def record(self, entry: JournalEntry) -> None:
        """Append one entry."""
        ...

    async def prune(self, *, before: datetime) -> int:
        """Forget entries delivered before ``before``; return how many went.

        History exists so a person can find what they fetched last week, not
        so the device keeps a row for every file it ever sent. Called at
        start-up with the configured retention.
        """
        ...

    async def recent(self, principal: str, *, limit: int = 10) -> Sequence[JournalEntry]:
        """Return a principal's most recent entries, newest first.

        A principal sees only their own history: on a shared household device,
        one person's viewing is not another's business.
        """
        ...
