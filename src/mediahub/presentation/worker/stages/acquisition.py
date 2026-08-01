"""The five steps of an acquisition, each one idempotent.

Every stage handler in this package is three lines long, and this is why: the
work itself lives here, as one method per step, and each method **ensures** its
outcome rather than performing it. Called with the outcome already in the state,
it returns immediately; called without it, it does the work and records it.

That shape gives three things that a straight sequence of actions cannot.

**Re-running a stage is free.** A job reclaimed mid-stage runs that stage again,
which the runtime requires (``stages.base``) and which is normal rather than
exceptional. A step that has already happened costs a dictionary lookup.

**Resuming is possible from any point.** Each step ensures the step before it,
so a stage that is reached with its inputs missing rebuilds them instead of
failing. That is what makes a checkpoint useful after a power cut: the workspace
lease belongs to *one attempt*, so a job that resumes at `verify` finds an empty
lease, and `verify` quietly downloads again rather than reporting corruption
that never happened.

**There is still exactly one pipeline.** The order stages run in is stated once,
in ``DEFAULT_STAGE_PLAN``, and executed once, by the executor. What is here is
each step's *precondition*, not a second copy of the sequence - and the
difference shows up the moment the plan changes: a shorter plan runs fewer
stages, and these methods do not care.

Two orderings must not be "optimised away":

* the receipt is taken **before** anything local is deleted, because deleting
  first risks losing both copies (``docs/architecture/07-download-pipeline.md``
  §7.5);
* a delivery that has succeeded is never repeated, whatever else a later attempt
  re-runs, because the user has already been sent the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from loguru import logger

from mediahub.application.delivery.errors import ArtifactTooLargeError
from mediahub.application.delivery.ports import DeliveryKind, DeliveryRequest
from mediahub.application.download.dto import GetDownloadJobQuery, QualityOption
from mediahub.application.download.errors import (
    DownloadCancelledError,
    InvalidDownloadResultError,
    LocalCopyNotReleasableError,
)
from mediahub.application.download.journal import JournalEntry
from mediahub.application.download.ports import DownloadRequest
from mediahub.application.download.quality import build_quality_options
from mediahub.application.workspace.ports import ArtifactRef, ArtifactRole
from mediahub.domain.media.enums import MediaType
from mediahub.domain.workspace.value_objects import IntegrityExpectation
from mediahub.presentation.worker.stages.state import (
    DEFAULT_QUALITY_KEY,
    DEFAULT_QUALITY_LABEL,
    PipelineState,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.delivery.ports import (
        DeliveryProgress,
        DeliveryRouter,
        DeliveryTarget,
    )
    from mediahub.application.download.journal import AcquisitionJournal
    from mediahub.application.download.ports import DownloaderPort, DownloadProgress
    from mediahub.application.download.use_cases.get_download_job import GetDownloadJob
    from mediahub.application.workspace.ports import WorkspaceScope
    from mediahub.presentation.worker.stages.base import StageContext

_KIND_MAP: Final[dict[MediaType, DeliveryKind]] = {
    MediaType.VIDEO: DeliveryKind.VIDEO,
    MediaType.AUDIO: DeliveryKind.AUDIO,
    MediaType.IMAGE: DeliveryKind.PHOTO,
}
"""How a destination should present each category. A translation table, not a
rule: a destination that cannot honour the hint falls back and says so."""

_CANCELLED_MESSAGE: Final[str] = "the job was asked to stop"

_NO_ARTIFACT_MESSAGE: Final[str] = (
    "the download step reported success but left no artifact in the lease"
)

_EMPTY_ARTIFACT_MESSAGE: Final[str] = "the download step reported success but the artifact is empty"


@dataclass(frozen=True, slots=True)
class AcquisitionPolicy:
    """The deployment decisions an acquisition needs, gathered in one value.

    Attributes:
        target: Where a queued job's result is sent. A ``DownloadJob`` carries
            no destination - nobody named one when it was enqueued - so the
            worker is configured with this deployment's destination and the
            router decides which provider owns it.
        principal: Whose history the acquisition is recorded under.
        max_item_bytes: This deployment's ceiling, or ``None`` for none. The
            destination's ceiling is applied as well, and the smaller wins.
        include_thumbnail: Fetch the poster image alongside the media.
        probe_timeout_seconds: Budget for the metadata probe.
        download_timeout_seconds: Wall-clock budget for the transfer.
        socket_timeout_seconds: Per-connection read timeout, which is what turns
            a stalled source into a failure instead of a hung worker.
    """

    target: DeliveryTarget
    principal: str = "worker"
    max_item_bytes: int | None = None
    include_thumbnail: bool = True
    probe_timeout_seconds: float | None = None
    download_timeout_seconds: float | None = None
    socket_timeout_seconds: float | None = None


class AcquisitionSteps:
    """Performs, idempotently, each step a stage of the pipeline stands for."""

    __slots__ = ("_delivery", "_downloader", "_jobs", "_journal", "_policy")

    def __init__(
        self,
        *,
        downloader: DownloaderPort,
        delivery: DeliveryRouter,
        journal: AcquisitionJournal,
        jobs: GetDownloadJob,
        policy: AcquisitionPolicy,
    ) -> None:
        """Wire the steps to their ports.

        ``delivery`` is a *router*, not a provider: the steps name a target and
        let the router decide which destination owns it, which is what allows a
        destination to be added without this file changing.
        """
        self._downloader = downloader
        self._delivery = delivery
        self._journal = journal
        self._jobs = jobs
        self._policy = policy

    # -- Probe ---------------------------------------------------------------

    async def ensure_probed(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Ensure the source has been described and a rendition chosen.

        Raises:
            DownloadCancelledError: If the job was asked to stop.
            DownloadError: If the source could not be described.
        """
        if state.is_probed:
            return state
        _stop_if_cancelled(context.cancellation)

        url = await self._url_of(context)
        metadata = await self._downloader.probe(
            url, timeout_seconds=self._policy.probe_timeout_seconds
        )
        chosen = _choose_quality(build_quality_options(metadata, max_bytes=self._ceiling()))
        logger.bind(
            job_id=str(context.job_id),
            provider=metadata.provider,
            quality=chosen.label,
            expected_bytes=metadata.expected_bytes,
        ).info("Probed source")
        return state.with_probe(metadata, chosen)

    # -- Download ------------------------------------------------------------

    async def ensure_downloaded(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Ensure the media is present in this attempt's lease.

        Resumption is requested on every fetch: a partial file left in the lease
        by an interrupted attempt is continued rather than started again, which
        on a domestic connection is the difference between a usable product and
        an unusable one.

        Raises:
            DownloadCancelledError: If the job was asked to stop.
            DownloadError: If the transfer failed.
        """
        if self._artifact_present(context.workspace, state) is not None:
            return state
        state = await self.ensure_probed(context, state)
        _stop_if_cancelled(context.cancellation)

        url = state.url or await self._url_of(context)
        result = await self._downloader.fetch(
            DownloadRequest(
                url=url,
                selection=state.selection(),
                max_bytes=self._ceiling(),
                timeout_seconds=self._policy.download_timeout_seconds,
                socket_timeout_seconds=self._policy.socket_timeout_seconds,
                include_thumbnail=self._policy.include_thumbnail,
                resume=True,
            ),
            context.workspace,
            on_progress=_download_reporter(context),
            cancellation=context.cancellation,
        )
        logger.bind(
            job_id=str(context.job_id),
            bytes=result.total_bytes,
            resumed=result.resumed,
            artifact=result.primary.name,
        ).info("Downloaded source into the lease")
        return state.with_download(result, lease_id=context.workspace.lease_id)

    # -- Verify --------------------------------------------------------------

    async def ensure_verified(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Ensure the bytes in the lease are the bytes that were asked for.

        Three checks, all performed by the workspace rather than here: the lease
        still holds nothing but regular files, the artifact is the size it was
        written at, and its digest matches the one taken from the stream when
        the engine produced one. The digest is computed either way, so what is
        delivered is always something whose hash is known.

        Raises:
            DownloadCancelledError: If the job was asked to stop.
            IntegrityCheckFailedError: If size or digest disagree.
        """
        if state.is_verified_in(context.workspace.lease_id):
            return state
        state = await self.ensure_downloaded(context, state)
        _stop_if_cancelled(context.cancellation)

        scope = context.workspace
        artifact = state.artifact_in(scope.lease_id)
        if artifact is None:  # pragma: no cover - ensure_downloaded guarantees one
            raise InvalidDownloadResultError(_NO_ARTIFACT_MESSAGE)

        scope.verify_consistency()
        digest = scope.fingerprint_of(artifact.name)
        reference = scope.verify(
            artifact.name,
            expect=IntegrityExpectation(
                expected_bytes=artifact.size_bytes,
                expected_fingerprint=artifact.digest,
            ),
        )
        if reference.size_bytes <= 0:
            # Checked here as well as in the engine adapter, because this is the
            # stage that *is* the guarantee. An engine that reports success over
            # an empty file is a bug in that engine, and the cost of trusting it
            # is a zero-byte file delivered to a person as their download - at
            # which point the receipt says it worked and nothing will retry.
            raise InvalidDownloadResultError(_EMPTY_ARTIFACT_MESSAGE)
        context.observe(transferred_bytes=reference.size_bytes, total_bytes=reference.size_bytes)
        logger.bind(
            job_id=str(context.job_id), bytes=reference.size_bytes, digest=str(digest)
        ).info("Verified the downloaded artifact")
        return state.with_verified(
            ArtifactRef(
                lease_id=reference.lease_id,
                name=reference.name,
                size_bytes=reference.size_bytes,
                role=ArtifactRole.PRIMARY,
                fingerprint=digest,
            )
        )

    # -- Deliver -------------------------------------------------------------

    async def ensure_delivered(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Ensure the destination holds the artifact, and remember that it does.

        A delivery that has already succeeded is never repeated: the receipt in
        the state is checked first, so a job reclaimed between the transfer and
        its checkpoint sends nothing a second time.

        Raises:
            DownloadCancelledError: If the job was asked to stop *before* the
                transfer starts. Once bytes are moving the job runs to
                completion: telling someone their delivery was cancelled after
                it arrived would be a lie
                (``docs/architecture/07-download-pipeline.md`` §7.6).
            ArtifactTooLargeError: If the destination cannot accept it.
            DeliveryError: If the destination refused it.
        """
        if state.receipt is not None:
            return state
        state = await self.ensure_verified(context, state)
        _stop_if_cancelled(context.cancellation)

        target = self._policy.target
        artifact = self._require_artifact(context, state)
        capabilities = self._delivery.capabilities_for(target)
        if not capabilities.accepts(artifact.size_bytes):
            # Checked here as well as in the provider so the refusal names the
            # real ceiling instead of arriving as a transport error.
            raise ArtifactTooLargeError(
                capabilities.maximum_file_size,
                artifact.size_bytes,
                provider=capabilities.provider,
            )

        receipt = await self._delivery.deliver(
            DeliveryRequest(
                target=target,
                artifact=artifact,
                kind=_kind_for(state),
                caption=state.title,
                filename=artifact.name,
                duration_seconds=state.duration_seconds,
                width=state.width,
                height=state.height,
                thumbnail=self._thumbnail_of(context.workspace, state),
            ),
            context.workspace,
            on_progress=_delivery_reporter(context),
        )
        await self._journal.record(
            JournalEntry(
                principal=self._policy.principal,
                url=state.url or "",
                provider=state.provider or receipt.provider,
                title=state.title or "",
                quality_label=state.quality_label,
                bytes_delivered=receipt.size_bytes,
                remote_id=receipt.provider_asset_id,
                remote_unique_id=receipt.reference.remote_unique_id,
                message_id=receipt.provider_message_id,
                delivered_at=receipt.delivered_at,
            )
        )
        logger.bind(
            job_id=str(context.job_id),
            destination=receipt.provider,
            bytes=receipt.size_bytes,
            custodian=receipt.can_serve_back,
        ).info("Delivered the artifact and recorded the receipt")
        return state.with_receipt(receipt)

    # -- Cleanup -------------------------------------------------------------

    async def ensure_released(self, context: StageContext, state: PipelineState) -> PipelineState:
        """Ensure the local copy is gone, and only once it is safe for it to be.

        The lease is deleted by the claim loop whatever happens, so this step is
        not what stops bytes leaking - it is what makes the release *ordered*:
        nothing local is removed until a receipt proves the destination has it.

        Raises:
            LocalCopyNotReleasableError: If there is no receipt. Refusing costs
                a lease that a sweep reclaims; deleting would risk losing both
                copies.
        """
        if state.released:
            return state
        if state.receipt is None:
            raise LocalCopyNotReleasableError(context.job_id)

        scope = context.workspace
        for name in tuple(scope.names()):
            scope.remove(name)
        logger.bind(job_id=str(context.job_id), lease_id=scope.lease_id).info(
            "Released the local copy; the destination is the custodian"
        )
        return state.released_locally()

    # -- Internals -----------------------------------------------------------

    async def _url_of(self, context: StageContext) -> str:
        """Return the source this job was created for.

        Read through the use case rather than carried on the queue: the runtime
        holds scheduling mechanics only, and a payload duplicated into it would
        be a second, staler copy of the job
        (``docs/architecture/09-queue-architecture.md`` §9.3).
        """
        summary = await self._jobs.execute(GetDownloadJobQuery(job_id=context.job_id.value))
        return summary.source_url

    def _ceiling(self) -> int:
        """Return the smaller of this deployment's and the destination's limit.

        Downloading something the destination will certainly refuse wastes an
        hour and a lease, so the delivery ceiling is applied to the download.
        """
        limit = self._delivery.capabilities_for(self._policy.target).maximum_file_size
        if self._policy.max_item_bytes is None:
            return limit
        return min(self._policy.max_item_bytes, limit)

    @staticmethod
    def _artifact_present(scope: WorkspaceScope, state: PipelineState) -> str | None:
        """Return the artifact's name when this lease really still holds it.

        Both halves matter. The recorded lease answers "was this written by the
        attempt that is running now?"; the listing answers "is it still there?".
        """
        artifact = state.artifact_in(scope.lease_id)
        if artifact is None or artifact.name not in scope.names():
            return None
        return artifact.name

    @staticmethod
    def _require_artifact(context: StageContext, state: PipelineState) -> ArtifactRef:
        """Return the handle on the media, which ``ensure_verified`` guarantees."""
        artifact = state.artifact_in(context.workspace.lease_id)
        if artifact is None:  # pragma: no cover - ensure_verified guarantees one
            raise InvalidDownloadResultError(_NO_ARTIFACT_MESSAGE)
        return artifact.reference_in(context.workspace.lease_id)

    @staticmethod
    def _thumbnail_of(scope: WorkspaceScope, state: PipelineState) -> ArtifactRef | None:
        """Return the poster image, when this lease actually holds one."""
        thumbnail = state.thumbnail
        if thumbnail is None or state.lease_id != scope.lease_id:
            return None
        if thumbnail.name not in scope.names():
            return None
        return thumbnail.reference_in(scope.lease_id)


def _choose_quality(options: Sequence[QualityOption]) -> QualityOption:
    """Return the rendition a queued job should be acquired at.

    A job carries no choice, so the best the ceilings allow is taken. A source
    that offers nothing recognisable still gets a default rather than a refusal:
    the engine's own selection is a better answer than failing the job before
    anything has been attempted.
    """
    for option in options:
        if option.key == DEFAULT_QUALITY_KEY:
            return option
    if options:
        return options[0]
    return QualityOption(key=DEFAULT_QUALITY_KEY, label=DEFAULT_QUALITY_LABEL)


def _kind_for(state: PipelineState) -> DeliveryKind:
    """Return how the destination should present this acquisition."""
    if state.quality_audio_only:
        return DeliveryKind.AUDIO
    return _KIND_MAP.get(state.media_kind, DeliveryKind.DOCUMENT)


def _download_reporter(context: StageContext) -> Callable[[DownloadProgress], None]:
    """Return a sink that forwards engine progress to the running stage."""

    def report(update: DownloadProgress) -> None:
        context.observe(
            transferred_bytes=max(0, update.downloaded_bytes),
            total_bytes=None if update.total_bytes is None else max(0, update.total_bytes),
            speed_bps=_positive(update.speed_bps),
            eta_seconds=_positive(update.eta_seconds),
        )

    return report


def _delivery_reporter(context: StageContext) -> Callable[[DeliveryProgress], None]:
    """Return a sink that forwards upload progress to the running stage."""

    def report(update: DeliveryProgress) -> None:
        context.observe(
            transferred_bytes=max(0, update.sent_bytes),
            total_bytes=None if update.total_bytes is None else max(0, update.total_bytes),
        )

    return report


def _positive(value: float | None) -> float | None:
    """Return a rate or estimate a progress observation will accept.

    Engines occasionally report a negative estimate while they work one out, and
    a telemetry field must never be the reason a download fails.
    """
    return None if value is None else max(0.0, value)


def _stop_if_cancelled(cancellation: CancellationToken) -> None:
    """Raise if the job has been asked to stop.

    Raises:
        DownloadCancelledError: If cancellation has been requested. Typed, so
            the executor classifies it as a cancellation rather than a failure
            and the job is not charged an attempt.
    """
    if cancellation.cancelled:
        raise DownloadCancelledError(_CANCELLED_MESSAGE)
