"""Resolves a host and applies the URL policy to every answer.

This is the I/O half of the SSRF gate. The rules are in
:class:`~mediahub.domain.sources.policies.UrlPolicy`; this adapter performs the
lookup and feeds each resolved address through them.

**Every** answer is checked, not just the first. A hostname with several A
records only needs one of them to point at ``127.0.0.1`` for a permissive
implementation to be useless.

Known limitation, stated rather than hidden: this guard validates the URL that
is submitted. Once the download engine takes over, it performs its own
redirects and fetches media from CDN URLs it discovered itself, and those are
outside this check. Closing that gap requires installing a custom HTTP handler
inside the engine and is recorded as follow-up work in the Phase 03 notes. The
pre-flight check still removes the entire class of attacks that begin with a
user pasting an internal address.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.domain.sources.errors import BlockedAddressError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.domain.sources.policies import UrlPolicy
    from mediahub.domain.sources.value_objects import ValidatedUrl

DEFAULT_RESOLVE_TIMEOUT_SECONDS = 5.0


class DnsAddressGuard:
    """Checks that a URL's host resolves only to addresses we may connect to."""

    __slots__ = ("_policy", "_resolver", "_timeout")

    def __init__(
        self,
        policy: UrlPolicy,
        *,
        timeout_seconds: float = DEFAULT_RESOLVE_TIMEOUT_SECONDS,
        resolver: object | None = None,
    ) -> None:
        """Bind the guard to a policy.

        Args:
            policy: The rules to apply to each resolved address.
            timeout_seconds: Maximum time to spend resolving.
            resolver: Optional callable ``(host, port) -> Sequence[str]`` used
                instead of the system resolver. Tests supply one; production
                does not.
        """
        self._policy = policy
        self._timeout = timeout_seconds
        self._resolver = resolver

    def check(self, url: ValidatedUrl) -> tuple[str, ...]:
        """Resolve the URL's host and reject any forbidden address.

        Args:
            url: A syntactically validated URL.

        Returns:
            Every address the host resolved to.

        Raises:
            BlockedAddressError: If any answer is in a forbidden range, or the
                host cannot be resolved at all.
        """
        addresses = self._resolve(url)
        if not addresses:
            message = "the host could not be resolved"
            raise BlockedAddressError(url.value, url.host, message)

        for address in addresses:
            self._policy.check_address(url, address)

        logger.bind(host=url.host, addresses=len(addresses)).debug("Host address check passed")
        return addresses

    def _resolve(self, url: ValidatedUrl) -> tuple[str, ...]:
        """Return every address for the URL's host."""
        port = url.port or (443 if url.scheme == "https" else 80)
        if self._resolver is not None:
            custom = self._resolver
            if callable(custom):
                answers: Sequence[str] = custom(url.host, port)
                return tuple(answers)

        previous = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self._timeout)
        try:
            infos = socket.getaddrinfo(url.host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            message = "the host could not be resolved"
            raise BlockedAddressError(url.value, url.host, message) from exc
        finally:
            socket.setdefaulttimeout(previous)

        return tuple({str(info[4][0]) for info in infos})
