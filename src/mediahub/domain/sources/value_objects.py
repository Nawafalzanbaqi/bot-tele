"""Immutable values describing a source reference."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from mediahub.domain.common.value_object import ValueObject
from mediahub.domain.sources.errors import InvalidUrlError


@dataclass(frozen=True, slots=True)
class ValidatedUrl(ValueObject):
    """A URL that has passed :class:`~mediahub.domain.sources.policies.UrlPolicy`.

    Holding one of these means the syntax, scheme, port and host shape have all
    been checked. It does **not** mean the address has been resolved - that is a
    separate, I/O-bound step performed by an adapter, because DNS is not
    something the domain may do.

    Attributes:
        value: The canonical form of the URL.
        host: The hostname, lower-cased, without credentials or port.
        port: The explicit port, if the URL carried one.
        scheme: The (lower-cased) scheme.
    """

    value: str
    host: str
    port: int | None
    scheme: str

    @classmethod
    def of(cls, canonical: str) -> ValidatedUrl:
        """Build a validated URL from an already-canonical string.

        Intended for rehydration (a URL that was validated earlier and stored).
        New input must go through
        :meth:`~mediahub.domain.sources.policies.UrlPolicy.validate`.

        Raises:
            InvalidUrlError: If the value cannot be parsed at all.
        """
        parts = urlsplit(canonical)
        if not parts.scheme or not parts.hostname:
            message = "the value is not a parsable absolute URL"
            raise InvalidUrlError(canonical, message)
        return cls(
            value=urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")),
            host=parts.hostname.lower(),
            port=parts.port,
            scheme=parts.scheme.lower(),
        )

    def __str__(self) -> str:
        """Return the canonical URL."""
        return self.value
