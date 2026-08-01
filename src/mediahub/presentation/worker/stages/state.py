"""What the pipeline has learned so far, in a form that survives a power cut.

The runtime already carries one opaque field between stages: a checkpoint's
:attr:`~mediahub.application.download.queue.Checkpoint.resume_token`, written by
a stage and handed back to the next one. That is the only durable channel the
stages have, and it is the right one - it is committed in the same write that
records the stage as complete, so state and progress can never disagree.

:class:`PipelineState` is what MediaHub puts in it: the probe's answer, the
artifact the engine produced, whether it verified, and - the field everything
else exists to protect - the delivery receipt.

Three properties are load-bearing.

* **It is JSON, and decoding never raises.** A token written by an older build,
  or truncated by a half-committed write, decodes to an empty state and the job
  starts the pipeline again. Losing an hour of download is bad; failing a job
  because a string would not parse is worse.
* **The artifact is recorded with the lease it lives in.** A lease belongs to
  one attempt, so an artifact from a previous attempt is *gone*, not stale, and
  :meth:`PipelineState.artifact_in` says so. That single field is what lets a
  resumed job tell "the bytes are here" from "the bytes were here once".
* **The receipt outlives the bytes.** Once it is set, delivery has happened and
  must never happen twice, whatever else a later attempt re-runs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from mediahub.application.download.dto import QualityOption
from mediahub.application.download.quality import selection_for
from mediahub.application.workspace.ports import ArtifactRef, ArtifactRole
from mediahub.domain.common.fingerprint import Fingerprint, InvalidFingerprintError
from mediahub.domain.media.enums import MediaType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from mediahub.application.delivery.ports import DeliveryReceipt
    from mediahub.application.download.ports import (
        DownloadResult,
        FormatSelection,
        MediaMetadata,
    )

DEFAULT_QUALITY_KEY: Final[str] = "best"
"""What a queued job asks for. A job carries no chosen rendition - nobody was
looking at a list of buttons when it was enqueued - so the pipeline takes the
best the deployment's ceilings allow."""

DEFAULT_QUALITY_LABEL: Final[str] = "Best available"


@dataclass(frozen=True, slots=True)
class ArtifactState:
    """One file the pipeline produced, as remembered between stages.

    Attributes:
        name: Its name inside the lease.
        size_bytes: Its size when it was written.
        role: What it is, as an :class:`~mediahub.application.workspace.ports.ArtifactRole`
            value.
        fingerprint: Its digest in ``algorithm:digest`` form, when one was taken
            from the stream as it was written.
    """

    name: str
    size_bytes: int
    role: str = ArtifactRole.PRIMARY.value
    fingerprint: str | None = None

    @classmethod
    def of(cls, reference: ArtifactRef) -> ArtifactState:
        """Return the state describing ``reference``."""
        return cls(
            name=reference.name,
            size_bytes=reference.size_bytes,
            role=reference.role.value,
            fingerprint=None if reference.fingerprint is None else str(reference.fingerprint),
        )

    @property
    def digest(self) -> Fingerprint | None:
        """Return the recorded digest, or ``None`` when there is not a valid one."""
        if self.fingerprint is None:
            return None
        try:
            return Fingerprint.parse(self.fingerprint)
        except InvalidFingerprintError:
            return None

    def reference_in(self, lease_id: str) -> ArtifactRef:
        """Return the handle this state describes, inside ``lease_id``."""
        return ArtifactRef(
            lease_id=lease_id,
            name=self.name,
            size_bytes=self.size_bytes,
            role=_role_of(self.role),
            fingerprint=self.digest,
        )


@dataclass(frozen=True, slots=True)
class ReceiptState:
    """Durable proof that the destination accepted the artifact.

    A projection of :class:`~mediahub.application.delivery.ports.DeliveryReceipt`
    rather than the receipt itself: what a resumed attempt needs is "this was
    delivered, here is the reference", not a transfer's timings.

    Attributes:
        provider: Which destination accepted it.
        principal: Whose credentials the reference belongs to.
        remote_id: The destination's handle on the stored bytes.
        size_bytes: How much was transferred.
        delivered_at: When the destination confirmed, in ISO-8601 (UTC).
        can_serve_back: Whether the bytes can be retrieved from there again.
        remote_unique_id: Stable identifier that survives credential changes.
        message_id: Where the artifact was announced, when it was.
    """

    provider: str
    principal: str
    remote_id: str
    size_bytes: int
    delivered_at: str
    can_serve_back: bool
    remote_unique_id: str | None = None
    message_id: str | None = None

    @classmethod
    def of(cls, receipt: DeliveryReceipt) -> ReceiptState:
        """Return the state describing ``receipt``."""
        return cls(
            provider=receipt.provider,
            principal=receipt.reference.principal,
            remote_id=receipt.provider_asset_id,
            size_bytes=receipt.size_bytes,
            delivered_at=receipt.delivered_at.isoformat(),
            can_serve_back=receipt.can_serve_back,
            remote_unique_id=receipt.reference.remote_unique_id,
            message_id=receipt.provider_message_id,
        )

    @property
    def confirmed_at(self) -> datetime | None:
        """Return when the destination confirmed, when the stamp is readable."""
        try:
            return datetime.fromisoformat(self.delivered_at)
        except ValueError:  # pragma: no cover - only a hand-edited token gets here
            return None


@dataclass(frozen=True, slots=True)
class PipelineState:
    """Everything one job has established, carried between its stages.

    Attributes:
        url: The source being acquired, as the probe canonicalised it.
        provider: Which platform it came from.
        title: What it is called.
        kind: Broad media category, as a
            :class:`~mediahub.domain.media.enums.MediaType` value.
        quality_key: Key of the rendition that was chosen.
        quality_label: What to call that rendition in a message.
        quality_height: Vertical resolution the choice caps at, when it caps one.
        quality_audio_only: Whether the choice yields audio without video.
        expected_bytes: Rough size the source declared. Always a hint.
        duration_seconds: Length, when the source declares one.
        width: Frame width of what was downloaded, when known.
        height: Frame height of what was downloaded, when known.
        lease_id: The lease :attr:`artifact` lives in. A lease belongs to one
            attempt, so this is what distinguishes "the bytes are here" from
            "the bytes were here during an attempt that has ended".
        artifact: The media itself.
        thumbnail: The poster image, when the engine produced one.
        verified: Whether :attr:`artifact` has been proved to be what was asked
            for, in :attr:`lease_id`.
        receipt: Proof of delivery. Once set, the transfer has happened.
        released: Whether the local copy has been handed back to the workspace.
    """

    url: str | None = None
    provider: str | None = None
    title: str | None = None
    kind: str = MediaType.OTHER.value
    quality_key: str = DEFAULT_QUALITY_KEY
    quality_label: str = DEFAULT_QUALITY_LABEL
    quality_height: int | None = None
    quality_audio_only: bool = False
    expected_bytes: int | None = None
    duration_seconds: int | None = None
    width: int | None = None
    height: int | None = None
    lease_id: str | None = None
    artifact: ArtifactState | None = None
    thumbnail: ArtifactState | None = None
    verified: bool = False
    receipt: ReceiptState | None = None
    released: bool = False

    # -- Reading -------------------------------------------------------------

    @property
    def is_probed(self) -> bool:
        """Return whether the source has been described."""
        return self.url is not None and self.provider is not None

    @property
    def media_kind(self) -> MediaType:
        """Return the media category, defaulting to ``OTHER`` for anything odd."""
        try:
            return MediaType(self.kind)
        except ValueError:  # pragma: no cover - only a hand-edited token gets here
            return MediaType.OTHER

    def quality(self) -> QualityOption:
        """Return the rendition choice this state was built with."""
        return QualityOption(
            key=self.quality_key,
            label=self.quality_label,
            height=self.quality_height,
            approx_bytes=self.expected_bytes,
            is_audio_only=self.quality_audio_only,
        )

    def selection(self) -> FormatSelection:
        """Return the engine-neutral selection the chosen quality means.

        Delegates to the application's own rule rather than restating it, so a
        resumed job asks for exactly what the first attempt asked for.
        """
        option = self.quality()
        return selection_for(option.key, (option,))

    def artifact_in(self, lease_id: str) -> ArtifactState | None:
        """Return the media artifact if it belongs to ``lease_id``, else ``None``.

        The check that makes every downstream stage honest: an artifact recorded
        against another lease is not stale metadata, it is a file that no longer
        exists.
        """
        if self.artifact is None or self.lease_id != lease_id:
            return None
        return self.artifact

    def is_verified_in(self, lease_id: str) -> bool:
        """Return whether a verified artifact is present in ``lease_id``."""
        return self.verified and self.artifact_in(lease_id) is not None

    # -- Writing -------------------------------------------------------------

    def with_probe(self, metadata: MediaMetadata, chosen: QualityOption) -> PipelineState:
        """Return this state carrying what the probe found and what was chosen."""
        return replace(
            self,
            url=metadata.url,
            provider=metadata.provider,
            title=metadata.title,
            kind=metadata.kind.value,
            quality_key=chosen.key,
            quality_label=chosen.label,
            quality_height=chosen.height,
            quality_audio_only=chosen.is_audio_only,
            expected_bytes=chosen.approx_bytes or metadata.expected_bytes,
            duration_seconds=_whole_seconds(metadata),
        )

    def with_download(self, result: DownloadResult, *, lease_id: str) -> PipelineState:
        """Return this state carrying the artifacts a download produced.

        Verification is reset, because these are different bytes from whatever
        was verified before.
        """
        thumbnail = next(
            (artifact for artifact in result.artifacts if artifact.role is ArtifactRole.THUMBNAIL),
            None,
        )
        return replace(
            self,
            url=result.url,
            provider=result.provider,
            lease_id=lease_id,
            artifact=ArtifactState.of(result.primary),
            thumbnail=None if thumbnail is None else ArtifactState.of(thumbnail),
            width=result.selected_format.width,
            height=result.selected_format.height,
            verified=False,
            released=False,
        )

    def with_verified(self, reference: ArtifactRef) -> PipelineState:
        """Return this state with the artifact recorded as proved."""
        return replace(self, artifact=ArtifactState.of(reference), verified=True)

    def with_receipt(self, receipt: DeliveryReceipt) -> PipelineState:
        """Return this state carrying durable proof of delivery."""
        return replace(self, receipt=ReceiptState.of(receipt))

    def released_locally(self) -> PipelineState:
        """Return this state with the local copy recorded as handed back."""
        return replace(self, released=True, verified=False, artifact=None, thumbnail=None)

    # -- Transport -----------------------------------------------------------

    def encode(self) -> str:
        """Return the compact JSON form stored in a checkpoint."""
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, token: str | None) -> PipelineState:
        """Return the state a resume token describes, or an empty one.

        Never raises. A token is untrusted input - it may have been written by
        an older build, or truncated - and a job that cannot start because a
        string would not parse is a worse outcome than a job that re-probes.
        """
        if not token:
            return cls()
        try:
            payload = json.loads(token)
        except ValueError:
            return cls()
        if not isinstance(payload, dict):
            return cls()
        return cls._from_mapping(payload)

    @classmethod
    def _from_mapping(cls, payload: Mapping[str, Any]) -> PipelineState:
        """Rebuild a state from a decoded token, ignoring anything unexpected."""
        return cls(
            url=_text(payload.get("url")),
            provider=_text(payload.get("provider")),
            title=_text(payload.get("title")),
            kind=_text(payload.get("kind")) or MediaType.OTHER.value,
            quality_key=_text(payload.get("quality_key")) or DEFAULT_QUALITY_KEY,
            quality_label=_text(payload.get("quality_label")) or DEFAULT_QUALITY_LABEL,
            quality_height=_whole(payload.get("quality_height")),
            quality_audio_only=bool(payload.get("quality_audio_only")),
            expected_bytes=_whole(payload.get("expected_bytes")),
            duration_seconds=_whole(payload.get("duration_seconds")),
            width=_whole(payload.get("width")),
            height=_whole(payload.get("height")),
            lease_id=_text(payload.get("lease_id")),
            artifact=_artifact(payload.get("artifact")),
            thumbnail=_artifact(payload.get("thumbnail")),
            verified=bool(payload.get("verified")),
            receipt=_receipt(payload.get("receipt")),
            released=bool(payload.get("released")),
        )


def _role_of(value: str) -> ArtifactRole:
    """Return the role a recorded value names, defaulting to the media itself."""
    try:
        return ArtifactRole(value)
    except ValueError:  # pragma: no cover - only a hand-edited token gets here
        return ArtifactRole.PRIMARY


def _artifact(value: Any) -> ArtifactState | None:
    """Rebuild an artifact record from a decoded token, or return ``None``."""
    if not isinstance(value, dict):
        return None
    name = _text(value.get("name"))
    size = _whole(value.get("size_bytes"))
    if name is None or size is None:
        return None
    return ArtifactState(
        name=name,
        size_bytes=size,
        role=_text(value.get("role")) or ArtifactRole.PRIMARY.value,
        fingerprint=_text(value.get("fingerprint")),
    )


def _receipt(value: Any) -> ReceiptState | None:
    """Rebuild a receipt record from a decoded token, or return ``None``.

    A receipt without a remote reference is not a receipt: treating one as
    proof of delivery would let a resumed job skip the transfer entirely.
    """
    if not isinstance(value, dict):
        return None
    remote_id = _text(value.get("remote_id"))
    if not remote_id:
        return None
    return ReceiptState(
        provider=_text(value.get("provider")) or "unknown",
        principal=_text(value.get("principal")) or "unknown",
        remote_id=remote_id,
        size_bytes=_whole(value.get("size_bytes")) or 0,
        delivered_at=_text(value.get("delivered_at")) or "",
        can_serve_back=bool(value.get("can_serve_back")),
        remote_unique_id=_text(value.get("remote_unique_id")),
        message_id=_text(value.get("message_id")),
    )


def _text(value: Any) -> str | None:
    """Return ``value`` when it is a string, and ``None`` otherwise."""
    return value if isinstance(value, str) else None


def _whole(value: Any) -> int | None:
    """Return ``value`` when it is a whole number, and ``None`` otherwise."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _whole_seconds(metadata: MediaMetadata) -> int | None:
    """Return a whole-second duration, when the source declares one."""
    seconds = metadata.duration_seconds
    return None if seconds is None else int(seconds)
