"""Unversioned operational routes.

Health and readiness probes live outside ``/api/v1`` on purpose: orchestrators,
load balancers and uptime monitors hard-code these paths, so they must never
move with an API version bump.
"""

from __future__ import annotations
