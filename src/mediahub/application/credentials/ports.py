"""The credentials the engine presents to sources, behind a port.

A cookie jar is what makes several platforms answer at all: X returns "no video
could be found in this post" and TikTok returns "your IP address is blocked"
for content a logged-in session can see. Both are honest refusals, and both
read like a fault in this application.

Jars expire, so installing one is not a deployment step that happens once - it
is an operation the owner performs every few weeks. That is why it is a port
with a use case behind it rather than a file somebody edits over SSH: the same
operation has to be reachable from Telegram, from the HTTP API and from a
script, with one set of rules about who may do it and what is accepted.

**Nothing in this package ever logs a jar's contents**, and the summary type
below is deliberately the only thing that leaves it: counts, domains and
expiries are useful to a person and are not credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class CookieSummary:
    """What a stored jar contains, with nothing secret in it.

    Attributes:
        cookie_count: How many cookies the jar holds.
        domains: Which hosts they are for, deduplicated and sorted. This is the
            field that answers "did I export the right site?", which is the
            mistake people actually make.
        earliest_expiry: When the first cookie lapses, or ``None`` if every
            cookie is a session cookie. Reported because a jar does not stop
            working with a bang - a platform simply starts refusing again, and
            knowing the date turns that into a reminder rather than a mystery.
        installed_at: When the jar was stored.
        size_bytes: How large the file is.
    """

    cookie_count: int
    domains: tuple[str, ...]
    earliest_expiry: datetime | None
    installed_at: datetime
    size_bytes: int

    @property
    def is_empty(self) -> bool:
        """Return whether the jar holds no usable cookies."""
        return self.cookie_count == 0


class CookieStore(Protocol):
    """Somewhere a cookie jar is kept for the engine to read."""

    async def install(self, content: bytes) -> CookieSummary:
        """Validate ``content`` and make it the jar the engine will present.

        Raises:
            InvalidCookieJarError: If the content is not a usable jar. Storing
                an unusable one is worse than refusing it: the engine would
                keep failing with the platform's own confusing message rather
                than with the real reason.
        """
        ...

    async def describe(self) -> CookieSummary | None:
        """Return what is currently stored, or ``None`` if nothing is."""
        ...

    async def discard(self) -> bool:
        """Remove the stored jar. Returns whether there was one."""
        ...
