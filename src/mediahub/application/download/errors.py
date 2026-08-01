"""Typed failures of the acquisition context.

Two families live here, both belonging to the same context and therefore to the
same module:

* **The engine taxonomy** - what a download engine can report. Every error
  carries a :class:`~mediahub.domain.download.enums.FailureKind`, so a caller
  decides whether to retry by reading a field rather than by matching on message
  text. Classification is the adapter's job - only the adapter can read a
  provider's error and know what it means.
* **Queue and lease failures** - what a worker can meet while executing a job.
  They are separated at the end of this module.

**URL validation errors are deliberately absent from this module.** They belong
to :mod:`mediahub.domain.sources.errors` (``InvalidUrlError``,
``UnsupportedSchemeError``, ``BlockedAddressError``), because "which URLs may
this system fetch" is a rule that applies to every interface, not just to this
engine. Redefining them here would duplicate the rule and let the two copies
drift.
"""

from __future__ import annotations

from typing import ClassVar

from mediahub.application.common.errors import ApplicationError, FeatureNotAvailableError
from mediahub.domain.download.enums import FailureKind


class DownloadError(ApplicationError):
    """Base class for every failure the download engine can report.

    Attributes:
        kind: How the caller should treat this failure.
        provider: The extractor or platform involved, when known.
        retry_after_seconds: Delay the provider explicitly asked for. When
            present it is authoritative - guessing at backoff when the other
            side has told you the answer is self-inflicted damage.
    """

    code: ClassVar[str] = "download_error"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        """Initialise the error with its message and provider context."""
        super().__init__(message)
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_retryable(self) -> bool:
        """Return whether this failure may be attempted again."""
        return self.kind.is_retryable

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and test failures."""
        return (
            f"{type(self).__name__}(code={self.code!r}, kind={self.kind.value!r}, "
            f"message={self.message!r})"
        )


class UnsupportedProviderError(DownloadError):
    """No configured engine or extractor claims this URL.

    Permanent: retrying will not make an extractor appear. A site that gains
    support in a later engine release becomes a *new* request, not a retry.
    """

    code: ClassVar[str] = "unsupported_provider"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class MetadataUnavailableError(DownloadError):
    """The source could not be described.

    Permanent by default: private, deleted, geo-blocked and paywalled items all
    land here, and none of them improve with another attempt. A provider that
    failed *transiently* while probing raises :class:`ProviderError` instead.
    """

    code: ClassVar[str] = "metadata_unavailable"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class FormatUnavailableError(DownloadError):
    """The requested rendition does not exist for this source.

    Permanent: a stale ``format_id`` from an old probe, or a quality the source
    never published.
    """

    code: ClassVar[str] = "format_unavailable"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class ProviderError(DownloadError):
    """The provider failed in a way that may resolve on its own.

    Rate limits, upstream 5xx, connection resets. This is normal traffic for a
    media fetcher, not an incident.
    """

    code: ClassVar[str] = "provider_error"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT


class DownloadFailedError(DownloadError):
    """The transfer did not complete, for a reason with no better class.

    Transient by default and deliberately so: an unfamiliar failure gets one
    honest retry rather than being written off, and then becomes a dead letter a
    human can read.
    """

    code: ClassVar[str] = "download_failed"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT


class DownloadTimeoutError(DownloadError):
    """The operation exceeded its wall-clock budget.

    Transient: a slow source at midnight may be fine at noon.
    """

    code: ClassVar[str] = "download_timeout"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT


class DownloadCancelledError(DownloadError):
    """The operation stopped because cancellation was requested.

    Not a failure. It is in this hierarchy so that one ``except`` clause covers
    every way an engine call can end without a result.
    """

    code: ClassVar[str] = "download_cancelled"
    kind: ClassVar[FailureKind] = FailureKind.CANCELLED


class SizeLimitExceededError(DownloadError):
    """The transfer grew past the ceiling the caller set.

    A policy refusal rather than a failure: the engine did its job and the
    content is simply larger than this deployment accepts.

    Attributes:
        limit_bytes: The ceiling that was breached.
        observed_bytes: How much had been transferred when it was noticed.
    """

    code: ClassVar[str] = "size_limit_exceeded"
    kind: ClassVar[FailureKind] = FailureKind.POLICY

    def __init__(
        self,
        limit_bytes: int,
        observed_bytes: int,
        *,
        provider: str | None = None,
    ) -> None:
        """Initialise the error from the ceiling and the observed size."""
        super().__init__(
            f"Transfer exceeded the {limit_bytes} byte ceiling "
            f"(observed {observed_bytes} bytes).",
            provider=provider,
        )
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes


class LiveSourceNotAllowedError(DownloadError):
    """The source is a live stream and live capture was not requested.

    A live stream has no end; discovering that through a byte ceiling wastes an
    hour and a lease.
    """

    code: ClassVar[str] = "live_source_not_allowed"
    kind: ClassVar[FailureKind] = FailureKind.POLICY


class PlaylistNotAllowedError(DownloadError):
    """The URL denotes a collection and collection downloads were not requested.

    Refused by default so that one pasted link cannot enqueue five hundred
    files on a device with a 32 GB card.

    Attributes:
        entry_count: How many entries the collection advertises, when known.
    """

    code: ClassVar[str] = "playlist_not_allowed"
    kind: ClassVar[FailureKind] = FailureKind.POLICY

    def __init__(
        self,
        message: str,
        *,
        entry_count: int | None = None,
        provider: str | None = None,
    ) -> None:
        """Initialise the error, recording how large the collection is."""
        super().__init__(message, provider=provider)
        self.entry_count = entry_count


class InvalidFormatSelectionError(DownloadError):
    """A caller asked for a combination of options that contradicts itself.

    A programming error rather than an environmental one, so it is never
    retried.
    """

    code: ClassVar[str] = "invalid_format_selection"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class InvalidDownloadResultError(DownloadError):
    """An engine produced a result that violates the contract.

    Raised when a result cannot be trusted - for example when it contains no
    primary artifact. It indicates a defect in an adapter, and the caller must
    not treat the download as successful.
    """

    code: ClassVar[str] = "invalid_download_result"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class DownloaderNotConfiguredError(DownloadError, FeatureNotAvailableError):
    """No download engine is wired into this build.

    Raised by
    :class:`~mediahub.infrastructure.downloader.null_downloader.NullDownloader`,
    the placeholder adapter installed when the engine is disabled. It is a
    configuration state, not a bug: the API answers ``501 Not Implemented``.

    It inherits from both hierarchies deliberately - it is a download failure to
    the engine's callers, and an unavailable feature to the HTTP layer, which
    already maps ``FeatureNotAvailableError`` to ``501``.
    """

    code: ClassVar[str] = "downloader_not_configured"
    kind: ClassVar[FailureKind] = FailureKind.POLICY

    def __init__(self, source_url: str | None = None) -> None:
        """Initialise the error, naming the URL that could not be fetched."""
        target = f" for '{source_url}'" if source_url else ""
        super().__init__(
            f"No download engine is configured{target}; " f"jobs can be queued but not executed."
        )
        self.source_url = source_url


# --------------------------------------------------------------------------- #
# Queue and lease failures                                                     #
# --------------------------------------------------------------------------- #


class InvalidWorkerIdentityError(ApplicationError):
    """A worker identity could not be told apart from another one.

    Worker identity is stable by design, so that a restarted process can release
    its own stale leases. A blank or ambiguous identity would silently break
    that, which is why it is refused at construction.
    """

    code: ClassVar[str] = "invalid_worker_identity"


class InvalidLeaseError(ApplicationError):
    """A lease was described in a way that cannot be honoured.

    A lease that expires before it was acquired, or an extension that moves the
    expiry backwards, would make crash detection meaningless.
    """

    code: ClassVar[str] = "invalid_lease"


class InvalidCheckpointError(ApplicationError):
    """A checkpoint claims to have completed the same stage twice.

    Resuming is set arithmetic over completed stages; duplicates would make
    "what is left to run" ambiguous.
    """

    code: ClassVar[str] = "invalid_checkpoint"


class LeaseLostError(DownloadError):
    """The worker no longer owns the job it was writing to.

    Either the lease expired and another worker claimed the job, or it was
    reclaimed administratively. Transient by classification, but the correct
    response is **to stop touching the job entirely** - whoever owns it now is
    running it, and a second writer is how one job becomes two.
    """

    code: ClassVar[str] = "lease_lost"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT

    def __init__(self, job_id: object, owner: object | None = None) -> None:
        """Initialise the error from the job and the worker that lost it."""
        holder = f" (held by '{owner}')" if owner is not None else ""
        super().__init__(f"The lease on job '{job_id}' is no longer held{holder}.")
        self.job_id = job_id
        self.owner = owner


class StageHandlerMissingError(DownloadError):
    """A stage in the plan has nothing wired to execute it.

    A configuration defect rather than an environmental one, so it is never
    retried: the next attempt would find the same empty slot.
    """

    code: ClassVar[str] = "stage_handler_missing"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT

    def __init__(self, stage: object) -> None:
        """Initialise the error from the stage that has no handler."""
        super().__init__(f"No handler is registered for the '{stage}' stage.")
        self.stage = stage


class LocalCopyNotReleasableError(DownloadError):
    """Something asked for the local copy to be deleted before it was delivered.

    The product's central rule, expressed as a type: **a receipt authorises
    deletion, and nothing else does** (``docs/architecture/07-download-pipeline.md``
    §7.5). Deleting first risks losing both copies; refusing costs a lease that
    a sweep will reclaim anyway.

    Permanent by classification: the state that produced it will not improve by
    being asked again.
    """

    code: ClassVar[str] = "local_copy_not_releasable"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT

    def __init__(self, job_id: object) -> None:
        """Initialise the error from the job whose copy was nearly deleted."""
        super().__init__(
            f"Job '{job_id}' has no delivery receipt; its local copy must not be released."
        )
        self.job_id = job_id


class WorkerNotReadyError(ApplicationError):
    """A worker was asked to run before it could safely do so.

    Startup validates its preconditions - a stage plan with a handler for every
    stage, a writable workspace - and refuses rather than claiming work it
    cannot finish (``docs/architecture/10-worker-architecture.md`` §10.5).
    """

    code: ClassVar[str] = "worker_not_ready"
