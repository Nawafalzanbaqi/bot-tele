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
    DownloadError,
    DownloadFailedError,
    FormatUnavailableError,
    MetadataUnavailableError,
    ProviderError,
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
    (
        (
            "video unavailable",
            "this video is private",
            "private video",
            "members-only",
            "sign in to confirm your age",
            "age-restricted",
            "who has blocked it",
            "available in your country",
            "geo restricted",
            "geo-restricted",
            "video has been removed",
            "account associated with this video has been terminated",
            "this live event has ended",
            "drm",
            "paid members",
            "requires purchase",
            "404",
            "not found",
        ),
        MetadataUnavailableError,
    ),
)

_TRANSIENT_MARKERS: Final[tuple[str, ...]] = (
    "429",
    "too many requests",
    "rate limit",
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

_UNSUPPORTED_EXCEPTIONS: Final[frozenset[str]] = frozenset({"UnsupportedError"})
_PERMANENT_EXCEPTIONS: Final[frozenset[str]] = frozenset(
    {"GeoRestrictedError", "AgeRestrictedError", "UnavailableVideoError"}
)
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

    if names & _UNSUPPORTED_EXCEPTIONS:
        return UnsupportedProviderError(f"No extractor supports '{url}'.", provider=provider)
    if names & _PERMANENT_EXCEPTIONS:
        return MetadataUnavailableError(f"'{url}' cannot be retrieved: {detail}", provider=provider)

    for needles, factory in _PERMANENT_MARKERS:
        if any(needle in text for needle in needles):
            return factory(f"'{url}' cannot be retrieved: {detail}", provider=provider)

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
