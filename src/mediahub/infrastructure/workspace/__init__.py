"""Filesystem implementation of the workspace port.

One directory per lease under a configured root, deleted when the lease closes.
The root is wiped at startup: anything present there belongs to a process that
no longer exists.
"""

from __future__ import annotations
