"""Adapters for ambient system services.

Small on purpose: these wrap the two global functions that would otherwise make
the whole system untestable - reading the clock and generating identifiers.
Routing them through ports means a test can freeze time and fix the id
sequence without patching anything.
"""

from __future__ import annotations
