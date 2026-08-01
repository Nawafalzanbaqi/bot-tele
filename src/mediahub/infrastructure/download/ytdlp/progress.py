"""Turns yt-dlp progress hooks into MediaHub progress updates.

The hook is also where three guards run, because it is the only code that
executes regularly *inside* a transfer:

* **cancellation** - the token is checked on every tick, so a cancelled download
  stops at the next chunk boundary rather than when the file finishes;
* **the deadline** - a wall-clock budget is enforced from inside the worker
  thread, not only by the caller's ``wait_for``, so the thread actually unwinds;
* **the byte ceiling** - enforced while streaming, because a declared size is a
  hint from an untrusted party.

Updates are throttled. yt-dlp calls its hook on every chunk; forwarding that
rate to a consumer would flood it and, once progress is persisted, would destroy
an SD card (``docs/architecture/05-component-communication.md`` §5.9). Stage
changes and the final update are always emitted, whatever the throttle says.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

from mediahub.application.common.cancellation import CancellationReason
from mediahub.application.download.ports import DownloadProgress, DownloadStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.ports import ProgressCallback

_PROGRESS_DELTA_BYTES: Final[int] = 4 * 1024 * 1024


class EngineAbort(Exception):  # noqa: N818 - a control signal, not a reported failure
    """Raised inside the engine thread to unwind a download deliberately.

    Never surfaces to callers: :class:`ProgressBridge`'s owner converts it into
    the appropriate typed error. It exists because aborting yt-dlp from a
    progress hook is the only supported way to stop it mid-transfer.

    Attributes:
        reason: Why the abort was requested.
        observed_bytes: Bytes transferred when the abort was raised.
    """

    def __init__(self, reason: CancellationReason, observed_bytes: int = 0) -> None:
        """Initialise the signal with its reason and the observed progress."""
        super().__init__(f"engine aborted: {reason.value}")
        self.reason = reason
        self.observed_bytes = observed_bytes


class SizeCeilingExceeded(Exception):  # noqa: N818 - a control signal
    """Raised inside the engine thread when the byte ceiling is breached.

    Attributes:
        limit_bytes: The ceiling.
        observed_bytes: What had been transferred when it was noticed.
    """

    def __init__(self, limit_bytes: int, observed_bytes: int) -> None:
        """Initialise the signal with the ceiling and the observed size."""
        super().__init__(f"exceeded {limit_bytes} byte ceiling at {observed_bytes} bytes")
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes


class ProgressBridge:
    """Adapts yt-dlp's hook protocol and enforces the in-transfer guards."""

    __slots__ = (
        "_callback",
        "_cancellation",
        "_deadline",
        "_last_bytes",
        "_last_emit",
        "_last_stage",
        "_max_bytes",
        "_min_interval",
        "_observed_bytes",
    )

    def __init__(
        self,
        *,
        callback: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
        max_bytes: int | None = None,
        deadline: float | None = None,
        min_interval_seconds: float = 0.5,
    ) -> None:
        """Configure the bridge.

        Args:
            callback: Where updates are sent. ``None`` disables reporting but
                keeps every guard active.
            cancellation: Checked on every tick.
            max_bytes: Hard ceiling for the transfer.
            deadline: Monotonic time after which the transfer must abort.
            min_interval_seconds: Shortest gap between two forwarded updates.
        """
        self._callback = callback
        self._cancellation = cancellation
        self._max_bytes = max_bytes
        self._deadline = deadline
        self._min_interval = max(0.0, min_interval_seconds)
        self._last_emit = 0.0
        self._last_bytes = 0
        self._last_stage: DownloadStage | None = None
        self._observed_bytes = 0

    @property
    def observed_bytes(self) -> int:
        """Return the highest byte count seen so far."""
        return self._observed_bytes

    def check_guards(self) -> None:
        """Run the cancellation, deadline and ceiling checks.

        Raises:
            EngineAbort: If cancellation was requested or the deadline passed.
            SizeCeilingExceeded: If the ceiling has been breached.
        """
        if self._cancellation is not None and self._cancellation.cancelled:
            reason = self._cancellation.reason or CancellationReason.REQUESTED
            raise EngineAbort(reason, self._observed_bytes)
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise EngineAbort(CancellationReason.TIMEOUT, self._observed_bytes)
        if self._max_bytes is not None and self._observed_bytes > self._max_bytes:
            raise SizeCeilingExceeded(self._max_bytes, self._observed_bytes)

    def emit(self, progress: DownloadProgress, *, force: bool = False) -> None:
        """Forward an update, subject to throttling."""
        if self._callback is None:
            return
        now = time.monotonic()
        stage_changed = progress.stage is not self._last_stage
        moved_enough = progress.downloaded_bytes - self._last_bytes >= _PROGRESS_DELTA_BYTES
        due = now - self._last_emit >= self._min_interval

        unknown_total = progress.total_bytes is None
        if force or stage_changed or (due and (moved_enough or unknown_total)):
            self._last_emit = now
            self._last_bytes = progress.downloaded_bytes
            self._last_stage = progress.stage
            self._callback(progress)

    def stage(self, stage: DownloadStage) -> None:
        """Report a stage transition. Always forwarded."""
        self.emit(DownloadProgress(stage=stage, downloaded_bytes=self._observed_bytes), force=True)

    def on_download_hook(self, status: Mapping[str, Any]) -> None:
        """Handle one yt-dlp download hook call.

        Raises:
            EngineAbort: If cancellation was requested or the deadline passed.
            SizeCeilingExceeded: If the byte ceiling has been breached.
        """
        downloaded = _as_int(status.get("downloaded_bytes")) or 0
        self._observed_bytes = max(self._observed_bytes, downloaded)
        self.check_guards()

        state = status.get("status")
        if state == "finished":
            self.emit(self._build(status, DownloadStage.DOWNLOADING), force=True)
            return
        if state != "downloading":
            return
        self.emit(self._build(status, DownloadStage.DOWNLOADING))

    def on_postprocessor_hook(self, status: Mapping[str, Any]) -> None:
        """Handle one yt-dlp post-processor hook call."""
        self.check_guards()
        if status.get("status") == "started":
            self.stage(DownloadStage.POSTPROCESSING)

    def _build(self, status: Mapping[str, Any], stage: DownloadStage) -> DownloadProgress:
        """Build a progress DTO from a raw hook payload."""
        total = _as_int(status.get("total_bytes"))
        estimated = False
        if total is None:
            total = _as_int(status.get("total_bytes_estimate"))
            estimated = total is not None
        return DownloadProgress(
            stage=stage,
            downloaded_bytes=self._observed_bytes,
            total_bytes=total,
            total_is_estimate=estimated,
            speed_bps=_as_float(status.get("speed")),
            eta_seconds=_as_float(status.get("eta")),
            filename=_basename(status.get("filename")),
            fragment_index=_as_int(status.get("fragment_index")),
            fragment_count=_as_int(status.get("fragment_count")),
        )


def _as_int(value: object) -> int | None:
    """Coerce a hook field to a non-negative int, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    coerced = int(value)
    return coerced if coerced >= 0 else None


def _as_float(value: object) -> float | None:
    """Coerce a hook field to a non-negative float, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    coerced = float(value)
    return coerced if coerced >= 0 else None


def _basename(value: object) -> str | None:
    """Return just the file name from a hook's path field.

    Full paths are deliberately not forwarded: progress updates reach logs and
    user interfaces, and neither needs the layout of the device's filesystem.
    """
    if not isinstance(value, str) or not value:
        return None
    return value.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
