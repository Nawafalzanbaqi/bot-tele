"""Aggregates every version 1 route under a single prefix.

Adding a resource means writing its router module and including it here - one
line, in one place, so the shape of the public API is readable at a glance.
"""

from __future__ import annotations

from fastapi import APIRouter

from mediahub.presentation.api.v1.routers import downloads, media

API_V1_PREFIX = "/api/v1"

api_v1_router = APIRouter(prefix=API_V1_PREFIX)
api_v1_router.include_router(media.router)
api_v1_router.include_router(downloads.router)
