"""One free ProtonVPN tunnel whose exit country is switched on demand.

The third egress tier. Its shape is dictated by the account: a free Proton
account allows **one** connection, so there is one gluetun container, and the
exit country is moved through gluetun's control server rather than by running
a container per country. Moving the exit takes ten to thirty seconds and is
global state - a request that switches the country under another request's
download would break it - so the tunnel is *held*: whoever needs it takes a
lock, asks for a country, and keeps the lock until their fetch is done.

The control server is API-key protected (gluetun's roles file); the key never
appears in a log line. Talking to it goes through a small transport protocol so
the tests can drive the switch without a network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Final, Protocol

from loguru import logger

from mediahub.application.download.errors import ProviderError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncIterator, Mapping, Sequence

COUNTRIES: Final[dict[str, str]] = {"nl": "Netherlands", "pl": "Poland", "ro": "Romania"}
"""Country codes the bot accepts, and the names gluetun's server list uses.

ProtonVPN Free offers these three in Europe (and a few outside it that this
deployment does not use). The order of the tuple in the settings is the order
they are tried in.
"""

COUNTRY_NAMES_AR: Final[dict[str, str]] = {"nl": "هولندا", "pl": "بولندا", "ro": "رومانيا"}

SWITCH_TIMEOUT_SECONDS: Final[float] = 90.0
"""How long a country switch may take before it is reported as failed.

A reconnect is usually done in 10-30 s; the free tier's servers are busy and
occasionally slower. Past this the tunnel is treated as unavailable for that
country and the next one is tried.
"""

POLL_INTERVAL_SECONDS: Final[float] = 2.0


class ControlTransport(Protocol):
    """The two calls the switcher makes against gluetun's control server."""

    async def get(self, path: str) -> Mapping[str, Any]:
        """GET a JSON document."""
        ...

    async def put(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """PUT a JSON document and return the JSON reply."""
        ...


class UrllibControl:
    """gluetun's control server over HTTP, with the API key on every request.

    Standard library on purpose: the infrastructure layer keeps to the three
    third-party packages it already has, and two tiny JSON calls do not earn a
    fourth. The blocking call runs on a worker thread.
    """

    __slots__ = ("_base", "_key", "_timeout")

    def __init__(self, base_url: str, api_key: str, *, timeout_seconds: float = 20.0) -> None:
        """Bind to the server. The key is held here and sent as ``X-API-Key``."""
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout_seconds

    async def get(self, path: str) -> Mapping[str, Any]:
        """GET a JSON document."""
        return await asyncio.to_thread(self._call, "GET", path, None)

    async def put(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """PUT a JSON document and return the JSON reply (or an empty one)."""
        return await asyncio.to_thread(self._call, "PUT", path, dict(body))

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> Mapping[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - fixed base URL, literal paths
            self._base + path,
            data=data,
            method=method,
            headers={"X-API-Key": self._key, "Content-Type": "application/json"},
        )
        # security: the control server is a fixed, configured base URL on the
        # container network; the path is one of two literals in this module.
        with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            raw = response.read()
        if not raw:
            return {}
        return _as_mapping(json.loads(raw.decode("utf-8", errors="replace")))


class ProtonEgress:
    """Holds the single free tunnel and points its exit at a country."""

    __slots__ = (
        "_control",
        "_countries",
        "_current",
        "_lock",
        "_poll",
        "_proxy",
        "_switch_timeout",
    )

    def __init__(
        self,
        *,
        proxy: str,
        control: ControlTransport,
        countries: Sequence[str] = ("nl", "pl", "ro"),
        switch_timeout_seconds: float = SWITCH_TIMEOUT_SECONDS,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        """Bind the tier to its proxy, its control server and the countries it may use.

        Args:
            proxy: The HTTP proxy the engine uses once the tunnel is up.
            control: The control-server transport.
            countries: Codes from :data:`COUNTRIES`, in the order to try them.
            switch_timeout_seconds: How long a switch may take.
            poll_interval_seconds: How often the exit is checked during a switch.
        """
        unknown = [code for code in countries if code not in COUNTRIES]
        if unknown:
            message = f"unknown ProtonVPN country code(s): {', '.join(unknown)}"
            raise ValueError(message)
        self._proxy = proxy
        self._control = control
        self._countries = tuple(dict.fromkeys(code.lower() for code in countries))
        self._current: str | None = None
        self._lock = asyncio.Lock()
        self._switch_timeout = switch_timeout_seconds
        self._poll = poll_interval_seconds

    @property
    def proxy(self) -> str:
        """Return the proxy URL the engine uses for this tier."""
        return self._proxy

    @property
    def countries(self) -> tuple[str, ...]:
        """Return the country codes in the order they are tried."""
        return self._countries

    @property
    def current(self) -> str | None:
        """Return the code the exit was last confirmed to be in, if known."""
        return self._current

    @contextlib.asynccontextmanager
    async def hold(self, code: str) -> AsyncIterator[str]:
        """Take the tunnel, point it at ``code``, and yield the proxy to use.

        The lock is held for the whole block, because the exit is global: a
        download in progress through Poland must not find itself in Romania.

        Raises:
            ValueError: If ``code`` is not one of the configured countries.
            ProviderError: If the exit could not be moved there in time.
        """
        code = code.lower()
        if code not in self._countries:
            message = f"'{code}' is not a configured ProtonVPN country"
            raise ValueError(message)
        async with self._lock:
            if self._current != code:
                await self._switch(code)
            yield self._proxy

    async def _switch(self, code: str) -> None:
        """Ask gluetun for ``code`` and wait until the public exit is there."""
        country = COUNTRIES[code]
        started = time.monotonic()
        try:
            await self._control.put(
                "/v1/vpn/settings",
                {"provider": {"server_selection": {"countries": [country]}}},
            )
        except Exception as exc:
            self._current = None
            message = f"the ProtonVPN control server refused the switch to {country}"
            raise ProviderError(message, provider="protonvpn") from exc

        deadline = started + self._switch_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll)
            seen = await self._country_now()
            if seen is not None and _same_country(seen, country):
                self._current = code
                logger.bind(country=code, seconds=round(time.monotonic() - started, 1)).info(
                    "ProtonVPN exit moved"
                )
                return
        self._current = None
        message = f"the ProtonVPN tunnel did not reach {country} within {self._switch_timeout:.0f}s"
        raise ProviderError(message, provider="protonvpn")

    async def _country_now(self) -> str | None:
        """Return the country of the current public exit, as gluetun sees it."""
        try:
            document = await self._control.get("/v1/publicip/ip")
        except Exception:
            return None
        value = document.get("country")
        return str(value) if value else None


def _same_country(seen: str, wanted: str) -> bool:
    """Compare gluetun's country field with a server-list name, leniently.

    The public-IP lookup returns a country *name*; the server list uses names
    too, but the two sources do not always spell them identically.
    """
    a, b = seen.strip().lower(), wanted.strip().lower()
    return a == b or a.startswith(b[:4]) or b.startswith(a[:4])


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Return a JSON object as a mapping; anything else is treated as empty."""
    return value if isinstance(value, dict) else {}
