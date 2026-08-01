"""Liveness and readiness probes.

The distinction is what makes an orchestrator behave sanely:

* **Liveness** (``/health/live``) - "is this process running?" It touches no
  dependency, so a database blip never causes a restart loop.
* **Readiness** (``/health/ready``) - "should traffic be routed here?" It pings
  the persistence backend and answers ``503`` when it cannot, so the instance
  is pulled from the pool instead of failing requests.

``/health`` returns build and environment information for humans.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict

from mediahub import __version__
from mediahub.presentation.api.dependencies import ContainerDep, SettingsDep

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    """Answer of the liveness probe.

    Attributes:
        status: Always ``alive`` when the process can serve requests.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    """Answer of the readiness probe.

    Attributes:
        status: ``ready`` when every dependency answered, else ``degraded``.
        database: Whether the persistence backend responded.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "degraded"]
    database: bool


class HealthInfoResponse(BaseModel):
    """Human-facing build and environment information.

    Attributes:
        name: Application name.
        version: Package version.
        environment: Which deployment this process represents.
        persistence_backend: Which repository adapters are wired up.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    environment: str
    persistence_backend: str


@router.get("", summary="Service information")
async def health_info(settings: SettingsDep) -> HealthInfoResponse:
    """Return build and environment information."""
    return HealthInfoResponse(
        name="mediahub",
        version=__version__,
        environment=settings.environment.value,
        persistence_backend=settings.database.backend.value,
    )


@router.get("/live", summary="Liveness probe")
async def liveness() -> LivenessResponse:
    """Return ``alive`` if the process can serve requests.

    Deliberately dependency-free: a failing database must not make the
    orchestrator kill an otherwise healthy process.
    """
    return LivenessResponse()


@router.get("/ready", summary="Readiness probe")
async def readiness(container: ContainerDep, response: Response) -> ReadinessResponse:
    """Report whether this instance should receive traffic.

    Two dependencies are checked, and both have to hold. The persistence backend
    is asked for a round trip. The workspace root is asked whether it is still a
    writable directory - an unmounted volume or a filesystem the kernel
    remounted read-only after an I/O error leaves an instance that answers every
    request and completes none of them, which is precisely what readiness exists
    to catch. A workspace that is merely *full* stays ready: the queue is the
    right place for that work to wait.

    Responses:
        200: Every dependency answered.
        503: A dependency is unavailable; stop routing traffic here.
    """
    database_ready = await container.check_database()
    workspace_ready = container.check_workspace()
    if not (database_ready and workspace_ready):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="degraded", database=database_ready)
    return ReadinessResponse(status="ready", database=True)
