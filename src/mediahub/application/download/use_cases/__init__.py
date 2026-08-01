"""One module per download use case.

Two groups live here. The first manages job *state* as an interface sees it:
requesting, reading, listing, cancelling. The second is what a worker drives -
claim, heartbeat, checkpoint, progress, and the four ways an attempt can end
(complete, fail, release, acknowledge a cancellation) - plus the sweep that
takes back leases from a worker that stopped reporting.

The split matters: the worker owns *execution*, and every rule it appears to
apply - whether to retry, how long to wait, when an attempt is spent - is
decided here, on top of the domain. A worker with no business logic is one that
can be rewritten without re-deciding anything.
"""

from __future__ import annotations
