"""The shape every use case follows.

One class, one intention, one public method::

    class RegisterMedia:
        async def execute(self, request: RegisterMediaCommand) -> MediaSummary: ...

Uniformity is the point: cross-cutting behaviour (timing, tracing, retries) can
be added by wrapping ``execute`` rather than by editing every handler, and a
newcomer can read any use case without learning a new convention first.

Conventions:

* Requests are **commands** (they change state) or **queries** (they do not).
  Both are frozen dataclasses declared next to their use case.
* Responses are DTOs, never domain entities: nothing outside this layer should
  be able to invoke domain behaviour by accident.
* Dependencies arrive through ``__init__``, never through module-level state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Command:
    """Marker base for requests that change state."""


@dataclass(frozen=True, slots=True)
class Query:
    """Marker base for requests that only read state."""


class UseCase[TRequest, TResponse](Protocol):
    """A single application operation.

    Type parameters:
        TRequest: The command or query this use case accepts.
        TResponse: The DTO it returns.
    """

    async def execute(self, request: TRequest) -> TResponse:
        """Run the operation and return its result."""
        ...
