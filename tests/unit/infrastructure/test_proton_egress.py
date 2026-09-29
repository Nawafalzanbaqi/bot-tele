"""The ProtonVPN tier: one tunnel, moved between countries, held while in use."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mediahub.application.download.errors import ProviderError
from mediahub.infrastructure.download.shared.proton import COUNTRIES, ProtonEgress

pytestmark = pytest.mark.unit


class FakeControl:
    """A control server whose exit moves to the requested country after ``delay`` polls."""

    def __init__(self, *, delay: int = 1, refuse: set[str] | None = None) -> None:
        self.country = "Netherlands"
        self.requested: list[str] = []
        self.delay = delay
        self.refuse = refuse or set()
        self._pending: str | None = None
        self._countdown = 0

    async def get(self, path: str) -> dict[str, Any]:
        assert path == "/v1/publicip/ip"
        if self._pending is not None:
            self._countdown -= 1
            if self._countdown <= 0:
                self.country = self._pending
                self._pending = None
        return {"public_ip": "203.0.113.9", "country": self.country, "city": "Somewhere"}

    async def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        assert path == "/v1/vpn/settings"
        wanted = body["provider"]["server_selection"]["countries"][0]
        self.requested.append(wanted)
        if wanted in self.refuse:
            message = "control server said no"
            raise RuntimeError(message)
        self._pending = wanted
        self._countdown = self.delay
        return {"outcome": "settings updated"}


def egress(control: FakeControl, **overrides: Any) -> ProtonEgress:
    options: dict[str, Any] = {
        "proxy": "http://vpn-proton:8888",
        "control": control,
        "countries": ("nl", "pl", "ro"),
        "switch_timeout_seconds": 1.0,
        "poll_interval_seconds": 0.01,
    }
    options.update(overrides)
    return ProtonEgress(**options)


class TestHolding:
    async def test_a_hold_moves_the_exit_and_yields_the_proxy(self) -> None:
        control = FakeControl()
        tier = egress(control)

        async with tier.hold("pl") as proxy:
            assert proxy == "http://vpn-proton:8888"
            assert control.country == "Poland"
            assert tier.current == "pl"

        assert control.requested == ["Poland"]

    async def test_the_same_country_is_not_switched_again(self) -> None:
        control = FakeControl()
        tier = egress(control)
        async with tier.hold("pl"):
            pass
        async with tier.hold("pl"):
            pass

        assert control.requested == ["Poland"], "one switch, the second hold reused it"

    async def test_codes_are_case_insensitive(self) -> None:
        tier = egress(FakeControl())
        async with tier.hold("RO"):
            assert tier.current == "ro"

    async def test_an_unknown_code_is_a_programming_error(self) -> None:
        tier = egress(FakeControl())
        with pytest.raises(ValueError, match="not a configured"):
            async with tier.hold("us"):
                pass

    async def test_unknown_countries_are_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="unknown ProtonVPN country"):
            egress(FakeControl(), countries=("nl", "xx"))

    def test_the_known_countries_are_the_free_tier_ones(self) -> None:
        assert set(COUNTRIES) == {"nl", "pl", "ro"}


class TestFailures:
    async def test_a_refused_switch_is_a_provider_error(self) -> None:
        control = FakeControl(refuse={"Romania"})
        tier = egress(control)

        with pytest.raises(ProviderError, match="refused the switch"):
            async with tier.hold("ro"):
                pass
        assert tier.current is None

    async def test_an_exit_that_never_arrives_times_out(self) -> None:
        control = FakeControl(delay=10_000)
        tier = egress(control, switch_timeout_seconds=0.05)

        with pytest.raises(ProviderError, match="did not reach Poland"):
            async with tier.hold("pl"):
                pass
        assert tier.current is None

    async def test_a_control_server_that_cannot_be_read_is_not_a_crash(self) -> None:
        class DeafControl(FakeControl):
            async def get(self, path: str) -> dict[str, Any]:
                message = "no route"
                raise ConnectionError(message)

        tier = egress(DeafControl(), switch_timeout_seconds=0.05)

        with pytest.raises(ProviderError):
            async with tier.hold("pl"):
                pass


class TestTheTunnelIsHeld:
    async def test_a_second_hold_waits_for_the_first(self) -> None:
        """The exit is global: nobody may move it under a running fetch."""
        control = FakeControl()
        tier = egress(control)
        order: list[str] = []
        first_in = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with tier.hold("pl"):
                order.append("first-in")
                first_in.set()
                await release_first.wait()
                order.append("first-out")

        async def second() -> None:
            await first_in.wait()
            async with tier.hold("ro"):
                order.append("second-in")

        tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
        await first_in.wait()
        await asyncio.sleep(0.05)
        assert order == ["first-in"], "the second hold is blocked while the first runs"
        assert control.country == "Poland"
        release_first.set()
        await asyncio.gather(*tasks)

        assert order == ["first-in", "first-out", "second-in"]
        assert control.requested == ["Poland", "Romania"]
