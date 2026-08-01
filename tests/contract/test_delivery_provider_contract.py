"""The contract every delivery provider must satisfy.

Parametrised over **every** implementation, so adding a destination means
adding one line here rather than writing a new suite. That is the mechanism
that keeps three providers honest with one set of assertions - and the reason a
fourth (filesystem, NAS, S3, webhook, email) can be integrated with confidence.

The load-bearing assertions:

* a receipt is issued only when the destination really took the bytes;
* ``can_serve_back`` on the receipt matches the provider's declared capability,
  because that flag is what authorises deleting the local copy;
* every failure is typed and classified;
* a provider reads only from the lease it is given, and writes nothing.

It also pins the one convention spanning two adapters that may not import each
other: the gateway builds a delivery target, and the provider must accept it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    DeliveryError,
    ResendNotSupportedError,
)
from mediahub.application.delivery.ports import (
    DeliveryKind,
    DeliveryProgress,
    DeliveryRequest,
    DeliveryStage,
    DeliveryTarget,
    RemoteArtifactRef,
    ResendRequest,
    TargetAddress,
)
from mediahub.infrastructure.delivery.dummy.provider import DummyDeliveryProvider
from mediahub.infrastructure.delivery.telegram.provider import TelegramDeliveryProvider
from mediahub.infrastructure.workspace.filesystem import FilesystemWorkspace
from mediahub.presentation.telegram.handlers import CHAT_FIELD, DELIVERY_PROVIDER
from tests.support.delivery_fakes import FakeDeliveryProvider
from tests.support.telegram_fakes import FakeUploader

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mediahub.application.delivery.ports import DeliveryProvider
    from mediahub.application.workspace.ports import WorkspaceScope

pytestmark = pytest.mark.integration

PROVIDER_NAMES = ["telegram", "dummy", "fake"]
PRINCIPALS = {"telegram": "bot-1", "dummy": "dummy", "fake": "fake-principal"}
ADDRESSES = {
    "telegram": {CHAT_FIELD: "1"},
    "dummy": {},
    "fake": {},
}


@pytest.fixture(params=PROVIDER_NAMES)
def provider(request: pytest.FixtureRequest) -> DeliveryProvider:
    """Every delivery provider implementation, one at a time."""
    if request.param == "telegram":
        return TelegramDeliveryProvider(FakeUploader(), bot_principal="bot-1")
    if request.param == "dummy":
        return DummyDeliveryProvider()
    return FakeDeliveryProvider()


@pytest.fixture
def target(provider: DeliveryProvider) -> DeliveryTarget:
    """A target the provider under test owns."""
    name = provider.name
    return DeliveryTarget(
        provider=name,
        address=TargetAddress(provider=name, opaque=ADDRESSES[name]),
    )


@pytest.fixture
def scope(tmp_path: Path) -> Iterator[WorkspaceScope]:
    workspace = FilesystemWorkspace(tmp_path / "ws")
    with workspace.lease(label="contract") as leased:
        leased.path_for("clip.mp4").write_bytes(b"x" * 1024)
        yield leased


def a_request(target: DeliveryTarget, scope: WorkspaceScope) -> DeliveryRequest:
    return DeliveryRequest(
        target=target, artifact=scope.artifact("clip.mp4"), kind=DeliveryKind.DOCUMENT
    )


class TestCapabilitiesContract:
    def test_reports_a_name_and_a_ceiling(self, provider: DeliveryProvider) -> None:
        capabilities = provider.capabilities()

        assert provider.name
        assert capabilities.provider == provider.name
        assert capabilities.maximum_file_size > 0

    def test_capabilities_are_stable(self, provider: DeliveryProvider) -> None:
        # Called on every acquisition to cap a download; it must be cheap and
        # must not change its mind between calls.
        assert provider.capabilities() == provider.capabilities()

    def test_accepts_agrees_with_the_ceiling(self, provider: DeliveryProvider) -> None:
        capabilities = provider.capabilities()

        assert capabilities.accepts(capabilities.maximum_file_size)
        assert not capabilities.accepts(capabilities.maximum_file_size + 1)

    def test_supports_is_pure_and_specific(
        self, provider: DeliveryProvider, target: DeliveryTarget
    ) -> None:
        assert provider.supports(target)
        assert provider.supports(target), "supports() must have no side effects"
        assert not provider.supports(
            DeliveryTarget(provider="nobody", address=TargetAddress(provider="nobody"))
        )


class TestDeliveryContract:
    async def test_a_receipt_proves_the_bytes_arrived(
        self, provider: DeliveryProvider, target: DeliveryTarget, scope: WorkspaceScope
    ) -> None:
        receipt = await provider.deliver(a_request(target, scope), scope)

        assert receipt.provider == provider.name
        assert receipt.size_bytes == 1024
        assert receipt.provider_asset_id
        assert receipt.reference.principal == PRINCIPALS[provider.name]
        assert receipt.delivered_at.tzinfo is not None
        assert receipt.delivery_time.total_seconds() >= 0
        assert receipt.reused_reference is False

    async def test_only_a_serving_destination_claims_custody(
        self, provider: DeliveryProvider, target: DeliveryTarget, scope: WorkspaceScope
    ) -> None:
        # This is the flag that authorises deleting the only local copy, so the
        # receipt and the declared capability must never disagree.
        receipt = await provider.deliver(a_request(target, scope), scope)

        assert receipt.can_serve_back == provider.capabilities().can_serve_back

    async def test_progress_is_reported_and_monotonic(
        self, provider: DeliveryProvider, target: DeliveryTarget, scope: WorkspaceScope
    ) -> None:
        seen: list[DeliveryProgress] = []

        await provider.deliver(a_request(target, scope), scope, on_progress=seen.append)

        assert seen, "a provider must report at least one progress observation"
        counts = [update.sent_bytes for update in seen]
        assert counts == sorted(counts), "progress must never go backwards"
        assert all(update.provider == provider.name for update in seen)
        assert seen[-1].stage in {DeliveryStage.COMPLETED, DeliveryStage.UPLOADING}

    async def test_a_missing_callback_is_fine(
        self, provider: DeliveryProvider, target: DeliveryTarget, scope: WorkspaceScope
    ) -> None:
        receipt = await provider.deliver(a_request(target, scope), scope)

        assert receipt.size_bytes > 0

    async def test_nothing_is_written_to_the_lease(
        self, provider: DeliveryProvider, target: DeliveryTarget, scope: WorkspaceScope
    ) -> None:
        before = set(scope.names())

        await provider.deliver(a_request(target, scope), scope)

        assert set(scope.names()) == before, "a provider reads; it must not write"

    async def test_refuses_an_artifact_over_the_ceiling(
        self, provider: DeliveryProvider, target: DeliveryTarget, scope: WorkspaceScope
    ) -> None:
        oversized = replace(
            scope.artifact("clip.mp4"),
            size_bytes=provider.capabilities().maximum_file_size + 1,
        )

        with pytest.raises(ArtifactTooLargeError) as excinfo:
            await provider.deliver(DeliveryRequest(target=target, artifact=oversized), scope)

        assert not excinfo.value.is_retryable, "a size refusal is policy, not weather"

    async def test_failures_are_typed(
        self, provider: DeliveryProvider, scope: WorkspaceScope
    ) -> None:
        foreign = DeliveryTarget(
            provider=provider.name, address=TargetAddress(provider=provider.name)
        )
        if provider.supports(foreign):
            pytest.skip("this provider needs no address fields")

        with pytest.raises(DeliveryError):
            await provider.deliver(
                DeliveryRequest(target=foreign, artifact=scope.artifact("clip.mp4")),
                scope,
            )


class TestResendContract:
    async def test_resend_matches_the_declared_capability(
        self, provider: DeliveryProvider, target: DeliveryTarget
    ) -> None:
        reference = RemoteArtifactRef(
            provider=provider.name,
            principal=PRINCIPALS[provider.name],
            remote_id="existing-ref",
        )
        request = ResendRequest(target=target, reference=reference)

        if not provider.capabilities().supports_resend:
            with pytest.raises(ResendNotSupportedError):
                await provider.resend(request)
            return

        receipt = await provider.resend(request)

        assert receipt.reused_reference is True
        assert receipt.size_bytes == 0, "a re-send must move no bytes"
        assert receipt.checksum is None, "nothing was hashed because nothing moved"
        assert receipt.provider_asset_id

    async def test_a_reference_from_other_credentials_is_refused(
        self, provider: DeliveryProvider, target: DeliveryTarget
    ) -> None:
        if not provider.capabilities().supports_resend:
            pytest.skip("this provider cannot re-send at all")

        stranger = RemoteArtifactRef(
            provider=provider.name, principal="someone-else", remote_id="ref"
        )

        with pytest.raises(DeliveryError):
            await provider.resend(ResendRequest(target=target, reference=stranger))


def test_the_gateways_target_is_one_the_provider_accepts() -> None:
    """Two adapters that may not import each other must still agree.

    The gateway builds a target address; the provider reads it. This asserts the
    convention they share, which is the only thing preventing a silent "nothing
    is ever delivered" if one side is renamed.
    """
    provider = TelegramDeliveryProvider(FakeUploader(), bot_principal="bot-1")
    built_by_gateway = DeliveryTarget(
        provider=DELIVERY_PROVIDER,
        address=TargetAddress(provider=DELIVERY_PROVIDER, opaque={CHAT_FIELD: "4242"}),
    )

    assert provider.supports(built_by_gateway)
