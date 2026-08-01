"""Version 1 of the public HTTP API.

Everything a client can depend on lives here: request schemas, response
schemas and routes, all mounted under ``/api/v1``.

**The contract is frozen.** Once ``v1`` ships, its schemas are additive-only -
new optional fields are fine, renaming or removing a field is not. A breaking
change means a ``v2`` package beside this one, with both served until clients
migrate. Copying a schema into ``v2`` is cheap; breaking a running client is
not.
"""

from __future__ import annotations
