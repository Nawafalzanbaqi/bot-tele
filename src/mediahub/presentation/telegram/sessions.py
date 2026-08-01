"""Short-lived state between a question posted and an answer tapped.

The gateway posts "what quality?" and gets back a button press some seconds -
or hours - later. Something has to remember what the question was about, and it
cannot be the callback payload, which holds 64 bytes of attacker-returnable
text.

The store is deliberately small and forgetful:

* entries expire, so a button tapped tomorrow fails cleanly rather than
  acquiring a URL from yesterday;
* the store is bounded, so a user who pastes a thousand links cannot exhaust
  memory;
* every entry records its owner, so one person cannot drive another's session
  by guessing a token.

It holds no business state. Losing the whole store costs users a re-paste.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.common.cancellation import CancellationSource
    from mediahub.application.download.dto import SourceSummary

TOKEN_BYTES: Final[int] = 6
DEFAULT_TTL_SECONDS: Final[float] = 900.0
DEFAULT_CAPACITY: Final[int] = 200


@dataclass(slots=True)
class Session:
    """What the gateway remembers about one pending choice.

    Attributes:
        token: Short identifier carried by the buttons.
        owner: Principal identity that created it. Checked on every callback.
        chat_id: Where the conversation is happening.
        summary: What the application said about the source.
        prompt_message_id: The message holding the buttons, so it can be edited.
        created_at: Monotonic creation time, for expiry.
        cancellation: Set once acquisition starts, so "Stop" can reach it.
    """

    token: str
    owner: str
    chat_id: str
    summary: SourceSummary
    prompt_message_id: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    cancellation: CancellationSource | None = None


class SessionStore:
    """A bounded, expiring map of tokens to pending choices."""

    __slots__ = ("_capacity", "_sessions", "_ttl")

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        """Create an empty store."""
        self._ttl = ttl_seconds
        self._capacity = max(1, capacity)
        self._sessions: dict[str, Session] = {}

    def create(self, *, owner: str, chat_id: str, summary: SourceSummary) -> Session:
        """Open a session and return it, evicting expired ones first."""
        self._evict()
        token = secrets.token_hex(TOKEN_BYTES)
        session = Session(token=token, owner=owner, chat_id=chat_id, summary=summary)
        self._sessions[token] = session
        return session

    def get(self, token: str, *, owner: str) -> Session | None:
        """Return a live session belonging to ``owner``, or ``None``.

        Returns ``None`` for an expired session, an unknown token, and a token
        belonging to someone else - all three are indistinguishable to the
        caller on purpose.
        """
        session = self._sessions.get(token)
        if session is None:
            return None
        if self._is_expired(session):
            self._sessions.pop(token, None)
            return None
        if session.owner != owner:
            return None
        return session

    def discard(self, token: str) -> None:
        """Forget a session. Unknown tokens are ignored."""
        self._sessions.pop(token, None)

    def __len__(self) -> int:
        """Return how many sessions are held, expired ones included."""
        return len(self._sessions)

    def _is_expired(self, session: Session) -> bool:
        """Return whether a session has outlived its time to live."""
        return time.monotonic() - session.created_at > self._ttl

    def _evict(self) -> None:
        """Drop expired sessions, then the oldest if still over capacity."""
        expired = [token for token, session in self._sessions.items() if self._is_expired(session)]
        for token in expired:
            self._sessions.pop(token, None)

        overflow = len(self._sessions) - self._capacity + 1
        if overflow <= 0:
            return
        oldest = sorted(self._sessions.items(), key=lambda item: item[1].created_at)
        for token, _ in oldest[:overflow]:
            self._sessions.pop(token, None)
