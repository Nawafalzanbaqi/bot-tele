"""Health and readiness endpoints behave the way an orchestrator expects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_liveness_needs_no_dependency(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_reports_the_backend(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": True}


async def test_info_exposes_version_and_environment(client: AsyncClient) -> None:
    body = (await client.get("/health")).json()

    assert body["name"] == "mediahub"
    assert body["environment"] == "testing"
    assert body["persistence_backend"] == "memory"


async def test_correlation_id_is_echoed(client: AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "trace-123"})

    assert response.headers["X-Request-ID"] == "trace-123"


async def test_correlation_id_is_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.headers["X-Request-ID"]


async def test_openapi_document_is_served(client: AsyncClient) -> None:
    document = (await client.get("/openapi.json")).json()

    assert document["info"]["title"] == "MediaHub API"
    assert "/api/v1/media" in document["paths"]
    assert "/api/v1/downloads" in document["paths"]
