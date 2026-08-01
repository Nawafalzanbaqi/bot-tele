"""Provider selection, failure isolation and the measuring reader.

The registry is what makes "add a destination without changing application
code" true, so its selection rules are pinned here: deterministic order,
configured priority, and a circuit that deprioritises a failing destination
without ever making a working one unreachable.
"""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING

import pytest

from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    DeliveryNotConfiguredError,
    DeliveryProviderError,
    NoProviderForTargetError,
    TargetUnreachableError,
)
from mediahub.application.delivery.ports import (
    DeliveryRequest,
    DeliveryTarget,
    RemoteArtifactRef,
    ResendRequest,
    TargetAddress,
)
from mediahub.domain.common.fingerprint import HashAlgorithm
from mediahub.infrastructure.delivery.circuit import CircuitBreaker, CircuitState
from mediahub.infrastructure.delivery.registry import (
    DeliveryProviderRegistry,
    ProviderRegistration,
)
from mediahub.infrastructure.delivery.shared.measured_reader import MeasuredReader
from mediahub.infrastructure.di.container import Container
from mediahub.infrastructure.downloader.null_downloader import NullDownloader
from mediahub.infrastructure.messaging.logging_event_publisher import LoggingEventPublisher
from mediahub.infrastructure.persistence.memory.factory import InMemoryUnitOfWorkFactory
from mediahub.infrastructure.system.clock import SystemClock
from mediahub.infrastructure.system.id_generator import Uuid4Generator
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.shared.config.settings import (
    DatabaseSettings,
    DeliverySettings,
    Environment,
    PersistenceBackend,
    Settings,
)
from tests.support.delivery_fakes import FakeDeliveryProvider

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.unit


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="registry") as leased:
        leased.path_for("clip.mp4").write_bytes(b"x" * 512)
        yield leased


def target_for(name: str) -> DeliveryTarget:
    return DeliveryTarget(provider=name, address=TargetAddress(provider=name))


def a_request(name: str, scope: WorkspaceScope) -> DeliveryRequest:
    return DeliveryRequest(target=target_for(name), artifact=scope.artifact("clip.mp4"))


class TestSelection:
    def test_routes_to_the_provider_that_claims_the_target(self) -> None:
        alpha = FakeDeliveryProvider(provider_name="alpha")
        beta = FakeDeliveryProvider(provider_name="beta")
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(alpha), ProviderRegistration(beta)]
        )

        assert registry.provider_for(target_for("beta")) is beta

    def test_priority_decides_between_claimants(self) -> None:
        low = FakeDeliveryProvider(provider_name="shared", maximum_file_size=1)
        high = FakeDeliveryProvider(provider_name="shared", maximum_file_size=999)
        registry = DeliveryProviderRegistry(
            registrations=[
                ProviderRegistration(low, priority=10),
                ProviderRegistration(high, priority=90),
            ]
        )

        assert registry.provider_for(target_for("shared")) is high

    def test_selection_is_deterministic(self) -> None:
        # Equal priority must not resolve by dictionary order: a bug that
        # reproduces on one machine in three is the worst kind.
        first = FakeDeliveryProvider(provider_name="shared")
        second = FakeDeliveryProvider(provider_name="shared")
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(first), ProviderRegistration(second)]
        )

        chosen = [registry.provider_for(target_for("shared")) for _ in range(10)]
        assert all(picked is chosen[0] for picked in chosen)

    def test_a_disabled_provider_is_not_selected(self) -> None:
        provider = FakeDeliveryProvider(provider_name="alpha")
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(provider, enabled=False)]
        )

        with pytest.raises(NoProviderForTargetError):
            registry.provider_for(target_for("alpha"))

    def test_an_unclaimed_target_is_refused(self) -> None:
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(FakeDeliveryProvider())]
        )

        with pytest.raises(NoProviderForTargetError) as excinfo:
            registry.provider_for(target_for("s3"))

        assert excinfo.value.target_provider == "s3"
        assert not excinfo.value.is_retryable

    def test_capabilities_come_from_the_owning_provider(self) -> None:
        provider = FakeDeliveryProvider(provider_name="alpha", maximum_file_size=4242)
        registry = DeliveryProviderRegistry(registrations=[ProviderRegistration(provider)])

        assert registry.capabilities_for(target_for("alpha")).maximum_file_size == 4242

    def test_provider_names_are_reported_best_first(self) -> None:
        registry = DeliveryProviderRegistry(
            registrations=[
                ProviderRegistration(FakeDeliveryProvider(provider_name="low"), priority=1),
                ProviderRegistration(FakeDeliveryProvider(provider_name="high"), priority=9),
            ]
        )

        assert registry.provider_names == ("high", "low")


class TestDefaultCapabilities:
    def test_uses_the_configured_default(self) -> None:
        registry = DeliveryProviderRegistry(
            registrations=[
                ProviderRegistration(FakeDeliveryProvider(provider_name="a", maximum_file_size=1)),
                ProviderRegistration(FakeDeliveryProvider(provider_name="b", maximum_file_size=2)),
            ],
            default_provider="b",
        )

        assert registry.default_capabilities().maximum_file_size == 2

    def test_falls_back_to_the_highest_priority(self) -> None:
        registry = DeliveryProviderRegistry(
            registrations=[
                ProviderRegistration(
                    FakeDeliveryProvider(provider_name="a", maximum_file_size=7), priority=99
                )
            ]
        )

        assert registry.default_capabilities().maximum_file_size == 7

    def test_an_empty_registry_is_a_configuration_error(self) -> None:
        with pytest.raises(DeliveryNotConfiguredError):
            DeliveryProviderRegistry().default_capabilities()

    def test_a_missing_default_is_a_configuration_error(self) -> None:
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(FakeDeliveryProvider(provider_name="a"))],
            default_provider="nowhere",
        )

        with pytest.raises(DeliveryNotConfiguredError):
            registry.default_capabilities()


class TestDelegation:
    async def test_delivers_through_the_chosen_provider(self, scope: WorkspaceScope) -> None:
        provider = FakeDeliveryProvider(provider_name="alpha")
        registry = DeliveryProviderRegistry(registrations=[ProviderRegistration(provider)])

        receipt = await registry.deliver(a_request("alpha", scope), scope)

        assert receipt.provider == "alpha"
        assert len(provider.delivered) == 1

    async def test_forwards_progress(self, scope: WorkspaceScope) -> None:
        provider = FakeDeliveryProvider(provider_name="alpha")
        registry = DeliveryProviderRegistry(registrations=[ProviderRegistration(provider)])
        seen: list[object] = []

        await registry.deliver(a_request("alpha", scope), scope, on_progress=seen.append)

        assert seen

    async def test_resends_through_the_chosen_provider(self) -> None:
        provider = FakeDeliveryProvider(provider_name="alpha")
        registry = DeliveryProviderRegistry(registrations=[ProviderRegistration(provider)])

        receipt = await registry.resend(
            ResendRequest(
                target=target_for("alpha"),
                reference=RemoteArtifactRef(
                    provider="alpha", principal="fake-principal", remote_id="r"
                ),
            )
        )

        assert receipt.reused_reference
        assert len(provider.resent) == 1

    async def test_the_registry_never_retries(self, scope: WorkspaceScope) -> None:
        # Deciding to try again is the caller's policy. A registry that retried
        # would silently multiply whatever budget the caller had set.
        provider = FakeDeliveryProvider(
            provider_name="alpha", fail_with=DeliveryProviderError("down")
        )
        registry = DeliveryProviderRegistry(registrations=[ProviderRegistration(provider)])

        with pytest.raises(DeliveryProviderError):
            await registry.deliver(a_request("alpha", scope), scope)

        assert provider.calls == 1


class TestFailureIsolation:
    async def test_transient_failures_eventually_degrade_a_provider(
        self, scope: WorkspaceScope
    ) -> None:
        provider = FakeDeliveryProvider(
            provider_name="alpha", fail_with=DeliveryProviderError("down")
        )
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(provider)], failure_threshold=2
        )

        for _ in range(2):
            with pytest.raises(DeliveryProviderError):
                await registry.deliver(a_request("alpha", scope), scope)

        assert registry.health()["alpha"] is CircuitState.OPEN

    async def test_a_degraded_provider_loses_to_a_healthy_one(self, scope: WorkspaceScope) -> None:
        # Two separately-named providers serving one destination: health is
        # tracked per provider, so one falling over must not take the other
        # with it.
        broken = FakeDeliveryProvider(
            provider_name="broken", claims="shared", fail_with=DeliveryProviderError("down")
        )
        working = FakeDeliveryProvider(provider_name="working", claims="shared")
        registry = DeliveryProviderRegistry(
            registrations=[
                ProviderRegistration(broken, priority=90),
                ProviderRegistration(working, priority=10),
            ],
            failure_threshold=1,
        )

        with pytest.raises(DeliveryProviderError):
            await registry.deliver(a_request("shared", scope), scope)

        # The higher-priority provider has failed, so the healthy one now wins.
        receipt = await registry.deliver(a_request("shared", scope), scope)
        assert len(working.delivered) == 1
        assert receipt.provider == "working"

    async def test_a_degraded_provider_is_still_tried_when_it_is_the_only_one(
        self, scope: WorkspaceScope
    ) -> None:
        # Reporting the destination's real error beats reporting that nothing
        # is configured.
        provider = FakeDeliveryProvider(
            provider_name="alpha", fail_with=DeliveryProviderError("down")
        )
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(provider)], failure_threshold=1
        )

        for _ in range(3):
            with pytest.raises(DeliveryProviderError):
                await registry.deliver(a_request("alpha", scope), scope)

        assert provider.calls == 3

    async def test_policy_and_permanent_failures_do_not_degrade_a_provider(
        self, scope: WorkspaceScope
    ) -> None:
        # A file that is too large, or a chat that no longer exists, says
        # nothing about the destination's health. Counting it would take a
        # working destination out of service because a user made a bad request.
        for failure in (
            ArtifactTooLargeError(1, 2, provider="alpha"),
            TargetUnreachableError("gone", provider="alpha"),
        ):
            provider = FakeDeliveryProvider(provider_name="alpha", fail_with=failure)
            registry = DeliveryProviderRegistry(
                registrations=[ProviderRegistration(provider)], failure_threshold=1
            )

            with pytest.raises(type(failure)):
                await registry.deliver(a_request("alpha", scope), scope)

            assert registry.health()["alpha"] is CircuitState.CLOSED

    async def test_a_success_clears_the_record(self, scope: WorkspaceScope) -> None:
        provider = FakeDeliveryProvider(
            provider_name="alpha", fail_with=DeliveryProviderError("blip"), fail_times=1
        )
        registry = DeliveryProviderRegistry(
            registrations=[ProviderRegistration(provider)], failure_threshold=2
        )

        with pytest.raises(DeliveryProviderError):
            await registry.deliver(a_request("alpha", scope), scope)
        await registry.deliver(a_request("alpha", scope), scope)

        assert registry.health()["alpha"] is CircuitState.CLOSED


class TestConfiguredWiring:
    """The composition root, where "no code change" has to actually be true."""

    def _container(self, delivery: DeliverySettings) -> Container:
        return Container(
            settings=Settings(
                _env_file=None,
                environment=Environment.TESTING,
                database=DatabaseSettings(backend=PersistenceBackend.MEMORY),
                delivery=delivery,
            ),
            clock=SystemClock(),
            uuid_generator=Uuid4Generator(),
            event_publisher=LoggingEventPublisher(),
            unit_of_work=InMemoryUnitOfWorkFactory(),
            downloader=NullDownloader(),
        )

    def test_configuration_decides_which_destination_is_preferred(self) -> None:
        container = self._container(
            DeliverySettings(default_provider="fake", telegram_priority=5, dummy_priority=900)
        )
        registry = container.delivery_router(
            FakeDeliveryProvider(provider_name="dummy"),
            FakeDeliveryProvider(provider_name="telegram"),
        )

        # Priority, not registration order, decides.
        assert registry.provider_names == ("dummy", "telegram")

    def test_the_discard_destination_is_installed_by_a_setting_alone(self) -> None:
        container = self._container(DeliverySettings(enable_dummy=True, default_provider="dummy"))
        registry = container.delivery_router()

        assert registry.provider_names == ("dummy",)
        assert registry.default_capabilities().provider == "dummy"

    def test_it_is_absent_unless_asked_for(self) -> None:
        container = self._container(DeliverySettings(default_provider="telegram"))
        registry = container.delivery_router(FakeDeliveryProvider(provider_name="telegram"))

        assert "dummy" not in registry.provider_names

    def test_an_instance_with_no_destination_says_so(self) -> None:
        # Not a crash and not a silent success: a use case asking for a ceiling
        # before anything is configured must get a typed refusal.
        container = self._container(DeliverySettings())

        with pytest.raises(DeliveryNotConfiguredError):
            container.delivery_router().default_capabilities()

    def test_the_circuit_settings_reach_the_registry(self) -> None:
        container = self._container(DeliverySettings(failure_threshold=7, cooldown_seconds=42.0))
        registry = container.delivery_router(FakeDeliveryProvider())

        assert registry.failure_threshold == 7
        assert registry.cooldown_seconds == 42.0


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        breaker = CircuitBreaker()

        assert breaker.state is CircuitState.CLOSED
        assert breaker.is_healthy

    def test_opens_at_the_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3)

        for _ in range(2):
            breaker.record_failure()
        # Read into locals: a narrowed property type survives the mutating call
        # otherwise, and the second assertion would be checked against the first.
        below_threshold = breaker.state

        breaker.record_failure()
        at_threshold = breaker.state

        assert below_threshold is CircuitState.CLOSED
        assert at_threshold is CircuitState.OPEN
        assert not breaker.is_healthy

    def test_half_opens_after_the_cooldown(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0)
        breaker.record_failure()

        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker.is_healthy, "a half-open circuit gets a chance to prove itself"

    def test_success_resets_it(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure()

        breaker.record_success()

        assert breaker.state is CircuitState.CLOSED
        assert breaker.consecutive_failures == 0


class TestMeasuredReader:
    def test_counts_and_hashes_in_one_pass(self) -> None:
        payload = b"a" * 5000
        reader = MeasuredReader(io.BytesIO(payload), total_bytes=len(payload))

        data = reader.read()

        assert data == payload
        assert reader.bytes_read == len(payload)
        assert reader.fingerprint().algorithm is HashAlgorithm.SHA256

    def test_the_digest_matches_a_plain_hash(self) -> None:
        payload = b"the quick brown fox" * 100
        reader = MeasuredReader(io.BytesIO(payload))
        reader.read()

        assert reader.fingerprint().digest == hashlib.sha256(payload).hexdigest()

    def test_reports_progress_per_chunk(self) -> None:
        seen: list[tuple[int, int | None]] = []
        payload = b"x" * 10_000
        reader = MeasuredReader(
            io.BytesIO(payload),
            total_bytes=len(payload),
            on_chunk=lambda sent, total: seen.append((sent, total)),
            chunk_bytes=1000,
        )

        reader.read()

        assert len(seen) == 10
        assert [sent for sent, _ in seen] == sorted(sent for sent, _ in seen)
        assert seen[-1] == (10_000, 10_000)

    def test_a_full_read_still_chunks(self) -> None:
        # A library that asks for the whole file must not defeat progress.
        seen: list[int] = []
        reader = MeasuredReader(
            io.BytesIO(b"x" * 4096),
            on_chunk=lambda sent, total: seen.append(sent),
            chunk_bytes=1024,
        )

        reader.read(-1)

        assert len(seen) == 4

    def test_partial_reads_are_supported(self) -> None:
        reader = MeasuredReader(io.BytesIO(b"abcdef"))

        assert reader.read(3) == b"abc"
        assert reader.bytes_read == 3
        assert reader.read(3) == b"def"

    def test_an_empty_stream_is_harmless(self) -> None:
        reader = MeasuredReader(io.BytesIO(b""))

        assert reader.read() == b""
        assert reader.bytes_read == 0

    def test_it_is_readable_as_a_file(self) -> None:
        reader = MeasuredReader(io.BytesIO(b"x" * 10))

        assert reader.readable()
        assert reader.readall() == b"x" * 10
