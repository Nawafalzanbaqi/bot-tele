"""One module per workspace use case.

There is exactly one so far, and it is the one that cannot be left to the happy
path: recovering the workspace after a restart. Reserving and releasing space is
the lease context manager's job - a use case wrapping a ``with`` block would add
a layer and no decision.
"""

from __future__ import annotations
