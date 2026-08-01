"""``/api/v1/media`` end to end, including the error contract."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.integration

VALID_PAYLOAD = {
    "source_url": "https://example.com/talk.mp4",
    "title": "A talk",
    "media_type": "video",
}


async def register(client: AsyncClient, **overrides: str) -> dict[str, object]:
    """Register an item and return the response body."""
    payload = VALID_PAYLOAD | overrides
    response = await client.post("/api/v1/media", json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


class TestRegister:
    async def test_creates_a_pending_item(self, client: AsyncClient) -> None:
        body = await register(client)

        assert body["status"] == "pending"
        assert body["source_url"] == "https://example.com/talk.mp4"
        assert body["storage_key"] is None

    async def test_duplicate_source_url_is_a_conflict(self, client: AsyncClient) -> None:
        await register(client)
        response = await client.post("/api/v1/media", json=VALID_PAYLOAD)

        assert response.status_code == 409
        problem = response.json()
        assert problem["code"] == "duplicate_media"
        assert problem["status"] == 409
        assert problem["instance"] == "/api/v1/media"
        assert problem["correlation_id"]

    async def test_unsupported_scheme_is_unprocessable(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/media", json=VALID_PAYLOAD | {"source_url": "ftp://example.com/a.mp4"}
        )

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_source_url"

    async def test_unknown_field_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/media", json=VALID_PAYLOAD | {"oops": "x"})

        assert response.status_code == 422
        assert response.json()["code"] == "request_validation_failed"


class TestRead:
    async def test_get_returns_the_item(self, client: AsyncClient) -> None:
        created = await register(client)

        response = await client.get(f"/api/v1/media/{created['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    async def test_unknown_item_is_not_found(self, client: AsyncClient) -> None:
        response = await client.get(f"/api/v1/media/{uuid4()}")

        assert response.status_code == 404
        assert response.json()["code"] == "media_not_found"

    async def test_malformed_identifier_is_unprocessable(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/media/not-a-uuid")

        assert response.status_code == 422

    async def test_list_is_paginated(self, client: AsyncClient) -> None:
        for index in range(3):
            await register(
                client,
                source_url=f"https://example.com/{index}.mp4",
                title=f"Item {index}",
            )

        body = (await client.get("/api/v1/media", params={"limit": 2})).json()

        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["has_next"] is True

    async def test_list_rejects_an_oversized_limit(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/media", params={"limit": 10_000})

        assert response.status_code == 422

    async def test_list_filters_by_status(self, client: AsyncClient) -> None:
        await register(client)

        matching = (await client.get("/api/v1/media", params={"status": "pending"})).json()
        other = (await client.get("/api/v1/media", params={"status": "archived"})).json()

        assert matching["total"] == 1
        assert other["total"] == 0


class TestArchive:
    async def test_archives_once(self, client: AsyncClient) -> None:
        created = await register(client)

        first = await client.post(f"/api/v1/media/{created['id']}/archive")
        second = await client.post(f"/api/v1/media/{created['id']}/archive")

        assert first.status_code == 200
        assert first.json()["status"] == "archived"
        assert second.status_code == 409
        assert second.json()["code"] == "invalid_media_transition"
