"""Shared fixtures and test doubles.

The whole suite runs against in-memory adapters, so it needs no database, no
network and no Docker - and it still exercises the real use cases, the real
domain rules and the real HTTP stack. That is the practical payoff of the
ports-and-adapters layout.

Test doubles here implement the same ports as production adapters:

* :class:`FrozenClock` - :class:`~mediahub.application.common.ports.Clock`
* :class:`SequentialUuidGenerator` -
  :class:`~mediahub.application.common.ports.UuidGenerator`
* :class:`RecordingEventPublisher` -
  :class:`~mediahub.application.common.ports.EventPublisher`

Because they satisfy the protocols structurally, mypy checks them exactly as it
checks the real ones.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from mediahub.infrastructure.di.container import Container
from mediahub.infrastructure.downloader.null_downloader import NullDownloader
from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
from mediahub.presentation.api.app import create_app
from mediahub.shared.config.settings import (
    ApiSettings,
    DatabaseSettings,
    Environment,
    LoggingSettings,
    LogLevel,
    PersistenceBackend,
    Settings,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from fastapi import FastAPI

    from mediahub.domain.common.events import DomainEvent

FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FrozenClock:
    """A clock that only moves when a test tells it to."""

    def __init__(self, moment: datetime = FIXED_NOW) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, seconds: int) -> datetime:
        """Move the clock forward and return the new time."""
        self._moment += timedelta(seconds=seconds)
        return self._moment


class SequentialUuidGenerator:
    """Produces predictable identifiers: 000...001, 000...002, and so on."""

    def __init__(self) -> None:
        self._counter = 0

    def new_uuid(self) -> UUID:
        self._counter += 1
        return UUID(int=self._counter)


class RecordingEventPublisher:
    """Keeps every published event so tests can assert on them."""

    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, events: Sequence[DomainEvent]) -> None:
        self.published.extend(events)

    def names(self) -> list[str]:
        """Return the names of published events, in order."""
        return [event.name for event in self.published]


@pytest.fixture
def settings() -> Settings:
    """Configuration for a test process: in-memory backend, quiet logs."""
    return Settings(
        _env_file=None,
        environment=Environment.TESTING,
        debug=True,
        api=ApiSettings(docs_enabled=True, cors_origins=[]),
        database=DatabaseSettings(backend=PersistenceBackend.MEMORY),
        logging=LoggingSettings(level=LogLevel.WARNING),
    )


@pytest.fixture
def clock() -> FrozenClock:
    """A clock frozen at :data:`FIXED_NOW`."""
    return FrozenClock()


@pytest.fixture
def uuid_generator() -> SequentialUuidGenerator:
    """A deterministic identifier source."""
    return SequentialUuidGenerator()


@pytest.fixture
def event_publisher() -> RecordingEventPublisher:
    """An event publisher that records instead of emitting."""
    return RecordingEventPublisher()


@pytest.fixture
def unit_of_work() -> InMemoryUnitOfWorkFactory:
    """A factory over one empty in-memory store."""
    return InMemoryUnitOfWorkFactory()


@pytest.fixture
def container(
    settings: Settings,
    clock: FrozenClock,
    uuid_generator: SequentialUuidGenerator,
    event_publisher: RecordingEventPublisher,
    unit_of_work: InMemoryUnitOfWorkFactory,
) -> Container:
    """A fully wired container using test doubles for every port."""
    return Container(
        settings=settings,
        clock=clock,
        uuid_generator=uuid_generator,
        event_publisher=event_publisher,
        unit_of_work=unit_of_work,
        downloader=NullDownloader(),
        database=None,
    )


@pytest.fixture
def app(settings: Settings, container: Container) -> FastAPI:
    """An application wired to the test container."""
    return create_app(settings, container)


@pytest.fixture
async def client(app: FastAPI, settings: Settings) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app, with the lifespan actually running.

    Entering ``lifespan_context`` matters: it is what attaches the container to
    ``app.state``, so these tests exercise the same startup path production
    does. The client presents the API key by default, the way every real
    caller must; the authentication tests override the header per request.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        headers = {"X-MediaHub-Key": settings.security.secret_key.get_secret_value()}
        async with AsyncClient(
            transport=transport, base_url="http://testserver", headers=headers
        ) as http:
            yield http
