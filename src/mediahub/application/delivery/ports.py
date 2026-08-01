"""The delivery framework.

A **provider** takes one artifact out of a workspace lease and hands it to one
destination, returning a **receipt**. That is its entire job. It does not touch
the database, change a job's status, delete files, decide retries or apply
policy - those belong to the application, and a provider that reaches for them
has stopped being a provider.

A **router** picks the provider for a target. It exists so that adding a
destination - filesystem, NAS, S3, webhook, email - is a new adapter plus a
registry entry, and changes no application code at all.

Nothing here names a destination. ``chat_id`` is one provider's spelling of
:class:`TargetAddress`; ``file_id`` is one provider's spelling of
:class:`RemoteArtifactRef`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

PERCENT: Final[int] = 100

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Mapping
    from datetime import datetime, timedelta

    from mediahub.application.workspace.ports import ArtifactRef, WorkspaceScope
    from mediahub.domain.common.fingerprint import Fingerprint


# --------------------------------------------------------------------------- #
# Targets                                                                      #
# --------------------------------------------------------------------------- #


class DeliveryKind(StrEnum):
    """How a destination should present an artifact.

    A hint, not a guarantee: a destination that cannot honour it falls back to
    whatever it can do and reports what it did in the receipt.
    """

    VIDEO = "video"
    AUDIO = "audio"
    PHOTO = "photo"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class TargetAddress:
    """Where a destination should put something, in its own vocabulary.

    Opaque on purpose: the application copies it around and only the owning
    provider interprets it. A chat, a bucket and key, a directory, a webhook
    URL - all the same shape from here.

    Attributes:
        provider: Which provider understands this address.
        opaque: Provider-specific fields, as strings.
    """

    provider: str
    opaque: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """A destination an artifact can be sent to.

    Attributes:
        provider: The provider that owns this destination.
        address: Where, in that provider's terms.
        label: Human-readable name for messages and history.
    """

    provider: str
    address: TargetAddress
    label: str | None = None


# --------------------------------------------------------------------------- #
# References and receipts                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RemoteArtifactRef:
    """A handle the destination gave back, identifying the stored bytes.

    Attributes:
        provider: Which destination issued it.
        principal: Whose credentials it belongs to. Some destinations - Telegram
            among them - issue references usable only by the account that
            created them, so the owner is part of the reference's identity.
            Losing this field is how a credential rotation silently invalidates
            a whole library.
        remote_id: The reference itself, usable to re-send or fetch.
        remote_unique_id: A stable identifier that survives credential changes
            but cannot be used to fetch. Worth keeping: it is how a re-acquired
            file is recognised as the same content.
        expires_at: When the reference stops working, if the destination says.
    """

    provider: str
    principal: str
    remote_id: str
    remote_unique_id: str | None = None
    expires_at: datetime | None = None

    def is_usable_by(self, principal: str) -> bool:
        """Return whether ``principal`` may use this reference.

        A reference issued to one set of credentials is worthless to another.
        Providers check this before attempting a re-send, so a rotated
        credential produces a clean fall-back to uploading rather than a
        confusing failure from the destination.
        """
        return bool(self.remote_id) and self.principal == principal


@dataclass(frozen=True, slots=True)
class RemoteMessageRef:
    """Where the destination announced the artifact.

    Attributes:
        provider: Which destination issued it.
        container_id: The conversation, bucket or topic.
        message_id: The announcement itself.
    """

    provider: str
    container_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Durable proof that a destination accepted an artifact.

    This is the most important object in the product. It is what authorises
    deleting the local copy, and what makes a later re-send possible without
    downloading anything. It is issued **only** after the destination has
    confirmed - never optimistically.

    Attributes:
        provider: Which destination accepted it.
        reference: The remote reference it handed back.
        size_bytes: How much was transferred.
        delivery_time: How long the transfer took.
        delivered_at: When the destination confirmed (UTC).
        can_serve_back: Whether the bytes can be retrieved from this destination
            again. **Only a receipt with this set may justify deleting the local
            copy** - a webhook accepting a POST is a delivery, not a custodian.
        checksum: Hash of exactly what was transferred, when the provider
            computed one. Absent for a re-send, where no bytes moved.
        message: Where the artifact was announced, when it was.
        reused_reference: Whether this delivery reused an existing remote
            reference instead of uploading. Reported so a caller can tell a
            zero-byte re-send from a real transfer.
    """

    provider: str
    reference: RemoteArtifactRef
    size_bytes: int
    delivery_time: timedelta
    delivered_at: datetime
    can_serve_back: bool
    checksum: Fingerprint | None = None
    message: RemoteMessageRef | None = None
    reused_reference: bool = False

    @property
    def provider_asset_id(self) -> str:
        """Return the destination's identifier for the stored bytes."""
        return self.reference.remote_id

    @property
    def provider_message_id(self) -> str | None:
        """Return the destination's identifier for the announcement, if any."""
        return None if self.message is None else self.message.message_id

    @property
    def throughput_bytes_per_second(self) -> float | None:
        """Return the observed transfer rate, when it is meaningful."""
        seconds = self.delivery_time.total_seconds()
        if seconds <= 0 or self.size_bytes <= 0:
            return None
        return self.size_bytes / seconds


# --------------------------------------------------------------------------- #
# Capabilities                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DeliveryCapabilities:
    """What a destination can do, given how it is configured.

    Reported rather than assumed, so a caller adapts instead of discovering a
    limitation by failing. Every flag defaults to the conservative answer: a
    provider must opt *in* to a capability, so a new adapter that forgets to
    declare one is merely under-used rather than wrong.

    Attributes:
        provider: Provider name.
        maximum_file_size: Largest artifact accepted, in bytes.
        supports_streaming: Artifacts are uploaded incrementally rather than
            read into memory first. False means large files cost RAM.
        supports_large_files: The destination is happy with multi-gigabyte
            artifacts. Distinct from the ceiling: a provider may accept 2 GB and
            still be a poor choice for it.
        supports_resend: An earlier reference can be re-delivered without
            uploading again - the zero-byte path.
        supports_delete: Delivered artifacts can be removed from the
            destination afterwards.
        supports_metadata: Titles, durations and dimensions are preserved.
        supports_thumbnails: A poster image can accompany the artifact.
        supports_history: The destination keeps its own record of what was sent.
        can_serve_back: Stored bytes can be retrieved again. **This is the flag
            that decides whether the local copy may be deleted.**
        max_caption_length: Longest caption, when the destination has a limit.
        allowed_kinds: Presentation kinds this destination understands.
    """

    provider: str
    maximum_file_size: int
    supports_streaming: bool = False
    supports_large_files: bool = False
    supports_resend: bool = False
    supports_delete: bool = False
    supports_metadata: bool = False
    supports_thumbnails: bool = False
    supports_history: bool = False
    can_serve_back: bool = False
    max_caption_length: int | None = None
    allowed_kinds: frozenset[DeliveryKind] = field(default_factory=lambda: frozenset(DeliveryKind))

    def accepts(self, size_bytes: int) -> bool:
        """Return whether an artifact of this size is within the ceiling."""
        return size_bytes <= self.maximum_file_size


# --------------------------------------------------------------------------- #
# Progress                                                                     #
# --------------------------------------------------------------------------- #


class DeliveryStage(StrEnum):
    """Where a provider is in a single delivery.

    Attributes:
        PREPARING: Checking limits, resolving the artifact.
        UPLOADING: Bytes are moving.
        FINALISING: The destination is acknowledging.
        COMPLETED: A receipt exists.
    """

    PREPARING = "preparing"
    UPLOADING = "uploading"
    FINALISING = "finalising"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class DeliveryProgress:
    """One observation of an in-flight delivery.

    On a domestic connection the upload is usually slower than the download, so
    this is the part a user actually watches.

    Attributes:
        stage: What the provider is doing.
        sent_bytes: Bytes handed to the destination so far.
        total_bytes: Size of the artifact, when known.
        provider: Which provider is reporting.
    """

    stage: DeliveryStage
    sent_bytes: int = 0
    total_bytes: int | None = None
    provider: str | None = None

    @property
    def percentage(self) -> float | None:
        """Return completion in percent, or ``None`` if the total is unknown."""
        if not self.total_bytes:
            return None
        ratio = min(self.sent_bytes / self.total_bytes, 1.0)
        return round(ratio * PERCENT, 2)


type DeliveryProgressCallback = Callable[[DeliveryProgress], None]
"""Invoked as a delivery advances.

Must be cheap, non-blocking and must not raise: a provider may call it from a
worker thread, and an exception there would abort the transfer it is merely
describing.
"""


# --------------------------------------------------------------------------- #
# Requests                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DeliveryRequest:
    """One artifact, on its way to one destination.

    Attributes:
        target: Where it is going.
        artifact: Which file in the lease to send.
        kind: How it should be presented.
        caption: Optional text to accompany it. Truncated by the provider to
            whatever the destination allows.
        filename: Name to present it under. The provider sanitises it.
        duration_seconds: Media duration, when known.
        width: Frame width, when known.
        height: Frame height, when known.
        thumbnail: Optional poster image from the same lease.
    """

    target: DeliveryTarget
    artifact: ArtifactRef
    kind: DeliveryKind = DeliveryKind.DOCUMENT
    caption: str | None = None
    filename: str | None = None
    duration_seconds: int | None = None
    width: int | None = None
    height: int | None = None
    thumbnail: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class ResendRequest:
    """An artifact the destination already holds, sent somewhere again.

    No workspace, no bytes, no download - the whole point. Only providers that
    declare :attr:`DeliveryCapabilities.supports_resend` accept one.

    Attributes:
        target: Where it is going this time.
        reference: The reference the destination issued earlier.
        kind: How it should be presented.
        caption: Optional text to accompany it.
    """

    target: DeliveryTarget
    reference: RemoteArtifactRef
    kind: DeliveryKind = DeliveryKind.DOCUMENT
    caption: str | None = None


# --------------------------------------------------------------------------- #
# The provider and the router                                                  #
# --------------------------------------------------------------------------- #


class DeliveryProvider(Protocol):
    """Hands artifacts to one class of destination.

    Implementations must:

    * read only from the workspace lease they are given, and write nothing;
    * refuse an artifact larger than
      :attr:`DeliveryCapabilities.maximum_file_size` rather than attempting it;
    * raise a typed
      :class:`~mediahub.application.delivery.errors.DeliveryError`, never a raw
      library exception;
    * return a receipt only once the destination has actually confirmed;
    * report progress through the supplied callback, if they can.

    Implementations must **not** persist anything, change a job's state, delete
    a file, decide whether to retry, or apply any policy beyond their own
    destination's limits.
    """

    @property
    def name(self) -> str:
        """Return the provider's name."""
        ...

    def capabilities(self) -> DeliveryCapabilities:
        """Return what this destination can currently accept."""
        ...

    def supports(self, target: DeliveryTarget) -> bool:
        """Return whether this provider owns ``target``. Pure and fast."""
        ...

    async def deliver(
        self,
        request: DeliveryRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: DeliveryProgressCallback | None = None,
    ) -> DeliveryReceipt:
        """Send the artifact and return proof that it arrived.

        Raises:
            ArtifactTooLargeError: If the artifact exceeds the ceiling.
            DeliveryAuthenticationError: If the credentials were rejected.
            TargetUnreachableError: If the destination refuses this sender.
            DeliveryRateLimitedError: If the destination asked us to slow down.
            ProviderUnavailableError: If the destination is temporarily down.
        """
        ...

    async def resend(self, request: ResendRequest) -> DeliveryReceipt:
        """Re-deliver something the destination already holds.

        Costs one call and zero bytes. Providers that do not declare
        :attr:`DeliveryCapabilities.supports_resend` raise
        :class:`~mediahub.application.delivery.errors.ResendNotSupportedError`.

        Raises:
            ResendNotSupportedError: If this destination cannot re-send.
            ReferenceNotUsableError: If the reference belongs to different
                credentials, or has expired.
        """
        ...


class DeliveryRouter(Protocol):
    """Chooses the provider for a target, and delegates to it.

    The application depends on this rather than on any provider, which is what
    lets a destination be added without touching a use case.
    """

    def provider_for(self, target: DeliveryTarget) -> DeliveryProvider:
        """Return the provider that owns ``target``.

        Raises:
            NoProviderForTargetError: If no enabled provider claims it.
        """
        ...

    def capabilities_for(self, target: DeliveryTarget) -> DeliveryCapabilities:
        """Return the capabilities of whichever provider owns ``target``."""
        ...

    def default_capabilities(self) -> DeliveryCapabilities:
        """Return the capabilities of the destination used when none is named.

        Callers need a ceiling *before* a target exists - to decide what to
        offer a user, and to cap a download at something that can actually be
        sent.
        """
        ...

    async def deliver(
        self,
        request: DeliveryRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: DeliveryProgressCallback | None = None,
    ) -> DeliveryReceipt:
        """Route the request to its provider and deliver it."""
        ...

    async def resend(self, request: ResendRequest) -> DeliveryReceipt:
        """Route the request to its provider and re-send it."""
        ...
