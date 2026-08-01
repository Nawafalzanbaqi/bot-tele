"""Failures raised while inspecting a source reference.

All of these derive from
:class:`~mediahub.domain.common.errors.InvariantViolationError`, so the HTTP
layer already maps them to ``422`` and the worker already classifies them as
policy failures - no new mapping table is needed.
"""

from __future__ import annotations

from typing import ClassVar

from mediahub.domain.common.errors import InvariantViolationError


class InvalidUrlError(InvariantViolationError):
    """The submitted string is not a URL MediaHub is willing to fetch.

    Attributes:
        raw: The rejected value, truncated for safe logging.
        reason: Why it was rejected.
    """

    code: ClassVar[str] = "invalid_url"

    MAX_ECHO_LENGTH: ClassVar[int] = 120

    def __init__(self, raw: str, reason: str) -> None:
        """Initialise the error from the rejected value and the reason."""
        echoed = raw[: self.MAX_ECHO_LENGTH]
        super().__init__(f"'{echoed}' is not a usable source URL: {reason}")
        self.raw = echoed
        self.reason = reason


class UnsupportedSchemeError(InvalidUrlError):
    """The URL uses a scheme the system will never fetch.

    ``file:``, ``ftp:``, ``data:`` and friends are refused outright: they are
    either local-filesystem access dressed as a URL, or protocols with no
    business in a media fetcher.
    """

    code: ClassVar[str] = "unsupported_url_scheme"


class BlockedAddressError(InvalidUrlError):
    """The URL resolves to an address the system must never connect to.

    This is the SSRF refusal: loopback, private ranges, link-local (which is
    where cloud metadata services live), multicast and reserved space.

    Attributes:
        address: The offending address, as text.
    """

    code: ClassVar[str] = "blocked_address"

    def __init__(self, raw: str, address: str, reason: str) -> None:
        """Initialise the error from the URL, the address and the reason."""
        super().__init__(raw, f"{reason} ({address})")
        self.address = address
