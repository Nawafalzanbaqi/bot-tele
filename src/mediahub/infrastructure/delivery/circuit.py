"""A small circuit breaker for provider selection.

It lives in the registry, not in a provider, and that placement is the whole
point: a provider must never decide whether to try again, but the component
*choosing between providers* is entitled to stop picking one that has failed
repeatedly.

Deliberately minimal. It trips on consecutive transient failures, recovers
after a cooldown, and never prevents an attempt when there is no alternative -
failing with the destination's real error is far more useful than failing with
"nothing is available".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

DEFAULT_FAILURE_THRESHOLD: Final[int] = 3
DEFAULT_COOLDOWN_SECONDS: Final[float] = 60.0


class CircuitState(StrEnum):
    """Whether a provider is currently worth choosing.

    Attributes:
        CLOSED: Healthy; select it normally.
        OPEN: Failing; prefer an alternative if one exists.
        HALF_OPEN: The cooldown elapsed; allow one attempt to prove itself.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class CircuitBreaker:
    """Tracks one provider's recent reliability.

    Attributes:
        failure_threshold: Consecutive transient failures that trip it.
        cooldown_seconds: How long to prefer alternatives before trying again.
    """

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    _failures: int = 0
    _opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        """Return the current state, accounting for an elapsed cooldown."""
        if self._opened_at is None:
            return CircuitState.CLOSED
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    @property
    def is_healthy(self) -> bool:
        """Return whether this provider should be preferred over alternatives."""
        return self.state is not CircuitState.OPEN

    @property
    def consecutive_failures(self) -> int:
        """Return how many transient failures have happened in a row."""
        return self._failures

    def record_success(self) -> None:
        """Reset after a working delivery."""
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """Count a transient failure, tripping the circuit at the threshold.

        Only *transient* failures reach here. A file that is too large or a
        conversation that no longer exists says nothing about the provider's
        health, and counting it would take a working destination out of service
        because a user made a bad request.
        """
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Force the circuit closed. Operator action and tests only."""
        self.record_success()
