"""The DNS half of the SSRF gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.domain.sources.errors import BlockedAddressError
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.infrastructure.security.address_guard import DnsAddressGuard

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.unit


def guard_returning(*addresses: str) -> DnsAddressGuard:
    """Build a guard whose resolver answers with fixed addresses."""

    def resolver(host: str, port: int) -> Sequence[str]:
        del host, port
        return list(addresses)

    return DnsAddressGuard(UrlPolicy(), resolver=resolver)


def a_url() -> object:
    """Return a validated URL to check."""
    return UrlPolicy().validate("https://example.com/a")


def test_public_addresses_pass() -> None:
    addresses = guard_returning("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946").check(
        a_url()  # type: ignore[arg-type]
    )

    assert len(addresses) == 2


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "192.168.0.5", "169.254.169.254", "::1", "fe80::1"],
)
def test_a_forbidden_answer_is_refused(address: str) -> None:
    with pytest.raises(BlockedAddressError):
        guard_returning(address).check(a_url())  # type: ignore[arg-type]


def test_every_answer_is_checked_not_just_the_first() -> None:
    # A host with several A records only needs one to point inside the network.
    with pytest.raises(BlockedAddressError):
        guard_returning("93.184.216.34", "127.0.0.1").check(a_url())  # type: ignore[arg-type]


def test_an_unresolvable_host_is_refused() -> None:
    with pytest.raises(BlockedAddressError):
        guard_returning().check(a_url())  # type: ignore[arg-type]


def test_garbage_answers_are_refused() -> None:
    with pytest.raises(BlockedAddressError):
        guard_returning("not-an-address").check(a_url())  # type: ignore[arg-type]


def test_permissive_policy_allows_private_answers() -> None:
    policy = UrlPolicy(block_private_networks=False)
    guard = DnsAddressGuard(policy, resolver=lambda host, port: ["192.168.1.10"])

    assert guard.check(policy.validate("https://nas.example.com/a")) == ("192.168.1.10",)
