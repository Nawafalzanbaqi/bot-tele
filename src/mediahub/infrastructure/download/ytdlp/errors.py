"""Classifies yt-dlp failures into MediaHub's typed error taxonomy.

Only an adapter can read a provider's error and know whether it means
"geo-blocked forever" or "rate-limited, try in sixty seconds", so this is where
that judgement lives. The caller then decides whether to retry by reading
:attr:`~mediahub.application.download.errors.DownloadError.kind`, never by
matching on message text.

Two deliberate choices:

* **Classification is duck-typed**, by exception class *name* and message
  content, rather than by importing yt-dlp's exception classes. yt-dlp
  reorganises its internals frequently; an `isinstance` check against a moved
  class silently stops matching, while a name check degrades to the safe
  default.
* **Unknown means transient.** An unfamiliar failure gets one honest retry and
  then becomes a dead letter a human can read. Guessing "permanent" would
  quietly discard work that a retry would have completed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    ContentRemovedError,
    DownloadError,
    DownloadFailedError,
    FormatUnavailableError,
    GeoRestrictedError,
    MetadataUnavailableError,
    ProviderError,
    RateLimitedError,
    UnsupportedProviderError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

_RETRY_AFTER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"retry[ _-]?after[^0-9]{0,10}(\d+(?:\.\d+)?)", re.IGNORECASE
)

# Ordered: the first matching rule wins, so put specific phrases before generic
# ones. Each entry is (needles, error factory).
_PERMANENT_MARKERS: Final[tuple[tuple[tuple[str, ...], type[DownloadError]], ...]] = (
    (("unsupported url", "no suitable extractor", "is not a valid url"), UnsupportedProviderError),
    (
        (
            "requested format is not available",
            "requested format not available",
            "no video formats found",
            "no formats found",
        ),
        FormatUnavailableError,
    ),
    # Session-gated. These phrases are what the platforms actually say when
    # they are hiding something from a signed-out visitor, and several of them
    # sound like the content is missing rather than withheld - X's "no video
    # could be found in this tweet" is returned for a tweet that plainly has
    # one. Reading them as "gone" is what makes this failure so confusing, so
    # they are matched *before* the removal markers below.
    (
        (
            "no video could be found in this tweet",
            "no video could be found in this post",
            "nsfw tweet requires authentication",
            "requested content is not available",
            "login required",
            "log in",
            "sign in to",
            "sign in if",
            "authentication",
            "use --cookies",
            "cookies-from-browser",
            "private account",
            "this account is private",
            "unable to extract universal data",
            "your ip address is blocked",
            "confirm you are not a robot",
            "please wait",
        ),
        AuthenticationRequiredError,
    ),
    (
        (
            "available in your country",
            "geo restricted",
            "geo-restricted",
            "not available from your location",
            "blocked in your country",
        ),
        GeoRestrictedError,
    ),
    (
        (
            "video has been removed",
            "account associated with this video has been terminated",
            "this account has been suspended",
            "has been deleted",
            "no longer exists",
            "404",
            "not found",
        ),
        ContentRemovedError,
    ),
    (
        (
            "video unavailable",
            "this video is private",
            "private video",
            "members-only",
            "sign in to confirm your age",
            "age-restricted",
            "who has blocked it",
            "this live event has ended",
            "drm",
            "paid members",
            "requires purchase",
        ),
        MetadataUnavailableError,
    ),
)

_RATE_LIMIT_MARKERS: Final[tuple[str, ...]] = (
    "429",
    "too many requests",
    "rate limit",
    "rate-limit",
)
"""Checked before the generic transient list: "wait" is a useful
instruction and "the site is having trouble" is not."""

_TRANSIENT_MARKERS: Final[tuple[str, ...]] = (
    "temporarily unavailable",
    "service unavailable",
    "internal server error",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "unable to connect",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "incomplete read",
    "unable to download",
    "giving up after",
    "http error 5",
)

_EXCEPTION_NAME_MAP: Final[tuple[tuple[frozenset[str], type[DownloadError]], ...]] = (
    (frozenset({"UnsupportedError"}), UnsupportedProviderError),
    # yt-dlp's own class, mapped to ours of the same name: it knows the source
    # is geo-blocked more reliably than any phrase match can.
    (frozenset({"GeoRestrictedError"}), GeoRestrictedError),
    (frozenset({"AgeRestrictedError", "UnavailableVideoError"}), MetadataUnavailableError),
)
"""Engine exception class names, mapped to what they mean here."""
_TRANSIENT_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    {"TimeoutError", "ConnectionError", "ConnectionResetError", "socket.timeout", "IncompleteRead"}
)


def _exception_names(exc: BaseException) -> Sequence[str]:
    """Return the class names of the exception and everything it wraps."""
    names: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return names


def _messages(exc: BaseException) -> str:
    """Return the lower-cased text of the exception and everything it wraps."""
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return " ".join(parts).lower()


def extract_retry_after(text: str) -> float | None:
    """Return the delay a provider explicitly asked for, if it stated one."""
    match = _RETRY_AFTER_PATTERN.search(text)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:  # pragma: no cover - the pattern guarantees a number
        return None
    return value if value > 0 else None


def _by_exception_name(names: set[str]) -> type[DownloadError] | None:
    """Return the error an engine exception class name implies, if any.

    A table rather than a chain of conditions, so adding a case is a line here
    instead of another branch inside :func:`classify`. Names, not classes: an
    ``isinstance`` check against a class yt-dlp has since moved stops matching
    silently, while a name check degrades to the safe default.
    """
    for engine_names, factory in _EXCEPTION_NAME_MAP:
        if names & engine_names:
            return factory
    return None


def classify(exc: BaseException, *, url: str, provider: str | None = None) -> DownloadError:
    """Map a yt-dlp exception to a typed, classified MediaHub error.

    Args:
        exc: Whatever the engine raised.
        url: The URL being processed, for the message.
        provider: The extractor involved, when known.

    Returns:
        A :class:`~mediahub.application.download.errors.DownloadError` subclass.
        Never ``None``, and never a raw exception: the caller is guaranteed a
        classified failure.
    """
    if isinstance(exc, DownloadError):
        return exc

    names = set(_exception_names(exc))
    text = _messages(exc)
    detail = str(exc).strip() or type(exc).__name__
    retry_after = extract_retry_after(text)

    named = _by_exception_name(names)
    if named is not None:
        return named(f"'{url}' cannot be retrieved: {detail}", provider=provider)

    for needles, factory in _PERMANENT_MARKERS:
        if any(needle in text for needle in needles):
            return factory(f"'{url}' cannot be retrieved: {detail}", provider=provider)

    if any(needle in text for needle in _RATE_LIMIT_MARKERS):
        return RateLimitedError(
            f"'{url}' is being rate limited: {detail}",
            provider=provider,
            retry_after_seconds=retry_after,
        )

    if names & _TRANSIENT_EXCEPTIONS or any(needle in text for needle in _TRANSIENT_MARKERS):
        return ProviderError(
            f"'{url}' failed transiently: {detail}",
            provider=provider,
            retry_after_seconds=retry_after,
        )

    # Unknown: one honest retry, then a dead letter someone can read.
    return DownloadFailedError(
        f"'{url}' failed: {detail}", provider=provider, retry_after_seconds=retry_after
    )
