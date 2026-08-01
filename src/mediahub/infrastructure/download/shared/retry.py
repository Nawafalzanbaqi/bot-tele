"""Retry for cheap, idempotent engine operations.

**Only probing is retried here.** Fetching is not, and that is a deliberate
architectural boundary rather than an omission:

* A probe is seconds long, side-effect free and frequently fails for transient
  reasons. Retrying it inside the engine hides a common annoyance from every
  caller.
* A fetch is minutes to hours long and consumes bandwidth, disk and a worker
  slot. Retrying it *inside* the engine would silently multiply the job-level
  retry budget - three engine attempts inside three job attempts is nine
  downloads, and nobody configured that. Fetch retries belong to the queue
  (``docs/architecture/09-queue-architecture.md`` §9.5), which can apply
  backoff, priority and a visible attempt count.

Within a single fetch, the engine's own fragment-level retries handle brief
network hiccups; those never restart the transfer from zero.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.download.errors import DownloadError, DownloadFailedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

_JITTER_RESOLUTION = 1000


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    """How many times to try, and how long to wait between attempts.

    Attributes:
        attempts: Total attempts, including the first. ``1`` disables retrying.
        base_delay_seconds: Delay before the second attempt.
        max_delay_seconds: Ceiling for exponential growth.
        jitter_ratio: Fraction of the delay randomised, to stop several
            operations failing against one outage and then retrying in lockstep.
    """

    attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def delay_for(self, attempt: int) -> float:
        """Return the delay in seconds before ``attempt`` (1-based)."""
        if attempt <= 1:
            return 0.0
        exponential = self.base_delay_seconds * float(2 ** (attempt - 2))
        capped = min(exponential, self.max_delay_seconds)
        spread = capped * self.jitter_ratio
        # A uniform offset in [-1.0, 1.0].
        offset = (
            secrets.randbelow(_JITTER_RESOLUTION * 2 + 1) - _JITTER_RESOLUTION
        ) / _JITTER_RESOLUTION
        return max(0.0, capped + spread * offset)


async def retry_async[TResult](
    operation: Callable[[], Awaitable[TResult]],
    *,
    schedule: RetrySchedule,
    description: str,
) -> TResult:
    """Run ``operation``, retrying only failures classified as transient.

    A provider that supplied ``retry_after_seconds`` is obeyed exactly - guessing
    at backoff when the other side has stated the answer is self-inflicted
    damage.

    Args:
        operation: The awaitable to run. Must be idempotent.
        schedule: How many attempts and how long to wait.
        description: Short label used in log lines.

    Returns:
        Whatever ``operation`` returns.

    Raises:
        DownloadError: The last failure, once attempts are exhausted or the
            failure is not retryable.
    """
    last_error: DownloadError | None = None

    for attempt in range(1, max(1, schedule.attempts) + 1):
        try:
            return await operation()
        except DownloadError as exc:
            last_error = exc
            if not exc.is_retryable or attempt >= schedule.attempts:
                raise
            delay = exc.retry_after_seconds or schedule.delay_for(attempt + 1)
            logger.bind(
                operation=description,
                attempt=attempt,
                max_attempts=schedule.attempts,
                delay_seconds=round(delay, 2),
                error_code=exc.code,
            ).warning("Transient failure, retrying: {}", exc.message)
            await asyncio.sleep(delay)

    if last_error is not None:  # pragma: no cover - unreachable via the loop above
        raise last_error
    message = f"{description} exhausted its retry schedule without reporting an error"
    raise DownloadFailedError(message)  # pragma: no cover - defensive
