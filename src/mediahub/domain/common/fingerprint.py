"""Content fingerprints.

A fingerprint identifies *bytes*, independently of where they came from or
where they went. Two contexts need it and must agree on what it means: the
Catalogue uses it for duplicate detection, and Delivery records it on a receipt
as proof of what was actually transferred. Duplicating the type would let the
two disagree about what ``sha256`` is, which is exactly the failure the shared
kernel exists to prevent (``docs/architecture/06-domain-model.md`` §6.2).

The digest is validated for *shape* only - length and alphabet. Computing
hashes is an infrastructure concern; the domain's job is to make an invalid
fingerprint unrepresentable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

from mediahub.domain.common.errors import InvariantViolationError
from mediahub.domain.common.value_object import ValueObject

_HEX_DIGITS: Final[frozenset[str]] = frozenset("0123456789abcdef")


class InvalidFingerprintError(InvariantViolationError):
    """The digest does not match the shape required by its algorithm."""

    code: ClassVar[str] = "invalid_fingerprint"


class HashAlgorithm(StrEnum):
    """Hash algorithms accepted for integrity verification.

    Only algorithms with a fixed, known digest length are listed, which lets a
    fingerprint validate a digest without computing it.
    """

    SHA256 = "sha256"
    SHA512 = "sha512"
    BLAKE2B = "blake2b"

    @property
    def digest_length(self) -> int:
        """Return the expected number of hexadecimal characters."""
        return _DIGEST_LENGTHS[self]


_DIGEST_LENGTHS: Final[dict[HashAlgorithm, int]] = {
    HashAlgorithm.SHA256: 64,
    HashAlgorithm.SHA512: 128,
    HashAlgorithm.BLAKE2B: 128,
}


@dataclass(frozen=True, slots=True)
class Fingerprint(ValueObject):
    """A content hash, as ``algorithm`` plus a lower-case hexadecimal digest.

    Attributes:
        algorithm: Which hash was computed.
        digest: The digest, normalised to lower case.
    """

    algorithm: HashAlgorithm
    digest: str

    def __post_init__(self) -> None:
        """Normalise the digest to lowercase hex and validate its shape."""
        digest = (self.digest or "").strip().lower()
        if not digest:
            message = "A fingerprint digest must not be empty."
            raise InvalidFingerprintError(message)
        if set(digest) - _HEX_DIGITS:
            message = "A fingerprint digest must be hexadecimal."
            raise InvalidFingerprintError(message)
        expected = self.algorithm.digest_length
        if len(digest) != expected:
            message = (
                f"A {self.algorithm.value} digest must be {expected} characters, "
                f"got {len(digest)}."
            )
            raise InvalidFingerprintError(message)
        object.__setattr__(self, "digest", digest)

    @classmethod
    def parse(cls, value: str) -> Fingerprint:
        """Build a fingerprint from its ``algorithm:digest`` text form.

        Raises:
            InvalidFingerprintError: If the text is malformed or names an
                algorithm this system does not accept.
        """
        algorithm, separator, digest = (value or "").partition(":")
        if not separator:
            message = f"'{value}' is not in 'algorithm:digest' form."
            raise InvalidFingerprintError(message)
        try:
            parsed = HashAlgorithm(algorithm.strip().lower())
        except ValueError as exc:
            message = f"'{algorithm}' is not a supported hash algorithm."
            raise InvalidFingerprintError(message) from exc
        return cls(algorithm=parsed, digest=digest)

    def __str__(self) -> str:
        """Return the ``algorithm:digest`` form."""
        return f"{self.algorithm.value}:{self.digest}"
