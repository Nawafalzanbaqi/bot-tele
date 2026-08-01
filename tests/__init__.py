"""MediaHub test suite.

Layout mirrors the architecture:

* ``unit`` - domain rules, use cases and adapters in isolation. No I/O.
* ``integration`` - the HTTP surface end to end, over in-memory adapters.
* ``architecture`` - executable enforcement of the dependency rule.

The whole suite runs without Docker, a database or a network.
"""

from __future__ import annotations
