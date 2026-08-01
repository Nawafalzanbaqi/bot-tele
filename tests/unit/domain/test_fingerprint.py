"""Content fingerprints validate their own shape.

Lives in the shared kernel because two contexts depend on it meaning the same
thing: the Catalogue identifies duplicates by it, and a delivery receipt
records it as proof of what was transferred.
"""

from __future__ import annotations

import pytest

from mediahub.domain.common.fingerprint import (
    Fingerprint,
    HashAlgorithm,
    InvalidFingerprintError,
)

pytestmark = pytest.mark.unit


class TestConstruction:
    def test_normalises_digest_case(self) -> None:
        fingerprint = Fingerprint(algorithm=HashAlgorithm.SHA256, digest="A" * 64)

        assert fingerprint.digest == "a" * 64
        assert str(fingerprint) == f"sha256:{'a' * 64}"

    @pytest.mark.parametrize("digest", ["", "   ", "z" * 64, "a" * 63, "a" * 65])
    def test_rejects_a_malformed_digest(self, digest: str) -> None:
        with pytest.raises(InvalidFingerprintError):
            Fingerprint(algorithm=HashAlgorithm.SHA256, digest=digest)

    @pytest.mark.parametrize(
        ("algorithm", "length"),
        [(HashAlgorithm.SHA256, 64), (HashAlgorithm.SHA512, 128), (HashAlgorithm.BLAKE2B, 128)],
    )
    def test_each_algorithm_declares_its_digest_length(
        self, algorithm: HashAlgorithm, length: int
    ) -> None:
        assert algorithm.digest_length == length
        assert Fingerprint(algorithm=algorithm, digest="a" * length).digest == "a" * length

    def test_equality_is_by_value(self) -> None:
        first = Fingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64)
        second = Fingerprint(algorithm=HashAlgorithm.SHA256, digest="A" * 64)

        assert first == second


class TestParsing:
    def test_round_trips_the_text_form(self) -> None:
        original = Fingerprint(algorithm=HashAlgorithm.SHA256, digest="b" * 64)

        assert Fingerprint.parse(str(original)) == original

    @pytest.mark.parametrize(
        "value",
        ["", "nocolon", "md5:" + "a" * 32, "sha256:", "sha256:zz", ":" + "a" * 64],
    )
    def test_rejects_malformed_text(self, value: str) -> None:
        with pytest.raises(InvalidFingerprintError):
            Fingerprint.parse(value)
