"""A file wrapper that counts, hashes and reports as bytes are read.

Providers hand a file object to a client library and lose sight of it until the
upload finishes. Wrapping it here recovers three things in a single pass, with
no extra I/O:

* **progress**, because the library reads in chunks and each read is an event;
* **the checksum** that goes on the receipt, hashed as the bytes go past
  rather than by reading the file a second time;
* **the byte count**, which is what actually left the machine.

A second pass over a 2 GB file on an SD card costs about a minute of pure I/O
for information that was already in hand, which is why this exists rather than
a ``hash_file()`` helper.

**The buffering ceiling.** A client library that asks for the whole stream at
once - ``read()`` with no argument, which is what ``python-telegram-bot`` does -
materialises the entire file in memory. On a device with a gigabyte of RAM that
is not a slow upload, it is an OOM kill: the process dies, the lease is orphaned
and the job is only recovered when its lease lapses. So the wrapper carries a
ceiling. Crossing it raises :class:`ArtifactTooLargeError`, which the pipeline
already understands as a policy refusal, and the job fails in one line instead
of taking the worker with it.
"""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING, Final

from mediahub.application.delivery.errors import ArtifactTooLargeError
from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

CHUNK_BYTES: Final[int] = 1024 * 1024
"""Read granularity. Small enough for responsive progress, large enough that
the per-chunk overhead is irrelevant."""


class MeasuredReader(io.RawIOBase):
    """Wraps a readable binary stream, measuring everything that passes.

    Presents as a normal file object, so a client library needs no cooperation.
    A library that asks for the whole file at once still gets chunked hashing
    and progress, because :meth:`read` loops internally rather than delegating
    one enormous read.
    """

    def __init__(
        self,
        stream: io.BufferedIOBase,
        *,
        total_bytes: int | None = None,
        algorithm: HashAlgorithm = HashAlgorithm.SHA256,
        on_chunk: Callable[[int, int | None], None] | None = None,
        chunk_bytes: int = CHUNK_BYTES,
        max_buffer_bytes: int | None = None,
        provider: str | None = None,
    ) -> None:
        """Wrap ``stream``.

        Args:
            stream: The underlying readable binary stream.
            total_bytes: Expected size, used for progress percentages.
            algorithm: Which digest to compute.
            on_chunk: Called with ``(bytes_read_so_far, total_bytes)`` after
                each chunk. Must not raise: it is describing the transfer, not
                performing it.
            chunk_bytes: Read granularity.
            max_buffer_bytes: Most that one whole-stream ``read()`` may
                accumulate in memory. ``None`` means no ceiling, which is right
                for a caller that reads in chunks of its own choosing.
            provider: Named on the refusal, so a log line says which
                destination's upload was abandoned.
        """
        super().__init__()
        self._stream = stream
        self._total = total_bytes
        self._algorithm = algorithm
        self._digest = hashlib.new(algorithm.value)
        self._on_chunk = on_chunk
        self._chunk = max(1, chunk_bytes)
        self._read = 0
        self._max_buffer = max_buffer_bytes
        self._provider = provider

    @property
    def bytes_read(self) -> int:
        """Return how much has passed through so far."""
        return self._read

    def fingerprint(self) -> Fingerprint:
        """Return the digest of everything read so far."""
        return Fingerprint(algorithm=self._algorithm, digest=self._digest.hexdigest())

    def readable(self) -> bool:
        """Return ``True``; this wrapper is read-only."""
        return True

    def read(self, size: int | None = -1) -> bytes:
        """Read up to ``size`` bytes, measuring as they go.

        A request for everything (``-1``) is served by looping over chunks, so
        the digest and the progress callback still advance for a library that
        buffers the whole file.

        Raises:
            ArtifactTooLargeError: If a whole-stream read would accumulate more
                than the configured ceiling. Refusing is the point: the
                alternative is the kernel refusing, and it does that by killing
                the process.
        """
        if size is not None and size >= 0:
            return self._read_chunk(size)

        buffer = bytearray()
        while True:
            chunk = self._read_chunk(self._chunk)
            if not chunk:
                break
            buffer += chunk
            self._guard_buffer(len(buffer))
        return bytes(buffer)

    def readall(self) -> bytes:
        """Read the remainder of the stream."""
        return self.read(-1)

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        """Read into a caller-supplied buffer."""
        data = self._read_chunk(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        """Close the wrapper, leaving the underlying stream to its owner."""
        super().close()

    def _guard_buffer(self, buffered_bytes: int) -> None:
        """Refuse to keep accumulating past the ceiling.

        Raises:
            ArtifactTooLargeError: If the ceiling is set and has been passed.
        """
        if self._max_buffer is not None and buffered_bytes > self._max_buffer:
            raise ArtifactTooLargeError(self._max_buffer, buffered_bytes, provider=self._provider)

    def _read_chunk(self, size: int) -> bytes:
        """Read one chunk, updating the digest, the count and the callback."""
        data = self._stream.read(size)
        if not data:
            return b""
        self._digest.update(data)
        self._read += len(data)
        if self._on_chunk is not None:
            self._on_chunk(self._read, self._total)
        return data
