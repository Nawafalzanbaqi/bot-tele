"""A destination that accepts everything and keeps nothing.

Useful for three real purposes: a dry run against a live source without sending
anything anywhere, a working system for someone who has not configured a
destination yet, and - not least - a second implementation that keeps the
provider contract honest. A framework with one implementation is a guess.
"""

from __future__ import annotations
