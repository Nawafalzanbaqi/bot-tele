"""The URL policy refuses everything it should before a socket is opened."""

from __future__ import annotations

from ipaddress import ip_address

import pytest

from mediahub.domain.sources.errors import (
    BlockedAddressError,
    InvalidUrlError,
    UnsupportedSchemeError,
)
from mediahub.domain.sources.policies import UrlPolicy
from mediahub.domain.sources.value_objects import ValidatedUrl

pytestmark = pytest.mark.unit


@pytest.fixture
def policy() -> UrlPolicy:
    return UrlPolicy()


class TestAcceptance:
    def test_accepts_a_plain_https_url(self, policy: UrlPolicy) -> None:
        validated = policy.validate("https://example.com/watch?v=abc")

        assert validated.value == "https://example.com/watch?v=abc"
        assert validated.host == "example.com"
        assert validated.scheme == "https"
        assert validated.port is None

    def test_normalises_case_and_drops_the_fragment(self, policy: UrlPolicy) -> None:
        validated = policy.validate("  HTTPS://Example.COM/A?b=1#section  ")

        assert validated.value == "https://example.com/A?b=1"
        assert validated.host == "example.com"

    def test_accepts_an_allowed_explicit_port(self, policy: UrlPolicy) -> None:
        assert policy.validate("https://example.com:8443/a").port == 8443


class TestSyntax:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "not a url",
            "https://",
            "http://:8080/path",
            "https://example.com/a\nb",
            "https://example.com/a b",
        ],
    )
    def test_rejects_unparsable_input(self, policy: UrlPolicy, raw: str) -> None:
        with pytest.raises(InvalidUrlError):
            policy.validate(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "file:///etc/passwd",
            "ftp://example.com/a",
            "data:text/html,<script>",
            "gopher://example.com",
            "dict://example.com:11211/",
            "jar:http://example.com/a!/b",
        ],
    )
    def test_rejects_dangerous_schemes(self, policy: UrlPolicy, raw: str) -> None:
        with pytest.raises(UnsupportedSchemeError):
            policy.validate(raw)

    def test_rejects_embedded_credentials(self, policy: UrlPolicy) -> None:
        with pytest.raises(InvalidUrlError):
            policy.validate("https://user:secret@example.com/a")

    def test_rejects_an_unlisted_port(self, policy: UrlPolicy) -> None:
        with pytest.raises(InvalidUrlError):
            policy.validate("https://example.com:11211/a")

    def test_rejects_an_overlong_url(self, policy: UrlPolicy) -> None:
        with pytest.raises(InvalidUrlError):
            policy.validate("https://example.com/" + "a" * 3000)

    def test_error_message_truncates_the_echoed_value(self, policy: UrlPolicy) -> None:
        with pytest.raises(InvalidUrlError) as excinfo:
            policy.validate("ftp://" + "x" * 500 + ".example.com")

        assert len(excinfo.value.raw) <= InvalidUrlError.MAX_ECHO_LENGTH


class TestLiteralAddresses:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "10.0.0.5",
            "172.16.4.4",
            "192.168.1.1",
            "169.254.169.254",
            "0.0.0.0",  # noqa: S104 - the point is that it must be refused
            "[::1]",
            "[fe80::1]",
            "[fc00::1]",
        ],
    )
    def test_private_and_reserved_literals_are_refused(self, policy: UrlPolicy, host: str) -> None:
        with pytest.raises(BlockedAddressError):
            policy.validate(f"http://{host}/a")

    def test_public_literal_is_allowed(self, policy: UrlPolicy) -> None:
        assert policy.validate("https://93.184.216.34/a").host == "93.184.216.34"

    def test_hostnames_are_not_classified_here(self, policy: UrlPolicy) -> None:
        # A hostname that will resolve to loopback passes syntax; the address
        # guard is what catches it. This test pins that division of labour.
        assert policy.validate("https://localtest.me/a").host == "localtest.me"


class TestAddressClassification:
    @pytest.mark.parametrize(
        ("address", "expected_reason"),
        [
            ("127.0.0.1", "loopback"),
            ("169.254.169.254", "link-local"),
            ("10.1.2.3", "private"),
            ("224.0.0.1", "multicast"),
            ("240.0.0.1", "reserved"),
        ],
    )
    def test_forbidden_ranges_are_named(self, address: str, expected_reason: str) -> None:
        reason = UrlPolicy.classify_address(ip_address(address))

        assert reason is not None
        assert expected_reason in reason

    def test_public_address_is_allowed(self) -> None:
        assert UrlPolicy.classify_address(ip_address("93.184.216.34")) is None

    def test_check_address_raises_for_a_blocked_answer(self, policy: UrlPolicy) -> None:
        url = policy.validate("https://example.com/a")

        with pytest.raises(BlockedAddressError) as excinfo:
            policy.check_address(url, "169.254.169.254")

        assert excinfo.value.address == "169.254.169.254"

    def test_check_address_rejects_garbage(self, policy: UrlPolicy) -> None:
        url = policy.validate("https://example.com/a")

        with pytest.raises(BlockedAddressError):
            policy.check_address(url, "not-an-address")

    def test_check_address_passes_for_a_public_answer(self, policy: UrlPolicy) -> None:
        url = policy.validate("https://example.com/a")

        policy.check_address(url, "93.184.216.34")


class TestPolicyConfiguration:
    def test_private_networks_can_be_allowed_deliberately(self) -> None:
        permissive = UrlPolicy(block_private_networks=False)

        validated = permissive.validate("http://192.168.1.10:8080/a")
        permissive.check_address(validated, "192.168.1.10")

        assert validated.host == "192.168.1.10"

    def test_scheme_allow_list_is_configurable(self) -> None:
        strict = UrlPolicy(allowed_schemes=frozenset({"https"}))

        with pytest.raises(UnsupportedSchemeError):
            strict.validate("http://example.com/a")


class TestValidatedUrl:
    def test_rehydrates_a_canonical_url(self) -> None:
        url = ValidatedUrl.of("https://example.com:8443/a?b=1")

        assert url.host == "example.com"
        assert url.port == 8443
        assert str(url) == "https://example.com:8443/a?b=1"

    def test_refuses_a_non_absolute_url(self) -> None:
        with pytest.raises(InvalidUrlError):
            ValidatedUrl.of("/relative/path")
