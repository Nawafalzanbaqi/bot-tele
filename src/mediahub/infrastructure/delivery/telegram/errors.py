"""Maps Telegram failures into MediaHub's delivery taxonomy.

Rate limiting is **normal traffic** for a bot, not an incident: a ``429`` with
``retry_after`` is the service telling you exactly when to come back, and
obeying it is cheaper and politer than guessing.

Classification is duck-typed on exception class names and message content
rather than by importing the client library's exception classes, for the same
reason as the download engine: a library reorganisation silently breaks an
``isinstance`` check, while a name check degrades to the safe default.

The safe default is **transient**. An unfamiliar failure gets one honest
attempt later rather than being written off.
"""

from __future__ import annotations

import re
from typing import Final

from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    DeliveryAuthenticationError,
    DeliveryError,
    DeliveryProviderError,
    DeliveryQuotaExceededError,
    DeliveryRateLimitedError,
    ProviderUnavailableError,
    TargetUnreachableError,
)

PROVIDER: Final[str] = "telegram"

_RETRY_AFTER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"retry[ _-]?(?:after|in)[^0-9]{0,10}(\d+(?:\.\d+)?)", re.IGNORECASE
)

_AUTHENTICATION_MARKERS: Final[tuple[str, ...]] = (
    "unauthorized",
    "invalid token",
    "token is invalid",
)
_RATE_LIMIT_MARKERS: Final[tuple[str, ...]] = (
    "too many requests",
    "flood",
    "retry after",
)
_QUOTA_MARKERS: Final[tuple[str, ...]] = (
    "quota",
    "limit exceeded",
    "storage full",
)
_TOO_LARGE_MARKERS: Final[tuple[str, ...]] = (
    "request entity too large",
    "file is too big",
    "too large for a bot",
)
_UNREACHABLE_MARKERS: Final[tuple[str, ...]] = (
    "chat not found",
    "bot was blocked",
    "user is deactivated",
    "bot was kicked",
    "have no rights to send",
    "not enough rights",
    "peer_id_invalid",
    "chat_write_forbidden",
    "forbidden",
)
_UNAVAILABLE_MARKERS: Final[tuple[str, ...]] = (
    "temporarily unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "service unavailable",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "network is unreachable",
)

# Status codes are matched as whole tokens, never as substrings. A bare "413"
# search hits a byte count, a lease identifier or a temporary path, and a
# misread there turns weather into a permanent refusal - or worse, the reverse.
_STATUS_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])([1-5]\d\d)(?![\w.])")

_AUTHENTICATION_CODES: Final[frozenset[str]] = frozenset({"401"})
_RATE_LIMIT_CODES: Final[frozenset[str]] = frozenset({"429"})
_QUOTA_CODES: Final[frozenset[str]] = frozenset()
_TOO_LARGE_CODES: Final[frozenset[str]] = frozenset({"413"})
_UNREACHABLE_CODES: Final[frozenset[str]] = frozenset({"403"})
_UNAVAILABLE_CODES: Final[frozenset[str]] = frozenset({"502", "503", "504"})


def extract_retry_after(text: str) -> float | None:
    """Return the delay Telegram asked for, if it stated one."""
    match = _RETRY_AFTER_PATTERN.search(text)
    if match is None:
        return None
    value = float(match.group(1))
    return value if value > 0 else None


def _chain_text(exc: BaseException) -> str:
    """Return the lower-cased text of the exception and everything it wraps."""
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return " ".join(parts).lower()


def classify(exc: BaseException, *, detail: str = "") -> DeliveryError:
    """Map a Telegram client failure to a typed, classified delivery error.

    Args:
        exc: Whatever the client raised.
        detail: Short context for the message. **Never** a file path, a token or
            a URL - delivery errors are shown to people.

    Returns:
        A :class:`~mediahub.application.delivery.errors.DeliveryError` subclass.
        Never ``None``, and never a raw exception.
    """
    if isinstance(exc, DeliveryError):
        return exc

    text = _chain_text(exc)
    suffix = f" ({detail})" if detail else ""
    retry_after = extract_retry_after(text)
    codes = _status_codes(text)

    if _matches(text, _TOO_LARGE_MARKERS, codes, _TOO_LARGE_CODES):
        # The provider checks the size up front, so reaching here means the
        # destination's real limit is lower than the one we advertise.
        return ArtifactTooLargeError(0, 0, provider=PROVIDER)

    # Ordered: the first matching rule wins, so the most consequential
    # diagnoses come first. Authentication leads because it invalidates every
    # reference this account ever issued, and an operator needs to know that
    # rather than watch deliveries fail one at a time.
    for markers, rule_codes, message, factory in _RULES:
        if _matches(text, markers, codes, rule_codes):
            return factory(
                f"{message}{suffix}.", provider=PROVIDER, retry_after_seconds=retry_after
            )

    return DeliveryProviderError(
        f"Delivery failed{suffix}.", provider=PROVIDER, retry_after_seconds=retry_after
    )


_Rule = tuple[tuple[str, ...], frozenset[str], str, type[DeliveryError]]

_RULES: Final[tuple[_Rule, ...]] = (
    (
        _AUTHENTICATION_MARKERS,
        _AUTHENTICATION_CODES,
        "The destination rejected our credentials",
        DeliveryAuthenticationError,
    ),
    (
        _RATE_LIMIT_MARKERS,
        _RATE_LIMIT_CODES,
        "The destination asked us to slow down",
        DeliveryRateLimitedError,
    ),
    (
        _QUOTA_MARKERS,
        _QUOTA_CODES,
        "The destination's quota is exhausted",
        DeliveryQuotaExceededError,
    ),
    (
        _UNREACHABLE_MARKERS,
        _UNREACHABLE_CODES,
        "The destination will not accept messages",
        TargetUnreachableError,
    ),
    (
        _UNAVAILABLE_MARKERS,
        _UNAVAILABLE_CODES,
        "The destination is temporarily unavailable",
        ProviderUnavailableError,
    ),
)


def _status_codes(text: str) -> frozenset[str]:
    """Return every standalone three-digit status token in the text."""
    return frozenset(_STATUS_PATTERN.findall(text))


def _matches(
    text: str, markers: tuple[str, ...], codes: frozenset[str], rule_codes: frozenset[str]
) -> bool:
    """Return whether the text carries one of a rule's phrases or status codes."""
    return any(marker in text for marker in markers) or bool(codes & rule_codes)
