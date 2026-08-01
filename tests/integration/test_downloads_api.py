"""``/api/v1/downloads`` end to end.

These tests pin the honest contract of an unfinished feature: jobs are
accepted, stored, listed and cancelled, and nothing claims to have transferred
anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def register_media(client: AsyncClient, index: int = 0) -> str:
    """Register an item and return its identifier."""
    response = await client.post(
        "/api/v1/media",
        json={
            "source_url": f"https://example.com/{index}.mp4",
            "title": f"Item {index}",
            "media_type": "video",
        },
    )
    assert response.status_code == 201, response.text
    media_id: str = response.json()["id"]
    return media_id


class TestRequestDownload:
    async def test_accepts_and_queues(self, client: AsyncClient) -> None:
        media_id = await register_media(client)

        response = await client.post("/api/v1/downloads", json={"media_id": media_id})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "queued"
        assert body["attempts"] == 0
        assert body["downloaded_bytes"] == 0
        assert body["percentage"] is None
        assert body["started_at"] is None

    async def test_unknown_media_is_not_found(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/downloads", json={"media_id": str(uuid4())})

        assert response.status_code == 404
        assert response.json()["code"] == "media_not_found"

    async def test_second_active_job_is_a_conflict(self, client: AsyncClient) -> None:
        media_id = await register_media(client)
        await client.post("/api/v1/downloads", json={"media_id": media_id})

        response = await client.post("/api/v1/downloads", json={"media_id": media_id})

        assert response.status_code == 409
        assert response.json()["code"] == "duplicate_active_job"

    async def test_out_of_range_retry_budget_is_rejected(self, client: AsyncClient) -> None:
        media_id = await register_media(client)

        response = await client.post(
            "/api/v1/downloads", json={"media_id": media_id, "max_attempts": 99}
        )

        assert response.status_code == 422


class TestReadAndCancel:
    async def test_get_returns_the_job(self, client: AsyncClient) -> None:
        media_id = await register_media(client)
        created = (await client.post("/api/v1/downloads", json={"media_id": media_id})).json()

        response = await client.get(f"/api/v1/downloads/{created['id']}")

        assert response.status_code == 200
        assert response.json()["media_id"] == media_id

    async def test_unknown_job_is_not_found(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/downloads/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["code"] == "download_job_not_found"

    async def test_list_filters_by_priority(self, client: AsyncClient) -> None:
        media_id = await register_media(client)
        await client.post("/api/v1/downloads", json={"media_id": media_id, "priority": "high"})

        high = (await client.get("/api/v1/downloads", params={"priority": "high"})).json()
        low = (await client.get("/api/v1/downloads", params={"priority": "low"})).json()

        assert high["total"] == 1
        assert low["total"] == 0

    async def test_cancel_is_idempotent_only_once(self, client: AsyncClient) -> None:
        media_id = await register_media(client)
        created = (await client.post("/api/v1/downloads", json={"media_id": media_id})).json()

        first = await client.post(f"/api/v1/downloads/{created['id']}/cancel")
        second = await client.post(f"/api/v1/downloads/{created['id']}/cancel")

        assert first.status_code == 200
        assert first.json()["status"] == "cancelled"
        assert second.status_code == 409
        assert second.json()["code"] == "invalid_job_transition"
