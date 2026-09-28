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
    ConnectionBlockedError,
    ContentRemovedError,
    DownloadError,
    DownloadFailedError,
    DrmProtectedError,
    FormatUnavailableError,
    GeoRestrictedError,
    MetadataUnavailableError,
    NoPlayableMediaError,
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
    # Encrypted by design. First, because the phrases below can be accompanied
    # by "no video formats found" and that must not be read as a photo post.
    (
        (
            "drm protected",
            "drm-protected",
            "is drm",
            "protected by drm",
            "widevine",
            "fairplay",
            "playready",
        ),
        DrmProtectedError,
    ),
    (("unsupported url", "no suitable extractor", "is not a valid url"), UnsupportedProviderError),
    # A rendition that was asked for and does not exist - a stale format id from
    # an old probe. Deliberately *not* "no formats found", which means the
    # source offers nothing at all and is classified below: telling someone
    # their quality choice was unavailable, when the post simply has no video in
    # it, sends them to pick a different quality from a list that is empty.
    (
        ("requested format is not available", "requested format not available"),
        FormatUnavailableError,
    ),
    # A post that was read successfully and simply has no stream in it. This is
    # what every one of these platforms says about a **photo-only post**, and
    # the wording invites two wrong readings: that the post is gone, or that a
    # sign-in would reveal it. Neither is true, and the second is the expensive
    # one - it sends someone to re-export cookies that were never the problem.
    #
    # Matched before the session markers below, which is a reversal of what this
    # table did previously. The old order was chosen because X returns this
    # phrase to a signed-out visitor for a tweet that does have a video; but a
    # signed-in session meets it far more often on ordinary photo posts, so
    # reading it as an authentication failure is now wrong in the common case.
    # The renderer restores the missing nuance from something this module cannot
    # see: whether a session for that platform is actually stored.
    (
        (
            "no video could be found in this tweet",
            "no video could be found in this post",
            "no video formats found",
            "no formats found",
            "no media found",
            "there's no video in this post",
        ),
        NoPlayableMediaError,
    ),
    # Session-gated. These phrases are what the platforms actually say when
    # they are hiding something from a signed-out visitor. An age gate lands
    # here too, deliberately: "Sign in to confirm your age" is fixed by the
    # cookies of a verified account, so it is reported as something the user
    # can act on. "please wait" used to be listed here; it is a throttling
    # phrase ("Please wait a few minutes before you try again") and is now
    # classified as rate limiting, where it is retried after a pause instead
    # of being declared a permanent login failure.
    (
        (
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
            "confirm you're not a bot",
            # YouTube's own wording uses the typographic apostrophe.
            "confirm you’re not a bot",  # noqa: RUF001
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
            # The status code as a phrase, never as a bare substring: "404" on
            # its own also matches a byte count, a lease id or a video id.
            "http error 404",
            "404: not found",
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
            "age-restricted",
            "who has blocked it",
            "this live event has ended",
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
    # "Please wait a few minutes before you try again" is YouTube throttling a
    # client, not a login wall; retried after a pause, like the rest of these.
    "please wait",
)
"""Checked before the generic transient list: "wait" is a useful
instruction and "the site is having trouble" is not."""

_RESET_MARKERS: Final[tuple[str, ...]] = (
    "connection reset by peer",
    "errno 104",
    "econnreset",
)
"""A connection that opened and was then killed mid-handshake.

Kept apart from the ordinary transient list because the honest explanation is
different. A site under load answers slowly or returns a 5xx; it does not accept
a TCP connection and then reset the TLS handshake. That pattern - DNS fine, TCP
fine, handshake reset, every time - is something in the network path reading the
hostname and cutting the connection, and no amount of retrying reaches past it.

Still classified transient, because a single reset really can be noise. What
changes is what the user is told after the retries are spent."""

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

    return _classify_transient(
        names,
        text,
        url=url,
        detail=detail,
        provider=provider,
        retry_after=retry_after,
    )


def _classify_transient(
    names: set[str],
    text: str,
    *,
    url: str,
    detail: str,
    provider: str | None,
    retry_after: float | None,
) -> DownloadError:
    """Grade a failure that may resolve on its own.

    All four outcomes are retryable; they differ only in what they let a caller
    say afterwards, which is the whole value of separating them. "Wait a
    moment", "something is blocking this connection" and "the site is having
    trouble" send a person to three different places.
    """
    if any(needle in text for needle in _RATE_LIMIT_MARKERS):
        return RateLimitedError(
            f"'{url}' is being rate limited: {detail}",
            provider=provider,
            retry_after_seconds=retry_after,
        )

    if any(needle in text for needle in _RESET_MARKERS):
        return ConnectionBlockedError(
            f"'{url}' was reset before any data arrived: {detail}",
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
