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

**How the bytes travel depends on which server is in use**, and the
difference is the whole reason a 2 GB ceiling is usable.

Against the public API the client library reads the file into memory before
uploading it. At the 50 MB public ceiling that is merely wasteful, and progress
and the checksum come free from the measuring wrapper the artifact is read
through.

Against a **self-hosted** server the file is handed over by path and the server
reads it itself: no buffer, no socket, and no 2 GB of resident memory on a
board that has 4 GB in total. The checksum is then computed by streaming the
file in chunks, so it stays as real and stays bounded.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

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
from mediahub.domain.common.fingerprint import Fingerprint, HashAlgorithm
from mediahub.infrastructure.delivery.shared.measured_reader import MeasuredReader
from mediahub.infrastructure.delivery.telegram.errors import (
    PROVIDER,
    classify,
    is_safe_to_retry,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
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

LOCAL_API_MAX_BYTES: Final[int] = 2000 * 1024 * 1024
"""What a self-hosted Bot API server accepts: 2000 MiB, not 2 GiB.

The 48 MiB between the two is the difference between a file that is uploaded and
one that is downloaded in full, hashed, handed over and then refused.
"""

MAX_CAPTION_LENGTH: Final[int] = 1024

RETRY_DELAY_SECONDS: Final[float] = 2.0
"""Pause before the single retry of a delivery that never reached the destination."""

MAX_RETRY_DELAY_SECONDS: Final[float] = 30.0
"""Longest pause honoured when the destination itself names a retry delay."""

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
            # True only where the file is handed over by path: against the
            # public API the library buffers the whole upload, and claiming
            # otherwise would let a caller offer something this cannot keep.
            supports_streaming=self._local_api_server,
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

        if self._local_api_server:
            return await self._deliver_by_path(request, chat, path, thumbnail, size, on_progress)

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

    async def _deliver_by_path(
        self,
        request: DeliveryRequest,
        chat: str,
        path: Path,
        thumbnail: Path | None,
        size: int,
        on_progress: DeliveryProgressCallback | None,
    ) -> DeliveryReceipt:
        """Hand the file's path to a self-hosted server instead of its bytes.

        The client library reads a whole upload into memory. At the 50 MB
        public ceiling that is merely wasteful; at the 2 GB ceiling a
        self-hosted server allows, it is an out-of-memory kill on a small
        board - so the ceiling would exist on paper and not in practice.

        The server reads the file directly, which also means the bytes are
        never copied over a socket to a process on the same machine.

        The checksum is still real. It is computed by streaming the file in
        chunks rather than by measuring an upload that no longer happens, which
        costs one sequential read and never more than a buffer of memory.
        """
        started = time.monotonic()
        _report(on_progress, DeliveryStage.UPLOADING, 0, size)
        checksum = await asyncio.to_thread(_digest_of, path)

        uploaded = await self._upload(request, chat, path, thumbnail, local_path=path)

        _report(on_progress, DeliveryStage.COMPLETED, size, size)
        logger.bind(provider=PROVIDER, kind=request.kind.value, bytes=size, streamed=True).info(
            "Delivered artifact by path"
        )
        return self._receipt(
            remote_id=uploaded.file_id,
            remote_unique_id=uploaded.file_unique_id,
            container_id=uploaded.chat_id,
            message_id=uploaded.message_id,
            size_bytes=uploaded.bytes_sent or size,
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
        reader: Any,
        thumbnail: Path | None,
        *,
        local_path: Path | None = None,
    ) -> UploadedMedia:
        """Perform the upload, translating any failure on the way out.

        One retry, and only when two things are both true: the file is handed
        over **by path** (so re-sending costs one small request and nothing has
        to be rewound), and the failure is one that provably happened before
        the destination took the request - a refused connection, an unreachable
        network, a failed name lookup, or an explicit "retry after". A timeout
        is deliberately *not* retried: the local server may already be pushing
        the file to Telegram, and a second send would deliver it twice.

        Without this, a network blip at the last step discarded a download that
        had taken minutes, because the lease is released on any failure.
        """
        try:
            return await self._send(request, chat, reader, thumbnail, local_path)
        except Exception as exc:
            # The detail deliberately omits the path, the token and the
            # destination's own text: a delivery error is shown to a person.
            error = classify(exc, detail=request.kind.value)
            if local_path is None or not is_safe_to_retry(error, exc):
                raise error from exc
            delay = min(error.retry_after_seconds or RETRY_DELAY_SECONDS, MAX_RETRY_DELAY_SECONDS)
            logger.bind(provider=PROVIDER, code=error.code, delay=delay).warning(
                "Delivery failed before the destination took the file; retrying once"
            )
            await asyncio.sleep(delay)
            try:
                return await self._send(request, chat, reader, thumbnail, local_path)
            except Exception as again:
                raise classify(again, detail=request.kind.value) from again

    async def _send(
        self,
        request: DeliveryRequest,
        chat: str,
        reader: Any,
        thumbnail: Path | None,
        local_path: Path | None,
    ) -> UploadedMedia:
        """One attempt at the upload."""
        return await self._uploader.send_media(
            chat_id=chat,
            content=reader,
            local_path=local_path,
            kind=request.kind.value,
            caption=_truncate(request.caption),
            filename=request.filename or request.artifact.name,
            duration_seconds=request.duration_seconds,
            width=request.width,
            height=request.height,
            thumbnail=thumbnail,
        )

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


def _digest_of(path: Path, *, chunk: int = 1024 * 1024) -> Fingerprint:
    """Return a file's checksum, read in chunks.

    Sequential and bounded: the point of delivering by path is that a large
    file never sits in memory, and a checksum that slurped the file would
    reintroduce exactly what was avoided.
    """
    digest = hashlib.new(HashAlgorithm.SHA256.value)
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return Fingerprint(algorithm=HashAlgorithm.SHA256, digest=digest.hexdigest())


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
