"""Application contracts for identity and authorisation.

One use case - ``AuthorizePrincipal`` - and two ports. Every interface calls the
same use case, so a principal refused in Telegram is refused in the API, and
every refusal is audited in one place.
"""

from __future__ import annotations
