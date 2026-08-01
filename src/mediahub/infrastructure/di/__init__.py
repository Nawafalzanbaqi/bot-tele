"""The composition root.

Exactly one place in the codebase knows which concrete adapter satisfies which
port: :mod:`~mediahub.infrastructure.di.container`. Everything else receives
its dependencies through ``__init__`` and never imports an adapter.

That single rule is what makes the architecture testable - a test builds a
container with in-memory adapters and exercises the real use cases - and what
keeps swapping an implementation a one-file change.
"""

from __future__ import annotations
