"""Helpers shared by every download engine adapter.

Deliberately small: anything that grows a second implementation belongs behind a
port, and anything used by exactly one engine belongs in that engine's package.
"""

from __future__ import annotations
