"""Configuration validates itself, and refuses unsafe production setups."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from mediahub.shared.config.settings import (
    DatabaseSettings,
    Environment,
    PersistenceBackend,
    SecuritySettings,
    Settings,
    WorkerSettings,
)

pytestmark = pytest.mark.unit


def production(**overrides: object) -> Settings:
    """Build production settings that are safe unless a test breaks them."""
    defaults: dict[str, object] = {
        "environment": Environment.PRODUCTION,
        "debug": False,
        "database": DatabaseSettings(
            backend=PersistenceBackend.POSTGRES,
            password=SecretStr("a-real-generated-password"),
        ),
        "security": SecuritySettings(secret_key=SecretStr("a-real-generated-secret")),
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def test_defaults_are_usable_for_local_development() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment is Environment.LOCAL
    assert settings.api.port == 8000
    # SQLite is the system of record (ADR-0006): the default deployment is one
    # file on one device, with no second process to administer.
    assert settings.database.backend is PersistenceBackend.SQLITE


def test_dsn_is_async_and_masks_the_password_when_logged() -> None:
    database = DatabaseSettings(
        host="db", user="user name", password=SecretStr("p@ss/word"), name="mediahub"
    )

    assert database.dsn.startswith("postgresql+asyncpg://user+name:p%40ss%2Fword@db:5432/")
    assert "p@ss/word" not in database.safe_dsn
    assert "***" in database.safe_dsn


def test_settings_are_immutable() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValidationError):
        settings.debug = True  # type: ignore[misc]


def test_production_accepts_a_hardened_configuration() -> None:
    assert production().environment.is_production


@pytest.mark.parametrize(
    "overrides",
    [
        {"security": SecuritySettings(secret_key=SecretStr("change-me-in-production"))},
        {
            "database": DatabaseSettings(
                backend=PersistenceBackend.POSTGRES, password=SecretStr("mediahub")
            )
        },
        {"database": DatabaseSettings(backend=PersistenceBackend.MEMORY)},
        {"debug": True},
    ],
    ids=["placeholder-secret", "placeholder-password", "memory-backend", "debug-enabled"],
)
def test_production_refuses_unsafe_configuration(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        production(**overrides)


def test_production_on_sqlite_does_not_demand_a_postgres_password() -> None:
    """SQLite has no password, so the placeholder one is not a finding.

    Refusing to boot over an unused credential would be a false alarm - and a
    false alarm in a startup guard is how the guard gets disabled.
    """
    settings = production(database=DatabaseSettings(backend=PersistenceBackend.SQLITE))

    assert settings.database.backend is PersistenceBackend.SQLITE


class TestWorkerSettings:
    """A worker's timings are checked where the mistake is actually made."""

    def test_the_defaults_survive_a_missed_heartbeat(self) -> None:
        worker = WorkerSettings()

        assert worker.heartbeat_seconds * 2 <= worker.lease_seconds

    def test_a_heartbeat_slower_than_its_lease_is_refused(self) -> None:
        # The worst misconfiguration available: every healthy job is reclaimed
        # while it is still running, and it presents as duplicate execution.
        with pytest.raises(ValidationError, match="heartbeat_seconds"):
            WorkerSettings(lease_seconds=30.0, heartbeat_seconds=20.0)

    def test_a_backoff_ceiling_below_its_floor_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="max_idle_poll_seconds"):
            WorkerSettings(idle_poll_seconds=5.0, max_idle_poll_seconds=1.0)

    def test_a_worker_is_off_until_it_is_configured(self) -> None:
        assert not Settings(_env_file=None).worker.enabled

    def test_identity_parts_are_configurable(self) -> None:
        worker = WorkerSettings(role="delivery", index=3, host="pi5")

        assert (worker.role, worker.index, worker.host) == ("delivery", 3, "pi5")
