"""A cookie jar kept as one file on disk.

Implements :class:`~mediahub.application.credentials.ports.CookieStore`.

Two properties this adapter must have, both of which come from the same fact -
the engine reads this file while the owner may be replacing it:

* **The swap is atomic.** The new jar is written beside the target and renamed
  onto it. A rename within one directory is atomic on every filesystem this
  runs on, so a download in flight sees either the whole old jar or the whole
  new one, never half a file. Writing in place would let it read a truncated
  jar and fail with the platform's own baffling message.
* **The file is never group- or world-readable.** It is created at ``0600``
  before anything is written into it, rather than afterwards, so there is no
  window in which the credentials exist with looser permissions.

There is no reload to trigger: the engine builds a fresh client per operation
and reads the path each time, so a jar installed now applies to the next
download without restarting anything.
"""

from __future__ import annotations

import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from mediahub.application.credentials.errors import (
    CookieStoreUnavailableError,
    InvalidCookieJarError,
)
from mediahub.application.credentials.ports import CookieSummary
from mediahub.infrastructure.credentials.cookie_jar import decode, merge, parse

_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
"""0600. The jar is a live session; nobody else on the device needs it."""


class FilesystemCookieStore:
    """Keeps the jar at a configured path, swapping it atomically."""

    __slots__ = ("_path",)

    def __init__(self, path: Path | None) -> None:
        """Bind the store to a path, or to nothing at all.

        ``None`` is a legitimate configuration - most deployments never need a
        jar - and is reported as an unavailable store rather than crashing at
        the first upload.
        """
        self._path = path

    @property
    def is_configured(self) -> bool:
        """Return whether a location for the jar exists."""
        return self._path is not None

    async def install(self, content: bytes) -> CookieSummary:
        """Validate the content and swap it in.

        Raises:
            CookieStoreUnavailableError: If no path is configured, or the
                directory cannot be written.
            InvalidCookieJarError: If the content is not a usable jar.
        """
        path = self._require_path()
        try:
            text = decode(content)
        except ValueError as exc:
            raise InvalidCookieJarError(str(exc)) from exc

        parsed = parse(text)
        if parsed.cookie_count == 0:
            message = (
                "no cookies found in that file. It should be a Netscape-format "
                "export, one cookie per line with tab-separated fields"
            )
            raise InvalidCookieJarError(message)

        # Merge rather than replace. An export is taken one site at a time, so
        # overwriting the file makes two sites mutually exclusive: uploading
        # cookies for TikTok would silently sign the bot out of X.
        merged = merge(self._current_text(path), text)
        payload = merged.encode("utf-8")
        parsed = parse(merged)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomically(path, payload)
        except OSError as exc:
            message = f"the cookie jar location is not writable: {exc.strerror}"
            raise CookieStoreUnavailableError(message) from exc

        # Domains, counts and dates only. The jar's contents are never logged.
        logger.bind(cookies=parsed.cookie_count, domains=list(parsed.domains)).info(
            "Installed a cookie jar"
        )
        return CookieSummary(
            cookie_count=parsed.cookie_count,
            domains=parsed.domains,
            earliest_expiry=parsed.earliest_expiry,
            signed_in=parsed.signed_in,
            installed_at=datetime.now(UTC),
            size_bytes=len(payload),
        )

    async def describe(self) -> CookieSummary | None:
        """Return what is stored, or ``None`` if there is nothing readable."""
        if self._path is None or not self._path.is_file():
            return None
        try:
            content = self._path.read_bytes()
            parsed = parse(decode(content))
        except (OSError, ValueError):
            logger.opt(exception=True).warning("The stored cookie jar could not be read")
            return None
        return CookieSummary(
            cookie_count=parsed.cookie_count,
            domains=parsed.domains,
            earliest_expiry=parsed.earliest_expiry,
            signed_in=parsed.signed_in,
            installed_at=datetime.fromtimestamp(self._path.stat().st_mtime, tz=UTC),
            size_bytes=len(content),
        )

    async def discard(self) -> bool:
        """Delete the jar, returning whether one was there."""
        if self._path is None or not self._path.is_file():
            return False
        self._path.unlink(missing_ok=True)
        logger.info("Discarded the stored cookie jar")
        return True

    @staticmethod
    def _current_text(path: Path) -> str:
        """Return what is already stored, or empty for a first install.

        Unreadable is treated as empty rather than fatal: a corrupt jar must
        not block replacing it, which is the one action that would fix it.
        """
        if not path.is_file():
            return ""
        try:
            return decode(path.read_bytes())
        except (OSError, ValueError):
            logger.warning("The stored cookie jar could not be read; replacing it wholesale")
            return ""

    def _require_path(self) -> Path:
        """Return the configured path, or explain that there is not one."""
        if self._path is None:
            message = (
                "no cookie jar location is configured; set "
                "MEDIAHUB_DOWNLOAD__COOKIES_FILE and restart"
            )
            raise CookieStoreUnavailableError(message)
        return self._path

    @staticmethod
    def _write_atomically(path: Path, content: bytes) -> None:
        """Write beside the target, then rename onto it.

        The temporary file is created in the *same directory* so the rename
        stays within one filesystem - across a mount boundary it would degrade
        to a copy, which is exactly the non-atomic write being avoided.
        """
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".cookies-")
        try:
            os.fchmod(handle, _FILE_MODE)
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
