"""Cross-cutting concerns available to every layer.

This package holds the two things that genuinely cut across a system -
configuration and logging - and nothing else. It is the one exception to the
strict layering rule, so it comes with a hard constraint: **nothing in
``shared`` may import from ``domain``, ``application``, ``infrastructure`` or
``presentation``.** Keeping it dependency-free is what makes it safe for
everyone to import, and the architecture tests enforce it.

If you are tempted to add business logic here, it belongs in a layer instead.
"""

from __future__ import annotations
