"""In-memory progress, coalesced and throttled.

A download engine reports per chunk. A durable write per chunk would produce
thousands of writes for one file, and on a device whose database lives on an SD
card that is not slow - it is *fatal*, because flash wears out
(``docs/architecture/05-component-communication.md`` §5.9).

So progress is held here, in memory, and only some of it is written:

* **Coalescing.** Only the newest observation matters. An update that arrives
  before the previous one was written simply replaces it.
* **Throttling.** A write is due when enough time has passed, when the
  percentage has moved enough, or when the stage changed. Time alone is not
  enough: a fast download would report twice and look stuck.
* **Silence when nothing moved.** An observation that reports no more bytes than
  the one already written carries no information, and a stalled transfer would
  otherwise produce one durable write every interval, for hours, describing the
  same number. That is the exact shape of wear this module exists to prevent, so
  a write also has to have something to say.
* **Always the last one.** :meth:`ProgressRegistry.take_final` ignores the
  throttle, so the observation a stage ends on is never the one that is dropped.

A backward wall-clock step - routine on a device with no battery-backed clock -
makes the elapsed measurement negative. That is treated as "write it", which
re-anchors the throttle immediately instead of suppressing every update until
the clock has caught back up.

Thread-safe, because engine work often runs in a worker thread while the loop
that flushes lives on the event loop.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from mediahub.application.common.ports import Clock
    from mediahub.application.download.queue import StageProgress

DEFAULT_INTERVAL_SECONDS: Final[float] = 5.0
DEFAULT_PERCENT_STEP: Final[float] = 5.0


class ProgressRegistry:
    """Holds the latest observation of one job and decides when to write it."""

    __slots__ = (
        "_clock",
        "_lock",
        "_min_interval",
        "_min_step",
        "_pending",
        "_written_at",
        "_written_bytes",
        "_written_percentage",
        "_written_stage",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        min_interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        min_percent_step: float = DEFAULT_PERCENT_STEP,
    ) -> None:
        """Bind the registry to a clock and the throttle it should apply."""
        self._clock = clock
        self._min_interval = min_interval_seconds
        self._min_step = min_percent_step
        self._lock = threading.Lock()
        self._pending: StageProgress | None = None
        self._written_at: datetime | None = None
        self._written_percentage: float | None = None
        self._written_stage: object | None = None
        self._written_bytes: int | None = None

    def observe(self, progress: StageProgress) -> None:
        """Record an observation, replacing any that has not been written.

        Cheap, non-blocking and never raises: it is called from whatever thread
        the engine happens to be on, and an exception there would abort the
        transfer it is merely describing.
        """
        with self._lock:
            self._pending = progress

    @property
    def pending(self) -> StageProgress | None:
        """Return the observation waiting to be written, if any."""
        with self._lock:
            return self._pending

    def take_due(self) -> StageProgress | None:
        """Return the pending observation if it is time to write it.

        Returns:
            The observation to write, or ``None`` when nothing is pending or the
            throttle says not yet. Taking it marks it written.
        """
        with self._lock:
            progress = self._pending
            if progress is None or not self._is_due(progress):
                return None
            return self._take(progress)

    def take_final(self) -> StageProgress | None:
        """Return the pending observation regardless of the throttle.

        Called when a stage or a job ends: the last thing observed is the most
        useful thing to have recorded, and dropping it is how a finished job
        ends up displaying 87%.
        """
        with self._lock:
            progress = self._pending
            if progress is None:
                return None
            return self._take(progress)

    def _is_due(self, progress: StageProgress) -> bool:
        """Return whether ``progress`` has earned a durable write."""
        if self._written_at is None or progress.stage != self._written_stage:
            return True
        if not self._has_moved(progress):
            # A stalled transfer. The lease is still being renewed by the
            # heartbeat, so liveness is unaffected; what is withheld is a write
            # that would say exactly what the last one said.
            return False
        elapsed = (self._clock.now() - self._written_at).total_seconds()
        if elapsed >= self._min_interval or elapsed < 0:
            return True
        percentage = progress.percentage
        if percentage is None or self._written_percentage is None:
            return False
        return abs(percentage - self._written_percentage) >= self._min_step

    def _has_moved(self, progress: StageProgress) -> bool:
        """Return whether ``progress`` reports more than what was last written.

        Byte count rather than percentage: a source that declares no total has
        no percentage at all, and that is exactly the case where a stall would
        otherwise be written out on the interval forever.
        """
        if self._written_bytes is None:
            return True
        return progress.transferred_bytes > self._written_bytes

    def _take(self, progress: StageProgress) -> StageProgress:
        """Clear the pending observation and remember what was written."""
        self._pending = None
        self._written_at = self._clock.now()
        self._written_percentage = progress.percentage
        self._written_stage = progress.stage
        self._written_bytes = progress.transferred_bytes
        return progress
