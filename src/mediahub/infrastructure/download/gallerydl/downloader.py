"""Still-image engine, behind the same port as the video engine.

yt-dlp does not download photographs, and not by oversight. Its extractors
filter them out at the source: X drops every media item whose ``type`` is
``photo``, Instagram skips any node that is not a video, its story handler keeps
only items that produced a *format*, and TikTok's extractor contains no image
handling at all. A post of pictures therefore comes back as "no video could be
found", which is true and useless - the pictures are right there.

gallery-dl is the tool built for that half of the problem, and it plugs in here
rather than replacing anything: :class:`~...ports.DownloaderPort` is the seam,
so the video engine keeps every source it already handles and this one is asked
only about what the other refuses.

Four things this adapter must get right, none of them obvious:

* **Confinement.** gallery-dl is configured through *process-global* state and
  writes wherever that state points. Every download here pins the base
  directory to the caller's lease and flattens the directory template, so
  nothing lands beside the process's working directory.
* **Serialisation, bounded.** That global configuration is shared by every
  thread, so two concurrent downloads would silently write into each other's
  lease. One lock covers configure-and-run - and a caller waits for it only
  within its own budget. Without the bound, a probe queued behind a slow
  download timed out on the event loop while its thread went on waiting, then
  ran a full enumeration nobody was listening to.
* **Honest metadata, once.** A probe must not download. It enumerates what the
  source offers and reports the count. A download records the same facts as
  it goes, so the result is described without a second trip to the site.
* **Failures have names.** gallery-dl's job runner catches its own exceptions
  and reports them as status bits. Reading those bits is what turns "produced
  no file" into "you need to be logged in for this one".
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from loguru import logger

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    ContentRemovedError,
    DownloadFailedError,
    DownloadTimeoutError,
    MetadataUnavailableError,
    NoPlayableMediaError,
    SizeLimitExceededError,
    UnsupportedProviderError,
)
from mediahub.application.download.ports import (
    DownloadCapabilities,
    DownloadResult,
    MediaMetadata,
    SelectedFormat,
)
from mediahub.application.workspace.ports import ArtifactRole
from mediahub.domain.media.enums import MediaType
from mediahub.domain.sources.policies import UrlPolicy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterator, Sequence

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.ports import (
        DownloadRequest,
        ProgressCallback,
    )
    from mediahub.application.workspace.ports import ArtifactRef, WorkspaceScope
    from mediahub.domain.sources.value_objects import ValidatedUrl
    from mediahub.infrastructure.security.address_guard import DnsAddressGuard
    from mediahub.shared.config.settings import DownloadSettings

try:  # pragma: no cover - depends on the environment
    import gallery_dl
    import gallery_dl.config
    import gallery_dl.job
    from gallery_dl import version as _gallery_version
except ImportError:  # pragma: no cover - the engine is optional
    gallery_dl = None
    _gallery_version = None

ENGINE_NAME: Final[str] = "gallery-dl"

_CONFIG_LOCK: Final[threading.Lock] = threading.Lock()
"""Guards gallery-dl's process-global configuration.

Not a performance concern and not optional: the library keeps its settings in a
module-level dictionary, so two downloads configuring it at once would each see
the other's output directory. The download itself happens inside the lock."""

LOCK_WAIT_FRACTION: Final[float] = 0.8
"""How much of an operation's budget may be spent waiting for the lock.

Less than all of it, so a caller that gives up is guaranteed to have given up
*before* the thread could start work it would never see the end of."""

_IMAGE_SUFFIXES: Final[tuple[str, ...]] = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".avif",
)  # fmt: skip

MAX_ITEMS: Final[int] = 20
"""Ceiling on how many files one post may yield.

A carousel is a handful of pictures; a profile is thousands. Without a bound, a
mistyped link becomes an unattended device filling its disk."""

# gallery-dl's job status bits, as set by ``Job.run`` when it catches one of
# its own exceptions. The runner does not re-raise, so these are the only
# record of *why* a job produced nothing.
_STATUS_AUTH: Final[int] = 16
_STATUS_INPUT: Final[int] = 32
_STATUS_EXTRACTION: Final[int] = 4
_STATUS_OSERROR: Final[int] = 128

Seen = list[tuple[str, dict[str, Any]]]


def _build_enumerator(url: str, *, deadline: float) -> Any:
    """Return a gallery-dl job that collects URLs instead of fetching them.

    Built here rather than at import time because it subclasses a gallery-dl
    class, and this module must import cleanly when the engine is absent.
    """

    class Counter(gallery_dl.job.Job):  # type: ignore[misc]
        """A job whose only effect is to remember what it was offered."""

        def __init__(self, target: str) -> None:
            super().__init__(target)
            self.seen: Seen = []
            self.timed_out = False

        def handle_url(self, item_url: str, kwdict: dict[str, Any]) -> None:
            if time.monotonic() > deadline:
                self.timed_out = True
                return
            self.seen.append((item_url, kwdict))

        def handle_queue(self, item_url: str, kwdict: dict[str, Any]) -> None:
            self.handle_url(item_url, kwdict)

    return Counter(url)


def _build_fetcher(url: str, *, deadline: float) -> Any:
    """Return a gallery-dl download job that also remembers what it fetched.

    Remembering is what lets a download be described without enumerating the
    source a second time. The deadline check is what bounds a download the
    caller has stopped waiting for: the item in flight finishes, the rest are
    skipped rather than fetched into a lease that no longer exists.
    """

    class Recorder(gallery_dl.job.DownloadJob):  # type: ignore[misc]
        """A download job that records the items it handled."""

        def __init__(self, target: str) -> None:
            super().__init__(target)
            self.seen: Seen = []
            self.timed_out = False

        def handle_url(self, item_url: str, kwdict: dict[str, Any]) -> None:
            if time.monotonic() > deadline:
                self.timed_out = True
                return
            self.seen.append((item_url, dict(kwdict)))
            super().handle_url(item_url, kwdict)

    return Recorder(url)


class GalleryDlDownloader:
    """Fetches still images with gallery-dl, behind the engine-neutral port."""

    __slots__ = ("_address_guard", "_settings", "_url_policy")

    def __init__(
        self,
        settings: DownloadSettings,
        *,
        url_policy: UrlPolicy | None = None,
        address_guard: DnsAddressGuard | None = None,
    ) -> None:
        """Wire the engine to its configuration and the URL rules.

        Args:
            settings: Engine configuration; shares the video engine's.
            url_policy: URL rules. A default policy is used when omitted.
            address_guard: Resolves hosts and rejects forbidden addresses -
                the same gate the video engine applies. Omitting it leaves
                only the syntactic half of the check.
        """
        self._settings = settings
        self._url_policy = url_policy or UrlPolicy()
        self._address_guard = address_guard

    # -- Port surface --------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the engine's name, for logs and selection."""
        return ENGINE_NAME

    @property
    def is_available(self) -> bool:
        """Return whether the library is installed in this environment."""
        return gallery_dl is not None

    def capabilities(self) -> DownloadCapabilities:
        """Return what this engine can currently do.

        Everything to do with streams is false, and honestly so: there is no
        merging, no audio extraction and no rendition ladder, because a
        photograph has none of those.
        """
        return DownloadCapabilities(
            engine=ENGINE_NAME,
            version=str(getattr(_gallery_version, "__version__", "unknown")),
            supports_format_selection=False,
            supports_audio_only=False,
            supports_resume=False,
            supports_playlists=True,
            supports_thumbnails=False,
            requires_external_merger=False,
        )

    def supports(self, url: str) -> bool:
        """Return whether gallery-dl claims an extractor for this URL."""
        if gallery_dl is None:
            return False
        try:
            self._url_policy.validate(url)
        except Exception:
            return False
        try:
            return gallery_dl.extractor.find(url) is not None
        except Exception:
            return False

    async def probe(self, url: str, *, timeout_seconds: float | None = None) -> MediaMetadata:
        """Enumerate what the source offers, without downloading any of it."""
        validated = self._validate(url)
        budget = timeout_seconds or self._settings.probe_timeout_seconds
        deadline = time.monotonic() + budget
        seen = await self._on_thread(
            lambda: self._blocking_enumerate(validated.value, budget=budget, deadline=deadline),
            budget=budget,
            url=validated.value,
        )
        logger.bind(engine=ENGINE_NAME, items=len(seen), host=validated.host).debug(
            "Enumerated an image source"
        )
        return _describe_source(seen, validated)

    async def fetch(
        self,
        request: DownloadRequest,
        workspace: WorkspaceScope,
        *,
        on_progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        """Download every image the source offers into ``workspace``."""
        del on_progress, cancellation  # gallery-dl reports neither.
        validated = self._validate(request.url)
        started_at = datetime.now(UTC)
        existing = set(workspace.names())
        budget = request.timeout_seconds or self._settings.download_timeout_seconds
        deadline = time.monotonic() + budget

        seen = await self._on_thread(
            lambda: self._blocking_fetch(
                validated.value, workspace, budget=budget, deadline=deadline
            ),
            budget=budget,
            url=validated.value,
        )

        produced = [name for name in workspace.names() if name not in existing]
        if not produced:
            message = f"'{validated.value}' produced no file"
            raise MetadataUnavailableError(message, provider=ENGINE_NAME)

        artifacts = self._describe(produced, workspace)
        total = sum(artifact.size_bytes for artifact in artifacts)
        if request.max_bytes is not None and total > request.max_bytes:
            raise SizeLimitExceededError(request.max_bytes, total)

        logger.bind(engine=ENGINE_NAME, files=len(artifacts), bytes=total).info("Download complete")
        return DownloadResult(
            url=validated.value,
            provider=ENGINE_NAME,
            artifacts=artifacts,
            metadata=_describe_source(seen, validated),
            selected_format=SelectedFormat(format_id="original", container=None),
            total_bytes=total,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    # -- Internals -----------------------------------------------------------

    def _validate(self, url: str) -> ValidatedUrl:
        """Apply the URL policy, then the address guard - the video engine's gate."""
        validated = self._url_policy.validate(url)
        if self._address_guard is not None:
            self._address_guard.check(validated)
        return validated

    @staticmethod
    async def _on_thread[T](work: Callable[[], T], *, budget: float, url: str) -> T:
        """Run engine work on a worker thread inside ``budget`` seconds.

        The thread cannot be killed. What a timeout does is stop *waiting*, and
        say so: the job checks the same deadline between items and skips the
        rest, so the overrun is bounded to the item in flight.
        """
        try:
            result: T = await asyncio.wait_for(asyncio.to_thread(work), timeout=budget)
        except TimeoutError as exc:
            logger.bind(engine=ENGINE_NAME, url_host=url.split("/")[2], budget=budget).warning(
                "The image engine exceeded its budget; its thread will stop at the next item"
            )
            message = f"'{url}' exceeded its {budget:.0f}s budget"
            raise DownloadTimeoutError(message, provider=ENGINE_NAME) from exc
        return result

    def _describe(
        self, produced: Sequence[str], workspace: WorkspaceScope
    ) -> tuple[ArtifactRef, ...]:
        """Turn produced files into artifacts, largest image first.

        The largest is made primary because it is the one a person means when a
        post holds a picture and its thumbnail, and because a destination that
        can show only one should show the best of them.
        """
        refs = [workspace.artifact(name, role=ArtifactRole.PRIMARY) for name in sorted(produced)]
        refs.sort(key=lambda ref: (ref.name.lower().endswith(_IMAGE_SUFFIXES), ref.size_bytes))
        refs.reverse()
        return tuple(
            ref if index == 0 else workspace.artifact(ref.name, role=ArtifactRole.COMPANION)
            for index, ref in enumerate(refs)
        )

    def _blocking_enumerate(self, url: str, *, budget: float, deadline: float) -> Seen:
        """Walk the source on a worker thread and return what it offers."""
        with self._configured(budget=budget, url=url):
            try:
                job = _build_enumerator(url, deadline=deadline)
                job.run()
            except Exception as exc:
                raise _classify(exc, url) from exc
            if job.timed_out:
                message = f"'{url}' could not be enumerated within {budget:.0f}s"
                raise DownloadTimeoutError(message, provider=ENGINE_NAME)
            seen: Seen = list(job.seen[:MAX_ITEMS])
            if not seen:
                raise _from_status(int(job.status), url)
            return seen

    def _blocking_fetch(
        self, url: str, workspace: WorkspaceScope, *, budget: float, deadline: float
    ) -> Seen:
        """Download on a worker thread, confined to the lease directory."""
        directory = workspace.directory()
        with self._configured(budget=budget, url=url):
            # security: pin every write into the lease. `directory` is emptied
            # so gallery-dl does not build its usual site/user tree inside it,
            # and the filename is generated rather than taken from the source.
            gallery_dl.config.set((), "base-directory", str(directory))
            gallery_dl.config.set((), "directory", [])
            gallery_dl.config.set((), "filename", "{num:>03}.{extension}")
            gallery_dl.config.set((), "range", f"1-{MAX_ITEMS}")
            try:
                job = _build_fetcher(url, deadline=deadline)
                job.run()
            except Exception as exc:
                raise _classify(exc, url) from exc
            if job.timed_out:
                message = f"'{url}' could not be fetched within {budget:.0f}s"
                raise DownloadTimeoutError(message, provider=ENGINE_NAME)
            seen: Seen = list(job.seen)
            if not seen:
                raise _from_status(int(job.status), url)
            return seen

    @contextlib.contextmanager
    def _configured(self, *, budget: float, url: str) -> Iterator[None]:
        """Hold the engine lock, within the caller's budget, with the shared settings applied."""
        if gallery_dl is None:  # pragma: no cover - depends on the environment
            message = "gallery-dl is not installed; images cannot be fetched"
            raise UnsupportedProviderError(message)
        wait = max(0.05, budget * LOCK_WAIT_FRACTION)
        if not _CONFIG_LOCK.acquire(timeout=wait):
            message = f"'{url}' waited {wait:.0f}s for the image engine, which was busy"
            raise DownloadTimeoutError(message, provider=ENGINE_NAME)
        try:
            self._configure()
            yield
        finally:
            _CONFIG_LOCK.release()

    def _configure(self) -> None:
        """Apply the settings shared by enumeration and download."""
        gallery_dl.config.clear()
        gallery_dl.config.set((), "quiet", True)
        gallery_dl.config.set((), "verbose", False)
        # security: no shell, ever. gallery-dl can run commands after a
        # download and nothing here asks it to.
        gallery_dl.config.set((), "postprocessors", [])
        gallery_dl.config.set((), "skip", True)
        gallery_dl.config.set((), "sleep-request", 0)
        # A stalled connection is a failure, not a hung worker: the same read
        # timeout the video engine uses, applied to every request.
        gallery_dl.config.set((), "timeout", float(self._settings.socket_timeout_seconds))
        gallery_dl.config.set((), "retries", int(self._settings.retries))
        if self._settings.cookies_file is not None:
            gallery_dl.config.set((), "cookies", str(self._settings.cookies_file))
        if self._settings.proxy:
            gallery_dl.config.set((), "proxy", self._settings.proxy)
        if self._settings.user_agent:
            gallery_dl.config.set((), "user-agent", self._settings.user_agent)


def _describe_source(seen: Seen, validated: ValidatedUrl) -> MediaMetadata:
    """Turn what a job saw into metadata, without touching the network again."""
    if not seen:
        message = f"'{validated.value}' offers no images either"
        raise NoPlayableMediaError(message, provider=ENGINE_NAME)
    first = seen[0][1]
    title = (
        str(
            first.get("content") or first.get("description") or first.get("title") or validated.host
        ).strip()
        or validated.host
    )
    return MediaMetadata(
        url=validated.value,
        provider=str(first.get("category") or validated.host),
        provider_item_id=str(first.get("id") or "") or None,
        title=title[:200],
        kind=MediaType.IMAGE,
        uploader=str(first.get("author") or first.get("username") or "") or None,
        entry_count=len(seen),
        probed_at=datetime.now(UTC),
    )


def _from_status(status: int, url: str) -> Exception:
    """Name the failure a job recorded in its status bits, having produced nothing.

    ``Job.run`` catches gallery-dl's own exceptions, logs them and sets a bit;
    it does not re-raise. A clean status with nothing seen means the source
    was read and holds no image - the one refusal that means "ask nobody else".
    """
    if status == 0:
        return NoPlayableMediaError(f"'{url}' offers no images either", provider=ENGINE_NAME)
    if status & _STATUS_AUTH:
        return AuthenticationRequiredError(f"'{url}' is not shown to a signed-out visitor")
    if status & _STATUS_INPUT:
        return UnsupportedProviderError(f"'{url}' has no image extractor")
    if status & (_STATUS_EXTRACTION | _STATUS_OSERROR):
        return DownloadFailedError(f"'{url}' failed (status {status})", provider=ENGINE_NAME)
    return DownloadFailedError(f"'{url}' failed (status {status})", provider=ENGINE_NAME)


def _classify(exc: BaseException, url: str) -> Exception:
    """Map a gallery-dl failure onto the shared taxonomy.

    Deliberately duck-typed on the class *name*, for the same reason the video
    engine's classifier is: an ``isinstance`` check against a class the library
    has since moved stops matching silently.
    """
    if isinstance(exc, (DownloadTimeoutError, MetadataUnavailableError)):
        return exc
    name = type(exc).__name__
    detail = str(exc).strip() or name
    if name in {"NoExtractorError", "UnsupportedError"}:
        return UnsupportedProviderError(f"'{url}' has no image extractor: {detail}")
    if name in {"AuthenticationError", "AuthorizationError", "AuthRequired"}:
        return AuthenticationRequiredError(f"'{url}' cannot be retrieved: {detail}")
    if name == "NotFoundError":
        return ContentRemovedError(f"'{url}' cannot be retrieved: {detail}")
    return DownloadFailedError(f"'{url}' failed: {detail}", provider=ENGINE_NAME)
