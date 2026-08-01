"""Helpers shared by delivery provider adapters.

Deliberately small. Anything used by exactly one provider belongs in that
provider's package; anything that grows a second implementation belongs behind
a port.
"""

from __future__ import annotations
