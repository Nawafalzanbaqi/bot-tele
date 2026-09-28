"""Every versioned route needs the shared key; health does not.

The API is reachable by every container on the compose bridge and carries
routes that change state. The key it checks is the one production already had
to configure and nothing used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mediahub.presentation.api.security import API_KEY_HEADER
from mediahub.shared.config.settings import ApiSettings

if TYPE_CHECKING:
    from httpx import AsyncClient

pytestmark = pytest.mark.integration

PAYLOAD = {"source_url": "https://example.com/v/1", "title": "one", "media_type": "video"}


async def test_a_versioned_route_without_the_key_is_refused(client: AsyncClient) -> None:
    response = await client.get("/api/v1/media", headers={API_KEY_HEADER: ""})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == API_KEY_HEADER


async def test_a_wrong_key_is_refused_without_a_hint(client: AsyncClient) -> None:
    response = await client.get("/api/v1/media", headers={API_KEY_HEADER: "not-the-key"})

    assert response.status_code == 401
    assert "change-me" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/v1/media"),
        ("post", "/api/v1/media/00000000-0000-0000-0000-000000000000/archive"),
        ("post", "/api/v1/downloads"),
        ("post", "/api/v1/downloads/00000000-0000-0000-0000-000000000000/cancel"),
    ],
)
async def test_state_changing_routes_are_refused_before_they_run(
    client: AsyncClient, method: str, path: str
) -> None:
    """401 comes before validation: a bad body must not reveal the schema."""
    response = await client.request(method, path, json={}, headers={API_KEY_HEADER: ""})

    assert response.status_code == 401


async def test_the_right_key_is_accepted(client: AsyncClient) -> None:
    response = await client.get("/api/v1/media")

    assert response.status_code == 200


async def test_health_stays_open(client: AsyncClient) -> None:
    """The container healthcheck and pi-health call these without credentials."""
    for path in ("/health/live", "/health/ready", "/health"):
        response = await client.get(path, headers={API_KEY_HEADER: ""})
        assert response.status_code == 200, path


def test_docs_are_off_unless_asked_for() -> None:
    assert ApiSettings().docs_enabled is False
