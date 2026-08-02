"""Telegram as a :class:`~mediahub.application.delivery.ports.DeliveryProvider`.

Translates in both directions: a ``DeliveryRequest`` becomes an upload, and
Telegram's answer becomes a ``DeliveryReceipt``. Nothing above this file learns
what a conversation identifier or a file reference is.

Two Telegram facts are modelled rather than hidden, because forgetting either
produces a library that silently breaks:

* a file reference is usable **only by the bot that created it**, so the receipt
  records which principal owns it and a re-send checks that before trying;
* the stable identifier survives a token change but cannot be used to fetch, so
  it is kept alongside as the identity of the content.

**Known limitation, stated rather than hidden.** The client library reads a file
into memory before uploading it, so this provider reports
``supports_streaming=False``. Progress and the checksum are still real - the
artifact is read through a measuring wrapper - but a multi-gigabyte upload costs
that much RAM. That is why ``supports_large_files`` is tied to whether a
self-hosted Bot API server is configured: without one the ceiling is 50 MB and
the question does not arise.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.delivery.errors import (
    ArtifactTooLargeError,
    ReferenceNotUsableError,
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
from mediahub.infrastructure.delivery.shared.measured_reader import MeasuredReader
from mediahub.infrastructure.delivery.telegram.errors import PROVIDER, classify

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from mediahub.application.delivery.ports import (
        DeliveryProgressCallback,
        DeliveryRequest,
        DeliveryTarget,
        ResendRequest,
    )
    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.domain.common.fingerprint import Fingerprint
    from mediahub.infrastructure.delivery.telegram.client import (
        TelegramUploader,
        UploadedMedia,
    )

CHAT_FIELD: Final[str] = "chat"
"""Key under which a target address carries its conversation identifier.

Shared by convention with :mod:`mediahub.presentation.telegram.handlers`, which
builds the address. The two adapters must not import each other, so a contract
test asserts that a target built there is one this provider accepts.
"""

BOT_API_MAX_BYTES: Final[int] = 50 * 1024 * 1024
"""What the public Bot API accepts for an upload."""

LOCAL_API_MAX_BYTES: Final[int] = 2 * 1024 * 1024 * 1024
"""What a self-hosted Bot API server accepts."""

MAX_CAPTION_LENGTH: Final[int] = 1024

MAX_THUMBNAIL_BYTES: Final[int] = 200 * 1024
"""What Telegram accepts as a poster image, alongside "JPEG, and nothing else"."""


class TelegramDeliveryProvider:
    """Sends artifacts to a Telegram conversation."""

    __slots__ = ("_bot_principal", "_local_api_server", "_max_bytes", "_uploader")

    def __init__(
        self,
        uploader: TelegramUploader,
        *,
        bot_principal: str,
        local_api_server: bool = False,
    ) -> None:
        """Wire the provider to a client.

        Args:
            uploader: The Telegram client.
            bot_principal: Stable identity of the bot whose credentials issue
                the references. Recorded on every receipt, because a reference
                issued by one bot is worthless to another.
            local_api_server: Whether a self-hosted Bot API server is in use,
                which raises the upload ceiling from 50 MB to about 2 GB.
        """
        self._uploader = uploader
        self._bot_principal = bot_principal
        self._local_api_server = local_api_server
        self._max_bytes = LOCAL_API_MAX_BYTES if local_api_server else BOT_API_MAX_BYTES

    @property
    def name(self) -> str:
        """Return the provider's name."""
        return PROVIDER

    def capabilities(self) -> DeliveryCapabilities:
        """Return what this destination currently accepts."""
        return DeliveryCapabilities(
            provider=PROVIDER,
            maximum_file_size=self._max_bytes,
            # False, and honestly so: the client library buffers the whole file
            # before uploading it. See the module docstring.
            supports_streaming=False,
            supports_large_files=self._local_api_server,
            # Telegram keeps the bytes and hands back a reusable reference,
            # which is what makes it a custodian - and therefore what makes
            # deleting the local copy safe.
            supports_resend=True,
            can_serve_back=True,
            # A bot cannot reliably delete arbitrary messages it has sent, and
            # claiming otherwise would make a future cleanup feature lie.
            supports_delete=False,
            supports_metadata=True,
            supports_thumbnails=True,
            supports_history=True,
            max_caption_length=MAX_CAPTION_LENGTH,
            allowed_kinds=frozenset(DeliveryKind),
        )

    def supports(self, target: DeliveryTarget) -> bool:
        """Return whether this provider owns ``target``."""
        return target.provider == PROVIDER and bool(target.address.opaque.get(CHAT_FIELD))

    # -- Delivery ------------------------------------------------------------

    async def deliver(
        self,
        request: DeliveryRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: DeliveryProgressCallback | None = None,
    ) -> DeliveryReceipt:
        """Upload the artifact and return proof that Telegram accepted it."""
        chat = self._chat_of(request.target)
        size = request.artifact.size_bytes
        if size > self._max_bytes:
            raise ArtifactTooLargeError(self._max_bytes, size, provider=PROVIDER)

        _report(on_progress, DeliveryStage.PREPARING, 0, size)
        path = workspace.path_for(request.artifact.name)
        thumbnail = self._poster(request, workspace)

        started = time.monotonic()
        with path.open("rb") as handle:
            reader = MeasuredReader(
                handle,
                total_bytes=size,
                on_chunk=lambda sent, total: _report(
                    on_progress, DeliveryStage.UPLOADING, sent, total
                ),
                # The library buffers the whole file (see the module docstring),
                # so the destination's ceiling doubles as this process's memory
                # ceiling. Without it, an artifact that grew between the size
                # check above and the read below is an OOM kill rather than a
                # failed job.
                max_buffer_bytes=self._max_bytes,
                provider=PROVIDER,
            )
            uploaded = await self._upload(request, chat, reader, thumbnail)
            checksum = reader.fingerprint()
            measured = reader.bytes_read

        _report(on_progress, DeliveryStage.COMPLETED, measured or size, size)
        logger.bind(provider=PROVIDER, kind=request.kind.value, bytes=uploaded.bytes_sent).info(
            "Delivered artifact"
        )

        return self._receipt(
            remote_id=uploaded.file_id,
            remote_unique_id=uploaded.file_unique_id,
            container_id=uploaded.chat_id,
            message_id=uploaded.message_id,
            size_bytes=uploaded.bytes_sent or measured,
            started=started,
            checksum=checksum,
            reused=False,
        )

    async def resend(self, request: ResendRequest) -> DeliveryReceipt:
        """Re-deliver by reference: one call, zero bytes.

        Raises:
            ReferenceNotUsableError: If the reference was issued to different
                credentials. Falling back to an upload is the caller's decision,
                not ours - we have no artifact to upload.
        """
        chat = self._chat_of(request.target)
        if not request.reference.is_usable_by(self._bot_principal):
            message = "That reference was issued to different credentials and cannot " "be re-sent."
            raise ReferenceNotUsableError(message, provider=PROVIDER)

        started = time.monotonic()
        try:
            uploaded = await self._uploader.send_by_reference(
                chat_id=chat,
                reference=request.reference.remote_id,
                kind=request.kind.value,
                caption=_truncate(request.caption),
            )
        except Exception as exc:
            raise classify(exc, detail="resend") from exc

        logger.bind(provider=PROVIDER, reused=True).info("Re-sent artifact by reference")
        return self._receipt(
            remote_id=uploaded.file_id or request.reference.remote_id,
            remote_unique_id=uploaded.file_unique_id or request.reference.remote_unique_id,
            container_id=uploaded.chat_id,
            message_id=uploaded.message_id,
            size_bytes=0,
            started=started,
            checksum=None,
            reused=True,
        )

    # -- Internals -----------------------------------------------------------

    async def _upload(
        self,
        request: DeliveryRequest,
        chat: str,
        reader: MeasuredReader,
        thumbnail: Path | None,
    ) -> UploadedMedia:
        """Perform the upload, translating any failure on the way out."""
        try:
            return await self._uploader.send_media(
                chat_id=chat,
                content=reader,
                kind=request.kind.value,
                caption=_truncate(request.caption),
                filename=request.filename or request.artifact.name,
                duration_seconds=request.duration_seconds,
                width=request.width,
                height=request.height,
                thumbnail=thumbnail,
            )
        except Exception as exc:
            # The detail deliberately omits the path, the token and the
            # destination's own text: a delivery error is shown to a person.
            raise classify(exc, detail=request.kind.value) from exc

    @staticmethod
    def _poster(request: DeliveryRequest, workspace: WorkspaceScope) -> Path | None:
        """Return the poster image, but only if Telegram will accept it.

        Telegram takes **JPEG only**, under 200 kB. Engines commonly produce
        WebP, which is smaller and better and which Telegram rejects. Sending it
        anyway either fails the whole delivery or has the image silently
        discarded, so it is dropped here instead - and the destination generates
        its own poster from the video, which is what it does when none is given.
        """
        if request.thumbnail is None:
            return None
        name = request.thumbnail.name.lower()
        if not name.endswith((".jpg", ".jpeg")):
            logger.bind(provider=PROVIDER, thumbnail=request.thumbnail.name).debug(
                "Poster image is not JPEG; letting the destination generate its own"
            )
            return None
        if request.thumbnail.size_bytes > MAX_THUMBNAIL_BYTES:
            return None
        return workspace.path_for(request.thumbnail.name)

    def _chat_of(self, target: DeliveryTarget) -> str:
        """Return the conversation this target names, or refuse."""
        chat = target.address.opaque.get(CHAT_FIELD)
        if not chat:
            message = "the delivery target names no conversation"
            raise classify(ValueError(message), detail="invalid target")
        return chat

    def _receipt(
        self,
        *,
        remote_id: str | None,
        remote_unique_id: str | None,
        container_id: str,
        message_id: int,
        size_bytes: int,
        started: float,
        checksum: Fingerprint | None,
        reused: bool,
    ) -> DeliveryReceipt:
        """Assemble a receipt from what Telegram reported."""
        return DeliveryReceipt(
            provider=PROVIDER,
            reference=RemoteArtifactRef(
                provider=PROVIDER,
                principal=self._bot_principal,
                remote_id=remote_id or "",
                remote_unique_id=remote_unique_id,
            ),
            message=RemoteMessageRef(
                provider=PROVIDER,
                container_id=container_id,
                message_id=str(message_id),
            ),
            size_bytes=size_bytes,
            delivery_time=timedelta(seconds=max(0.0, time.monotonic() - started)),
            delivered_at=datetime.now(UTC),
            can_serve_back=True,
            checksum=checksum,
            reused_reference=reused,
        )


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


def _truncate(caption: str | None) -> str | None:
    """Trim a caption to what Telegram accepts."""
    if caption is None:
        return None
    trimmed = caption.strip()
    if len(trimmed) <= MAX_CAPTION_LENGTH:
        return trimmed or None
    return trimmed[: MAX_CAPTION_LENGTH - 1] + "…"
