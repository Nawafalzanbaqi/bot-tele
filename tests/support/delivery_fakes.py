"""A scriptable delivery provider for tests.

The framework's whole claim is that a destination can be added without touching
application code. ``FakeDeliveryProvider`` is that claim exercised: it is a
third implementation alongside Telegram and the dummy, it declares different
capabilities, and it can be told to fail in any of the ways the taxonomy
describes.

Failure simulation is first-class here rather than bolted on, because "what
happens when the destination says 429" is a question the application answers
and therefore a question tests must be able to ask.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    ReferenceNotUsableError,
    ResendNotSupportedError,
)
from mediahub.application.delivery.ports import (
    DeliveryCapabilities,
    DeliveryKind,
    DeliveryProgress,
    DeliveryReceipt,
    DeliveryStage,
    RemoteArtifactRef,
    RemoteMessageRef,
)
from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mediahub.application.delivery.ports import (
        DeliveryProgressCallback,
        DeliveryRequest,
        DeliveryTarget,
        ResendRequest,
    )
    from mediahub.application.workspace.ports import WorkspaceScope

FAKE_PROVIDER = "fake"
FAKE_DIGEST = "f" * 64


@dataclass
class FakeDeliveryProvider:
    """A destination that records what it was asked to do.

    Attributes:
        provider_name: What it calls itself. The registry keys a destination's
            health on this, so two registrations sharing a name share a circuit.
        claims: Which target provider it accepts, if that differs from its own
            name. Two distinctly-named providers serving one destination is a
            real configuration - a Bot API and a local-API Telegram, say - and
            the only way to test that a degraded one loses to a healthy one.
        principal: Whose credentials it issues references under.
        maximum_file_size: Its ceiling.
        can_serve_back: Whether receipts from it authorise local deletion.
        supports_resend: Whether it accepts a re-send.
        fail_with: Raised by the next ``deliver`` call, if set.
        fail_times: How many calls should fail before it starts working. Lets a
            test simulate a destination that recovers.
    """

    provider_name: str = FAKE_PROVIDER
    claims: str | None = None
    principal: str = "fake-principal"
    maximum_file_size: int = 50 * 1024 * 1024
    can_serve_back: bool = True
    supports_resend: bool = True
    fail_with: Exception | None = None
    fail_times: int | None = None
    supports_albums: bool = True
    delivered: list[DeliveryRequest] = field(default_factory=list)
    albums: list[list[DeliveryRequest]] = field(default_factory=list)
    resent: list[ResendRequest] = field(default_factory=list)
    progress: list[DeliveryProgress] = field(default_factory=list)
    calls: int = 0

    @property
    def name(self) -> str:
        """Return the provider's name."""
        return self.provider_name

    def capabilities(self) -> DeliveryCapabilities:
        """Return the configured capabilities."""
        return DeliveryCapabilities(
            provider=self.provider_name,
            maximum_file_size=self.maximum_file_size,
            supports_streaming=True,
            supports_large_files=True,
            supports_resend=self.supports_resend,
            supports_delete=True,
            supports_metadata=True,
            supports_thumbnails=True,
            supports_history=False,
            supports_albums=self.supports_albums,
            can_serve_back=self.can_serve_back,
            max_caption_length=200,
            allowed_kinds=frozenset(DeliveryKind),
        )

    def supports(self, target: DeliveryTarget) -> bool:
        """Return whether this provider owns ``target``."""
        return target.provider == (self.claims or self.provider_name)

    async def deliver(
        self,
        request: DeliveryRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: DeliveryProgressCallback | None = None,
    ) -> DeliveryReceipt:
        """Record the request and answer with a plausible receipt."""
        self.calls += 1
        self._maybe_fail()

        capabilities = self.capabilities()
        if not capabilities.accepts(request.artifact.size_bytes):
            raise ArtifactTooLargeError(
                capabilities.maximum_file_size,
                request.artifact.size_bytes,
                provider=self.provider_name,
            )

        # Prove the provider can actually reach the artifact it was handed.
        path = workspace.path_for(request.artifact.name)
        size = path.stat().st_size

        self._report(on_progress, DeliveryStage.UPLOADING, size, size)
        self.delivered.append(request)
        return self._receipt(size=size, reused=False)

    async def deliver_album(
        self,
        requests: Sequence[DeliveryRequest],
        workspace: WorkspaceScope,
        *,
        on_progress: DeliveryProgressCallback | None = None,
    ) -> DeliveryReceipt:
        """Record the group and answer with one receipt for all of it."""
        self.calls += 1
        self._maybe_fail()
        capabilities = self.capabilities()
        total = 0
        for request in requests:
            if not capabilities.accepts(request.artifact.size_bytes):
                raise ArtifactTooLargeError(
                    capabilities.maximum_file_size,
                    request.artifact.size_bytes,
                    provider=self.provider_name,
                )
            total += workspace.path_for(request.artifact.name).stat().st_size
        self._report(on_progress, DeliveryStage.UPLOADING, total, total)
        self.albums.append(list(requests))
        return self._receipt(size=total, reused=False)

    async def resend(self, request: ResendRequest) -> DeliveryReceipt:
        """Record the re-send and answer with a zero-byte receipt."""
        self.calls += 1
        self._maybe_fail()
        if not self.supports_resend:
            message = "this destination cannot re-send"
            raise ResendNotSupportedError(message, provider=self.provider_name)
        if not request.reference.is_usable_by(self.principal):
            message = "that reference belongs to other credentials"
            raise ReferenceNotUsableError(message, provider=self.provider_name)
        self.resent.append(request)
        return self._receipt(size=0, reused=True)

    # -- Helpers -------------------------------------------------------------

    def _maybe_fail(self) -> None:
        """Raise the scripted failure, honouring ``fail_times``."""
        if self.fail_with is None:
            return
        if self.fail_times is not None and self.calls > self.fail_times:
            return
        raise self.fail_with

    def _report(
        self,
        callback: DeliveryProgressCallback | None,
        stage: DeliveryStage,
        sent: int,
        total: int,
    ) -> None:
        """Record and forward one progress observation."""
        update = DeliveryProgress(
            stage=stage, sent_bytes=sent, total_bytes=total, provider=self.provider_name
        )
        self.progress.append(update)
        if callback is not None:
            callback(update)

    def _receipt(self, *, size: int, reused: bool) -> DeliveryReceipt:
        """Build a receipt consistent with this provider's capabilities."""
        return DeliveryReceipt(
            provider=self.provider_name,
            reference=RemoteArtifactRef(
                provider=self.provider_name,
                principal=self.principal,
                remote_id=f"{self.provider_name}-ref-{self.calls}",
                remote_unique_id=f"{self.provider_name}-uniq",
            ),
            message=RemoteMessageRef(
                provider=self.provider_name, container_id="c", message_id=str(self.calls)
            ),
            size_bytes=size,
            delivery_time=timedelta(milliseconds=max(1, int(time.monotonic() % 10))),
            delivered_at=datetime.now(UTC),
            can_serve_back=self.can_serve_back,
            checksum=(
                None if reused else Fingerprint(algorithm=HashAlgorithm.SHA256, digest=FAKE_DIGEST)
            ),
            reused_reference=reused,
        )
