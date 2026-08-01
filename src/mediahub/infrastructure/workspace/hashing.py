"""Content digests, computed once.

The rule from ``docs/architecture/11-storage-strategy.md`` §11.8: the
fingerprint is computed **while the bytes go past**, not by reading the finished
file again. Re-reading a 2 GB download from an SD card costs about a minute of
pure I/O for information that was in memory moments earlier, and it does it on
the device least able to afford it.

So there are two entry points and a clear preference between them.
:class:`StreamingDigest` is the good one - the writer feeds it. :func:`digest_of`
is the fallback for files an external tool wrote directly into the lease, and
the scope caches whatever it returns so the cost is paid at most once per file.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

READ_CHUNK_BYTES: Final[int] = 1024 * 1024
"""One mebibyte. Large enough to keep syscalls down, small enough that hashing a
file never becomes a memory decision."""

DEFAULT_ALGORITHM: Final[HashAlgorithm] = HashAlgorithm.SHA256
"""What the catalogue stores and what receipts quote."""


class StreamingDigest:
    """Accumulates a digest from chunks as they are written.

    Attributes are private and there is no way to feed it a whole file: the type
    exists to make "hash while streaming" the path of least resistance.
    """

    __slots__ = ("_algorithm", "_digest", "_length")

    def __init__(self, algorithm: HashAlgorithm = DEFAULT_ALGORITHM) -> None:
        """Start an empty digest for ``algorithm``."""
        self._algorithm = algorithm
        self._digest = hashlib.new(algorithm.value)
        self._length = 0

    @property
    def length(self) -> int:
        """Return how many bytes have been absorbed."""
        return self._length

    def update(self, chunk: bytes) -> None:
        """Absorb ``chunk``."""
        self._digest.update(chunk)
        self._length += len(chunk)

    def fingerprint(self) -> Fingerprint:
        """Return the digest of everything absorbed so far.

        Snapshotting rather than finalising, so a caller may ask mid-stream -
        for a progress line, say - and keep writing afterwards.
        """
        return Fingerprint(algorithm=self._algorithm, digest=self._digest.copy().hexdigest())


def digest_of(path: Path, algorithm: HashAlgorithm = DEFAULT_ALGORITHM) -> Fingerprint:
    """Return the digest of a file that was written by something else.

    Args:
        path: The file to read.
        algorithm: Which hash to compute.

    Returns:
        The fingerprint of the file's contents.

    Raises:
        OSError: If the file cannot be read.
    """
    accumulator = StreamingDigest(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK_BYTES):
            accumulator.update(chunk)
    return accumulator.fingerprint()
