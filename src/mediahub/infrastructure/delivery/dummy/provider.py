"""A provider that accepts artifacts and discards them.

Implements :class:`~mediahub.application.delivery.ports.DeliveryProvider`.

It performs a genuine read of the artifact - so the size and checksum on its
receipt are real - and then throws the bytes away. That makes it a faithful
dry-run: everything upstream behaves exactly as it would for a real
destination, and nothing leaves the machine.

Its capabilities are deliberately honest, and the important one is
``can_serve_back=False``. It cannot give the bytes back, so a receipt from here
must never be treated as custody transfer. A destination that accepts a file
and forgets it is a delivery, not a custodian - and the difference is whether
the local copy may be deleted.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    ResendNotSupportedError,
)
from mediahub.application.delivery.ports import (
    DeliveryCapabilities,
    DeliveryKind,
    DeliveryProgress,
    DeliveryReceipt,
    DeliveryStage,
    RemoteArtifactRef,
)
from mediahub.infrastructure.delivery.shared.measured_reader import MeasuredReader

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.delivery.ports import (
        DeliveryProgressCallback,
        DeliveryRequest,
        DeliveryTarget,
        ResendRequest,
    )
    from mediahub.application.workspace.ports import WorkspaceScope

PROVIDER: Final[str] = "dummy"
UNLIMITED: Final[int] = 1 << 62
"""No practical ceiling - it never stores anything, so nothing can be too big."""


class DummyDeliveryProvider:
    """Accepts an artifact, measures it, and discards it."""

    __slots__ = ("_deliveries", "_principal")

    def __init__(self, *, principal: str = "dummy") -> None:
        """Create a provider that reports deliveries under ``principal``."""
        self._principal = principal
        self._deliveries = 0

    @property
    def name(self) -> str:
        """Return the provider's name."""
        return PROVIDER

    @property
    def delivered_count(self) -> int:
        """Return how many artifacts this instance has accepted."""
        return self._deliveries

    def capabilities(self) -> DeliveryCapabilities:
        """Return what this destination can do - which is very little."""
        return DeliveryCapabilities(
            provider=PROVIDER,
            maximum_file_size=UNLIMITED,
            supports_streaming=True,
            supports_large_files=True,
            # It keeps nothing, so it can neither serve bytes back nor re-send
            # them. Saying otherwise would let the application delete the only
            # copy of a file on the strength of a receipt from a bin.
            supports_resend=False,
            can_serve_back=False,
            supports_delete=False,
            supports_metadata=False,
            supports_thumbnails=False,
            supports_history=False,
            allowed_kinds=frozenset(DeliveryKind),
        )

    def supports(self, target: DeliveryTarget) -> bool:
        """Return whether this provider owns ``target``."""
        return target.provider == PROVIDER

    async def deliver(
        self,
        request: DeliveryRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: DeliveryProgressCallback | None = None,
    ) -> DeliveryReceipt:
        """Read the artifact, measure it, and discard it."""
        capabilities = self.capabilities()
        if not capabilities.accepts(request.artifact.size_bytes):
            raise ArtifactTooLargeError(
                capabilities.maximum_file_size,
                request.artifact.size_bytes,
                provider=PROVIDER,
            )

        _report(on_progress, DeliveryStage.PREPARING, 0, request.artifact.size_bytes)
        started = time.monotonic()
        path = workspace.path_for(request.artifact.name)

        with path.open("rb") as handle:
            reader = MeasuredReader(
                handle,
                total_bytes=request.artifact.size_bytes,
                on_chunk=lambda sent, total: _report(
                    on_progress, DeliveryStage.UPLOADING, sent, total
                ),
            )
            while reader.read(1024 * 1024):
                pass
            checksum = reader.fingerprint()
            size = reader.bytes_read

        self._deliveries += 1
        _report(on_progress, DeliveryStage.COMPLETED, size, size)
        logger.bind(provider=PROVIDER, bytes=size).debug("Discarded artifact after measuring")

        return DeliveryReceipt(
            provider=PROVIDER,
            reference=RemoteArtifactRef(
                provider=PROVIDER,
                principal=self._principal,
                remote_id=f"dummy-{self._deliveries}",
            ),
            size_bytes=size,
            delivery_time=_elapsed(started),
            delivered_at=datetime.now(UTC),
            can_serve_back=False,
            checksum=checksum,
        )

    async def resend(self, request: ResendRequest) -> DeliveryReceipt:
        """Refuse: nothing was kept, so nothing can be sent again.

        Raises:
            ResendNotSupportedError: Always.
        """
        del request
        message = "The dummy destination keeps nothing and cannot re-send."
        raise ResendNotSupportedError(message, provider=PROVIDER)


def _report(
    callback: DeliveryProgressCallback | None,
    stage: DeliveryStage,
    sent: int,
    total: int | None,
) -> None:
    """Forward one progress observation, if anyone is listening."""
    if callback is not None:
        callback(
            DeliveryProgress(stage=stage, sent_bytes=sent, total_bytes=total, provider=PROVIDER)
        )


def _elapsed(started: float) -> timedelta:
    """Return how long has passed since ``started``."""
    return timedelta(seconds=max(0.0, time.monotonic() - started))
