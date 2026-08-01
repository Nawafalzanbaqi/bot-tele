"""The URL policy - which sources MediaHub is willing to fetch.

This is the SSRF gate (``docs/architecture/14-security-architecture.md`` §14.3).
It is split in two halves for a reason:

* :meth:`UrlPolicy.validate` is **pure syntax**: scheme, length, credentials,
  host shape, port. It runs in the domain, with no I/O.
* :meth:`UrlPolicy.check_address` classifies a single resolved IP address. It is
  also pure - the caller performs the DNS lookup and feeds each answer in.

Splitting it this way keeps the *rules* in the domain (one place, testable
without a network) while the *resolution* stays in an adapter, where it belongs.
An address literal in the URL is checked immediately by :meth:`validate`, so a
caller that forgets to resolve still cannot reach ``127.0.0.1``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import ClassVar, Final
from urllib.parse import urlsplit, urlunsplit

from mediahub.domain.sources.errors import (
    BlockedAddressError,
    InvalidUrlError,
    UnsupportedSchemeError,
)
from mediahub.domain.sources.value_objects import ValidatedUrl

DEFAULT_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
DEFAULT_ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443, 8080, 8443})

_MAX_URL_LENGTH: Final[int] = 2048
_MAX_HOST_LENGTH: Final[int] = 253
_MIN_PORT: Final[int] = 1
_MAX_PORT: Final[int] = 65535


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    """Decides whether a URL may be fetched at all.

    Attributes:
        allowed_schemes: Schemes the system will fetch. Anything else - and in
            particular ``file``, ``ftp`` and ``data`` - is refused outright.
        allowed_ports: Explicit ports that may be used. Default-port URLs (no
            port in the authority) are always allowed.
        block_private_networks: Refuse addresses in loopback, private,
            link-local, multicast and reserved ranges. **Defaults to true.**
            Disabling it is a deliberate, auditable choice for an operator who
            wants to fetch from their own NAS.
        max_length: Longest URL accepted.
    """

    allowed_schemes: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_SCHEMES)
    allowed_ports: frozenset[int] = field(default_factory=lambda: DEFAULT_ALLOWED_PORTS)
    block_private_networks: bool = True
    max_length: int = _MAX_URL_LENGTH

    CREDENTIAL_MARKER: ClassVar[str] = "@"

    def validate(self, raw: str) -> ValidatedUrl:
        """Check a submitted URL and return its canonical form.

        Args:
            raw: The value exactly as the user supplied it.

        Returns:
            The validated, canonical URL.

        Raises:
            UnsupportedSchemeError: If the scheme is not allowed.
            BlockedAddressError: If the host is a literal address in a blocked
                range.
            InvalidUrlError: For every other rejection.
        """
        candidate = (raw or "").strip()
        if not candidate:
            message = "the value is empty"
            raise InvalidUrlError(candidate, message)
        if len(candidate) > self.max_length:
            message = f"longer than {self.max_length} characters"
            raise InvalidUrlError(candidate, message)
        if any(char in candidate for char in ("\n", "\r", "\t", " ")):
            message = "it contains whitespace or control characters"
            raise InvalidUrlError(candidate, message)

        parts = urlsplit(candidate)
        scheme = parts.scheme.lower()
        if scheme not in self.allowed_schemes:
            allowed = ", ".join(sorted(self.allowed_schemes))
            message = f"scheme must be one of {allowed}"
            raise UnsupportedSchemeError(candidate, message)

        if parts.username or parts.password:
            message = "embedded credentials are not allowed"
            raise InvalidUrlError(candidate, message)

        host = (parts.hostname or "").lower()
        if not host:
            message = "the host is missing"
            raise InvalidUrlError(candidate, message)
        if len(host) > _MAX_HOST_LENGTH:
            message = "the host is implausibly long"
            raise InvalidUrlError(candidate, message)

        port = self._validated_port(candidate, parts.port)
        self._reject_blocked_literal(candidate, host)

        canonical = urlunsplit((scheme, parts.netloc.lower(), parts.path, parts.query, ""))
        return ValidatedUrl(value=canonical, host=host, port=port, scheme=scheme)

    def check_address(self, url: ValidatedUrl, address: str) -> None:
        """Reject a resolved address that must never be connected to.

        Called by an adapter once per DNS answer, and again for every redirect
        target. Splitting resolution from classification is what allows the rule
        to be unit-tested against a corpus of hostile addresses with no network.

        Args:
            url: The URL being checked, for the error message.
            address: A single resolved address, as text.

        Raises:
            BlockedAddressError: If the address is in a forbidden range, or is
                not a parsable address at all.
        """
        if not self.block_private_networks:
            return
        try:
            parsed = ip_address(address)
        except ValueError as exc:
            message = "the resolved address could not be parsed"
            raise BlockedAddressError(url.value, address, message) from exc

        reason = self.classify_address(parsed)
        if reason is not None:
            raise BlockedAddressError(url.value, address, reason)

    @staticmethod
    def classify_address(parsed: IPv4Address | IPv6Address) -> str | None:
        """Return why an address is forbidden, or ``None`` if it is allowed.

        Link-local is called out separately because that is where cloud
        metadata services live (``169.254.169.254``), and reaching one is the
        classic SSRF payoff.
        """
        # Order matters only for the message: several ranges satisfy more than
        # one predicate (240.0.0.0/4 is both reserved and, to Python, private),
        # and the most specific label is the more useful diagnostic.
        if parsed.is_loopback:
            return "loopback addresses are not reachable sources"
        if parsed.is_link_local:
            return "link-local addresses are not reachable sources"
        if parsed.is_multicast:
            return "multicast addresses are not reachable sources"
        if parsed.is_reserved or parsed.is_unspecified:
            return "reserved addresses are not reachable sources"
        if parsed.is_private:
            return "private addresses are not reachable sources"
        return None

    def _validated_port(self, candidate: str, port: int | None) -> int | None:
        """Return the explicit port after checking it, or ``None``."""
        if port is None:
            return None
        if not _MIN_PORT <= port <= _MAX_PORT:
            message = "the port is out of range"
            raise InvalidUrlError(candidate, message)
        if port not in self.allowed_ports:
            allowed = ", ".join(str(value) for value in sorted(self.allowed_ports))
            message = f"port {port} is not allowed (permitted: {allowed})"
            raise InvalidUrlError(candidate, message)
        return port

    def _reject_blocked_literal(self, candidate: str, host: str) -> None:
        """Refuse a host that is already an address in a forbidden range."""
        if not self.block_private_networks:
            return
        try:
            parsed = ip_address(host.strip("[]"))
        except ValueError:
            return  # A hostname; resolution happens in the adapter.
        reason = self.classify_address(parsed)
        if reason is not None:
            raise BlockedAddressError(candidate, host, reason)
