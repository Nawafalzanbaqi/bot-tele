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

Three things this adapter must get right, none of them obvious:

* **Confinement.** gallery-dl is configured through *process-global* state and
  writes wherever that state points. Every download here pins the base
  directory to the caller's lease and flattens the directory template, so
  nothing lands beside the process's working directory.
* **Serialisation.** That global configuration is shared by every thread, so
  two concurrent downloads would silently write into each other's lease. One
  lock covers configure-and-run.
* **Honest metadata.** A probe must not download. It enumerates what the source
  offers and reports the count, which is what lets the layers above offer a
  single "original" choice instead of a resolution ladder that does not exist.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from loguru import logger

from mediahub.application.download.errors import (
    AuthenticationRequiredError,
    ContentRemovedError,
    DownloadFailedError,
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
    from collections.abc import Sequence

    from mediahub.application.common.cancellation import CancellationToken
    from mediahub.application.download.ports import (
        DownloadRequest,
        ProgressCallback,
    )
    from mediahub.application.workspace.ports import ArtifactRef, WorkspaceScope
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
the other's output directory. The download itself happens inside the lock, which
is acceptable because the gateway already admits one acquisition at a time."""

_IMAGE_SUFFIXES: Final[tuple[str, ...]] = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".avif",
)  # fmt: skip

MAX_ITEMS: Final[int] = 20
"""Ceiling on how many files one post may yield.

A carousel is a handful of pictures; a profile is thousands. Without a bound, a
mistyped link becomes an unattended device filling its disk."""


def _build_enumerator(url: str) -> Any:
    """Return a gallery-dl job that collects URLs instead of fetching them.

    Built here rather than at import time because it subclasses a gallery-dl
    class, and this module must import cleanly when the engine is absent.
    """

    class Counter(gallery_dl.job.Job):  # type: ignore[misc]
        """A job whose only effect is to remember what it was offered."""

        def __init__(self, target: str) -> None:
            super().__init__(target)
            self.seen: list[tuple[str, dict[str, Any]]] = []

        def handle_url(self, item_url: str, kwdict: dict[str, Any]) -> None:
            self.seen.append((item_url, kwdict))

        def handle_queue(self, item_url: str, kwdict: dict[str, Any]) -> None:
            self.seen.append((item_url, kwdict))

    return Counter(url)


class GalleryDlDownloader:
    """Fetches still images with gallery-dl, behind the engine-neutral port."""

    __slots__ = ("_settings", "_url_policy")

    def __init__(
        self,
        settings: DownloadSettings,
        *,
        url_policy: UrlPolicy | None = None,
    ) -> None:
        """Wire the engine to its configuration and the URL rules."""
        self._settings = settings
        self._url_policy = url_policy or UrlPolicy()

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
        validated = self._url_policy.validate(url)
        budget = timeout_seconds or self._settings.probe_timeout_seconds
        seen = await asyncio.wait_for(
            asyncio.to_thread(self._blocking_enumerate, validated.value), timeout=budget
        )
        if not seen:
            message = f"'{validated.value}' offers no images either"
            raise NoPlayableMediaError(message, provider=ENGINE_NAME)

        first = seen[0][1]
        title = (
            str(
                first.get("content")
                or first.get("description")
                or first.get("title")
                or validated.host
            ).strip()
            or validated.host
        )
        logger.bind(engine=ENGINE_NAME, items=len(seen), host=validated.host).debug(
            "Enumerated an image source"
        )
        return MediaMetadata(
            url=validated.value,
            provider=str(first.get("category") or validated.host),
            provider_item_id=str(first.get("id") or "") or None,
            title=title[:200],
            kind=MediaType.IMAGE,
            uploader=str(first.get("author") or first.get("username") or "") or None,
            probed_at=datetime.now(UTC),
        )

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
        validated = self._url_policy.validate(request.url)
        started_at = datetime.now(UTC)
        existing = set(workspace.names())
        budget = request.timeout_seconds or self._settings.download_timeout_seconds

        await asyncio.wait_for(
            asyncio.to_thread(self._blocking_fetch, validated.value, workspace),
            timeout=budget,
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
            metadata=await self.probe(validated.value),
            selected_format=SelectedFormat(format_id="original", container=None),
            total_bytes=total,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    # -- Internals -----------------------------------------------------------

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

    def _blocking_enumerate(self, url: str) -> list[tuple[str, dict[str, Any]]]:
        """Walk the source on a worker thread and return what it offers."""
        if gallery_dl is None:  # pragma: no cover - depends on the environment
            message = "gallery-dl is not installed; images cannot be fetched"
            raise UnsupportedProviderError(message)
        with _CONFIG_LOCK:
            self._configure()
            job = _build_enumerator(url)
            try:
                job.run()
            except Exception as exc:
                raise _classify(exc, url) from exc
            return list(job.seen[:MAX_ITEMS])

    def _blocking_fetch(self, url: str, workspace: WorkspaceScope) -> None:
        """Download on a worker thread, confined to the lease directory."""
        if gallery_dl is None:  # pragma: no cover - depends on the environment
            message = "gallery-dl is not installed; images cannot be fetched"
            raise UnsupportedProviderError(message)
        directory = workspace.directory()
        with _CONFIG_LOCK:
            self._configure()
            # security: pin every write into the lease. `directory` is emptied
            # so gallery-dl does not build its usual site/user tree inside it,
            # and the filename is generated rather than taken from the source.
            gallery_dl.config.set((), "base-directory", str(directory))
            gallery_dl.config.set((), "directory", [])
            gallery_dl.config.set((), "filename", "{num:>03}.{extension}")
            gallery_dl.config.set((), "range", f"1-{MAX_ITEMS}")
            try:
                gallery_dl.job.DownloadJob(url).run()
            except Exception as exc:
                raise _classify(exc, url) from exc

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
        if self._settings.cookies_file is not None:
            gallery_dl.config.set((), "cookies", str(self._settings.cookies_file))
        if self._settings.proxy:
            gallery_dl.config.set((), "proxy", self._settings.proxy)
        if self._settings.user_agent:
            gallery_dl.config.set((), "user-agent", self._settings.user_agent)


def _classify(exc: BaseException, url: str) -> Exception:
    """Map a gallery-dl failure onto the shared taxonomy.

    Deliberately duck-typed on the class *name*, for the same reason the video
    engine's classifier is: an ``isinstance`` check against a class the library
    has since moved stops matching silently.
    """
    name = type(exc).__name__
    detail = str(exc).strip() or name
    if name in {"NoExtractorError", "UnsupportedError"}:
        return UnsupportedProviderError(f"'{url}' has no image extractor: {detail}")
    if name in {"AuthenticationError", "AuthorizationError"}:
        return AuthenticationRequiredError(f"'{url}' cannot be retrieved: {detail}")
    if name == "NotFoundError":
        return ContentRemovedError(f"'{url}' cannot be retrieved: {detail}")
    return DownloadFailedError(f"'{url}' failed: {detail}", provider=ENGINE_NAME)
