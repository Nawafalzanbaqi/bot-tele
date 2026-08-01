"""Typed failures a delivery provider can report.

Every error carries a :class:`~mediahub.domain.download.enums.FailureKind`, so
a caller decides whether to retry by reading a field rather than by matching on
message text. **Providers classify; they never retry** - deciding to try again
is the caller's policy, and a provider that retried internally would silently
multiply whatever budget the caller had set.

The taxonomy in one line each:

===========================  ==========  ==================================
Error                        Kind        Means
===========================  ==========  ==================================
``DeliveryRateLimitedError``  transient  Slow down; ``retry_after`` says how long
``ProviderUnavailableError``  transient  The destination is having a bad day
``DeliveryProviderError``     transient  Something unfamiliar went wrong
``DeliveryQuotaExceededError`` policy    An account limit was reached
``ArtifactTooLargeError``     policy     The file is bigger than the ceiling
``ResendNotSupportedError``   policy     This destination cannot re-send
``TargetUnreachableError``    permanent  The destination refuses this sender
``DeliveryAuthenticationError`` permanent Our credentials were rejected
``ReferenceNotUsableError``   permanent  That reference is not ours, or expired
``NoProviderForTargetError``  permanent  Nothing claims that destination
===========================  ==========  ==================================
"""

from __future__ import annotations

from typing import ClassVar

from mediahub.application.common.errors import ApplicationError, FeatureNotAvailableError
from mediahub.domain.download.enums import FailureKind


class DeliveryError(ApplicationError):
    """Base class for every failure a delivery provider can report.

    Attributes:
        kind: How the caller should treat this failure.
        provider: Which destination was involved.
        retry_after_seconds: Delay the destination explicitly asked for. When
            present it is authoritative - guessing at backoff when the other
            side has stated the answer is self-inflicted damage.
    """

    code: ClassVar[str] = "delivery_error"
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
        """Return whether this delivery may be attempted again."""
        return self.kind.is_retryable

    def __repr__(self) -> str:
        """Return an unambiguous representation for logs and test failures."""
        return (
            f"{type(self).__name__}(code={self.code!r}, kind={self.kind.value!r}, "
            f"provider={self.provider!r})"
        )


# -- Transient ------------------------------------------------------------- #


class DeliveryProviderError(DeliveryError):
    """The destination failed in a way with no better classification.

    Transient by default and deliberately so: an unfamiliar failure gets one
    honest attempt later rather than being written off.
    """

    code: ClassVar[str] = "delivery_provider_error"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT


class DeliveryRateLimitedError(DeliveryError):
    """The destination asked us to slow down.

    Normal traffic for a delivery adapter, not an incident. When the
    destination states a delay, it is carried on the error and is
    authoritative.
    """

    code: ClassVar[str] = "delivery_rate_limited"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT


class ProviderUnavailableError(DeliveryError):
    """The destination is unreachable or returning server errors.

    Distinct from :class:`TargetUnreachableError`: the *destination* is down,
    not the target invalid.
    """

    code: ClassVar[str] = "provider_unavailable"
    kind: ClassVar[FailureKind] = FailureKind.TRANSIENT


# -- Policy ---------------------------------------------------------------- #


class DeliveryQuotaExceededError(DeliveryError):
    """An account or plan limit was reached.

    A policy refusal: nothing failed, the account simply has no room. Retrying
    within the same window would only burn the remaining budget.
    """

    code: ClassVar[str] = "delivery_quota_exceeded"
    kind: ClassVar[FailureKind] = FailureKind.POLICY


class ArtifactTooLargeError(DeliveryError):
    """The artifact exceeds what this destination accepts.

    A policy refusal rather than a failure: nothing went wrong, the file is
    simply larger than the ceiling. Making it smaller is a different
    subsystem's job.

    Attributes:
        limit_bytes: The destination's ceiling.
        actual_bytes: The artifact's size.
    """

    code: ClassVar[str] = "artifact_too_large"
    kind: ClassVar[FailureKind] = FailureKind.POLICY

    def __init__(self, limit_bytes: int, actual_bytes: int, *, provider: str | None = None) -> None:
        """Initialise the error from the ceiling and the actual size."""
        super().__init__(
            f"The artifact is {actual_bytes} bytes; this destination accepts "
            f"at most {limit_bytes}.",
            provider=provider,
        )
        self.limit_bytes = limit_bytes
        self.actual_bytes = actual_bytes


class ResendNotSupportedError(DeliveryError):
    """This destination cannot re-deliver from a reference.

    Not every destination keeps the bytes, and not every one that does will
    hand them out again.
    """

    code: ClassVar[str] = "resend_not_supported"
    kind: ClassVar[FailureKind] = FailureKind.POLICY


# -- Permanent ------------------------------------------------------------- #


class TargetUnreachableError(DeliveryError):
    """The destination does not exist, or refuses this sender.

    Permanent: a blocked account or a deleted conversation does not recover by
    retrying.
    """

    code: ClassVar[str] = "delivery_target_unreachable"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class DeliveryAuthenticationError(DeliveryError):
    """The destination rejected our credentials.

    Permanent, and worth shouting about: every reference this account ever
    issued is now suspect, so an operator needs to know immediately rather than
    discovering it one failed re-send at a time.
    """

    code: ClassVar[str] = "delivery_authentication_failed"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class ReferenceNotUsableError(DeliveryError):
    """A remote reference cannot be used for this re-send.

    Either it belongs to different credentials or it has expired. Permanent for
    *this* reference; the caller's correct response is to upload again, not to
    retry.
    """

    code: ClassVar[str] = "reference_not_usable"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT


class NoProviderForTargetError(DeliveryError):
    """No enabled provider claims this destination.

    A configuration problem, not a transport one: the target names a provider
    that is not installed, not enabled, or spelled differently.

    Attributes:
        target_provider: The provider the target asked for.
    """

    code: ClassVar[str] = "no_provider_for_target"
    kind: ClassVar[FailureKind] = FailureKind.PERMANENT

    def __init__(self, target_provider: str) -> None:
        """Initialise the error from the provider the target named."""
        super().__init__(
            f"No enabled delivery provider handles '{target_provider}'.",
            provider=target_provider,
        )
        self.target_provider = target_provider


class DeliveryNotConfiguredError(DeliveryError, FeatureNotAvailableError):
    """No delivery provider is wired into this build at all.

    A configuration state rather than a bug, and mapped to ``501`` by the HTTP
    layer through :class:`FeatureNotAvailableError`.
    """

    code: ClassVar[str] = "delivery_not_configured"
    kind: ClassVar[FailureKind] = FailureKind.POLICY

    def __init__(self, target: str | None = None) -> None:
        """Initialise the error, naming the destination that was unreachable."""
        where = f" for '{target}'" if target else ""
        super().__init__(f"No delivery provider is configured{where}.")
