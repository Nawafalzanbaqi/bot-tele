"""Chooses the provider for a destination, and delegates to it.

Implements :class:`~mediahub.application.delivery.ports.DeliveryRouter`. This
is the component that makes "add a destination without changing application
code" true: a new provider is a class plus one registration, and every use case
keeps talking to the router.

Selection is deterministic - configured priority first, then name - because a
provider chosen by dictionary order is a bug that reproduces on one machine in
three.

What the registry does **not** do: retry, persist, or decide policy. It selects,
delegates, and records whether the delegate worked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

from mediahub.application.delivery.errors import (
    DeliveryError,
    DeliveryNotConfiguredError,
    NoProviderForTargetError,
)
from mediahub.domain.download.enums import FailureKind
from mediahub.infrastructure.delivery.circuit import CircuitBreaker, CircuitState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mediahub.application.delivery.ports import (
        DeliveryCapabilities,
        DeliveryProgressCallback,
        DeliveryProvider,
        DeliveryReceipt,
        DeliveryRequest,
        DeliveryTarget,
        ResendRequest,
    )
    from mediahub.application.workspace.ports import WorkspaceScope

DEFAULT_PRIORITY = 100


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """One provider, and how the registry should treat it.

    Attributes:
        provider: The provider itself.
        priority: Higher wins when several claim a target.
        enabled: Registered but switched off. Kept rather than removed so a
            deployment can disable a destination without a code change.
    """

    provider: DeliveryProvider
    priority: int = DEFAULT_PRIORITY
    enabled: bool = True


@dataclass(slots=True)
class DeliveryProviderRegistry:
    """Routes deliveries to registered providers.

    Attributes:
        registrations: Everything installed in this build.
        default_provider: Name of the destination used when a caller needs a
            ceiling before it has a target.
        failure_threshold: Consecutive transient failures that take a provider
            out of preference.
        cooldown_seconds: How long it stays out.
    """

    registrations: Sequence[ProviderRegistration] = ()
    default_provider: str | None = None
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Give every registered provider its own circuit breaker."""
        self._breakers = {
            registration.provider.name: CircuitBreaker(
                failure_threshold=self.failure_threshold,
                cooldown_seconds=self.cooldown_seconds,
            )
            for registration in self.registrations
        }

    # -- Selection -----------------------------------------------------------

    @property
    def provider_names(self) -> tuple[str, ...]:
        """Return the names of every enabled provider, best first."""
        return tuple(
            registration.provider.name for registration in self._ordered() if registration.enabled
        )

    def provider_for(self, target: DeliveryTarget) -> DeliveryProvider:
        """Return the provider that owns ``target``.

        Candidates are ordered by priority then name. A provider whose circuit
        is open is moved to the back rather than removed: if it is the only one
        that claims the target, trying it and reporting the destination's real
        error beats reporting that nothing is configured.

        Raises:
            NoProviderForTargetError: If no enabled provider claims it.
        """
        candidates = [
            registration
            for registration in self._ordered()
            if registration.enabled and registration.provider.supports(target)
        ]
        if not candidates:
            raise NoProviderForTargetError(target.provider)

        candidates.sort(key=lambda registration: self._is_degraded(registration.provider.name))
        return candidates[0].provider

    def capabilities_for(self, target: DeliveryTarget) -> DeliveryCapabilities:
        """Return the capabilities of whichever provider owns ``target``."""
        return self.provider_for(target).capabilities()

    def default_capabilities(self) -> DeliveryCapabilities:
        """Return the capabilities of the default destination.

        Raises:
            DeliveryNotConfiguredError: If nothing is registered, or the
                configured default is not installed.
        """
        enabled = [registration for registration in self._ordered() if registration.enabled]
        if not enabled:
            raise DeliveryNotConfiguredError

        if self.default_provider is not None:
            for registration in enabled:
                if registration.provider.name == self.default_provider:
                    return registration.provider.capabilities()
            raise DeliveryNotConfiguredError(self.default_provider)

        return enabled[0].provider.capabilities()

    def health(self) -> Mapping[str, CircuitState]:
        """Return each provider's current circuit state, for diagnostics."""
        return {name: breaker.state for name, breaker in self._breakers.items()}

    # -- Delegation ----------------------------------------------------------

    async def deliver(
        self,
        request: DeliveryRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: DeliveryProgressCallback | None = None,
    ) -> DeliveryReceipt:
        """Route the request to its provider and deliver it."""
        provider = self.provider_for(request.target)
        return await self._guard(
            provider.name,
            lambda: provider.deliver(request, workspace, on_progress=on_progress),
        )

    async def resend(self, request: ResendRequest) -> DeliveryReceipt:
        """Route the request to its provider and re-send it."""
        provider = self.provider_for(request.target)
        return await self._guard(provider.name, lambda: provider.resend(request))

    # -- Internals -----------------------------------------------------------

    async def _guard(
        self, name: str, operation: Callable[[], Awaitable[DeliveryReceipt]]
    ) -> DeliveryReceipt:
        """Run a provider call, recording what it says about the provider.

        Only transient failures count against a provider's health. A file that
        is too large, or a conversation that no longer exists, says nothing
        about the destination and must not take a working one out of service.
        """
        breaker = self._breakers.setdefault(
            name,
            CircuitBreaker(
                failure_threshold=self.failure_threshold,
                cooldown_seconds=self.cooldown_seconds,
            ),
        )
        try:
            receipt = await operation()
        except DeliveryError as exc:
            if exc.kind is FailureKind.TRANSIENT:
                breaker.record_failure()
                if not breaker.is_healthy:
                    logger.bind(provider=name, failures=breaker.consecutive_failures).warning(
                        "Delivery provider marked degraded"
                    )
            raise
        else:
            breaker.record_success()
            return receipt

    def _ordered(self) -> list[ProviderRegistration]:
        """Return registrations in deterministic selection order."""
        return sorted(
            self.registrations,
            key=lambda registration: (-registration.priority, registration.provider.name),
        )

    def _is_degraded(self, name: str) -> bool:
        """Return whether a provider should be deprioritised right now."""
        breaker = self._breakers.get(name)
        return breaker is not None and not breaker.is_healthy
