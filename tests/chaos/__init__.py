"""Tests that break something mid-flight and assert the system comes back.

These are the tests that justify the lease and checkpoint machinery. Without
them, crash recovery is a claim in a document
(``docs/architecture/17-testing-strategy.md`` §17.6).
"""

from __future__ import annotations
