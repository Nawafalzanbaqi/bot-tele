"""Access - who may ask MediaHub to do what.

Phase 04 implements the part the Telegram gateway needs: an allow-list of
external identities, the role each one carries, and which actions a role
permits.

The rules live here rather than in the gateway for the usual reason: an
authorisation check that exists in only one interface is not a check. When the
REST API and the CLI arrive they consult the same policy, and a principal
denied in Telegram is denied everywhere.
"""

from __future__ import annotations
