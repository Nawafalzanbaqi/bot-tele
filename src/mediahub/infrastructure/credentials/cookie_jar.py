"""Reading and validating a Netscape cookie jar.

Pure parsing, no I/O, so the fiddly part is exhaustively testable and the file
handling next door stays trivial.

The format is old and loosely specified. What is actually agreed on: comment
and blank lines are ignored, and every other line is seven tab-separated
fields - domain, include-subdomains flag, path, secure flag, expiry, name,
value. Browser extensions vary in what they put in the header and whether they
emit the ``#HttpOnly_`` prefix, so this reads leniently and judges only on
whether any usable cookie line survives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

HTTP_ONLY_PREFIX: Final[str] = "#HttpOnly_"
"""Marks a cookie the browser hides from scripts. Still a cookie, still valid."""

FIELD_COUNT: Final[int] = 7
MAX_JAR_BYTES: Final[int] = 2 * 1024 * 1024
"""Well above any real jar, and far below anything that could exhaust memory."""


@dataclass(frozen=True, slots=True)
class ParsedJar:
    """What a jar's text turned out to contain.

    Attributes:
        cookie_count: Usable cookie lines found.
        domains: Hosts covered, deduplicated, leading dots removed and sorted.
        earliest_expiry: The first cookie to lapse, or ``None`` when every
            cookie is a session cookie.
    """

    cookie_count: int
    domains: tuple[str, ...]
    earliest_expiry: datetime | None


def decode(content: bytes) -> str:
    """Return the text of a jar, or raise if it is not text at all.

    Raises:
        ValueError: If the bytes are not UTF-8, which is what happens when
            somebody sends the browser's binary cookie database instead of an
            export of it - a common and otherwise baffling mistake.
    """
    if len(content) > MAX_JAR_BYTES:
        message = f"a cookie jar of {len(content)} bytes is implausibly large"
        raise ValueError(message)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        message = (
            "this is not a text cookie jar; export it in Netscape format "
            "rather than sending the browser's own cookie database"
        )
        raise ValueError(message) from exc


def parse(text: str) -> ParsedJar:
    """Return what a jar contains, without keeping any of it.

    Names and values are counted and then dropped on purpose: everything this
    returns is safe to log, show in a chat and put in an error message.
    """
    count = 0
    domains: set[str] = set()
    expiries: list[datetime] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") and not line.startswith(HTTP_ONLY_PREFIX):
            continue
        if line.startswith(HTTP_ONLY_PREFIX):
            line = line[len(HTTP_ONLY_PREFIX) :]

        fields = line.split("\t")
        if len(fields) < FIELD_COUNT:
            continue

        domain = fields[0].strip().lstrip(".")
        if not domain:
            continue

        count += 1
        domains.add(domain.lower())
        expiry = _expiry(fields[4])
        if expiry is not None:
            expiries.append(expiry)

    return ParsedJar(
        cookie_count=count,
        domains=tuple(sorted(domains)),
        earliest_expiry=min(expiries) if expiries else None,
    )


def _expiry(field: str) -> datetime | None:
    """Return a cookie's expiry, or ``None`` for a session cookie.

    Zero means "session cookie" in this format, and an unreadable value is
    treated the same way: an expiry nobody can parse is not a date to report.
    """
    try:
        seconds = int(field.strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # Some exporters write year-9999 sentinels that overflow the platform's
        # time functions. Not a reason to reject an otherwise good jar.
        return None
