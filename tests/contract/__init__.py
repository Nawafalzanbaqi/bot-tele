"""Shared contract suites: one per port, run against every implementation.

The suites here are what keep several implementations of one port honest
without several sets of bespoke tests. A new provider is integrated by adding
it to a parametrised fixture, not by writing a new test file.
"""

from __future__ import annotations
