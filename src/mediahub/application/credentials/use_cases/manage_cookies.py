"""Use cases: install, inspect and remove the engine's cookie jar.

Thin on purpose. The rules that matter live elsewhere - who may do this is the
access policy's answer, and what counts as a usable jar is the store's - so
what is left here is the sequence, which is the same whether the request
arrived from a chat, from HTTP or from a script.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mediahub.application.credentials.ports import CookieStore, CookieSummary


class InstallCookies:
    """Replace the jar the engine presents to sources."""

    def __init__(self, *, store: CookieStore) -> None:
        """Wire the use case to wherever the jar is kept."""
        self._store = store

    async def execute(self, content: bytes, *, requested_by: str) -> CookieSummary:
        """Validate and store a jar, returning what it turned out to hold.

        Args:
            content: The raw uploaded file.
            requested_by: Identity of the owner performing this, recorded
                because replacing this file changes who the device fetches as.

        Returns:
            A summary with no credentials in it.

        Raises:
            InvalidCookieJarError: If the content is not a usable jar.
            CookieStoreUnavailableError: If there is nowhere to put it.
        """
        summary = await self._store.install(content)
        logger.bind(
            actor=requested_by,
            cookies=summary.cookie_count,
            domains=list(summary.domains),
        ).info("Cookie jar replaced")
        return summary


class DescribeCookies:
    """Report what jar is currently installed, if any."""

    def __init__(self, *, store: CookieStore) -> None:
        """Wire the use case to wherever the jar is kept."""
        self._store = store

    async def execute(self) -> CookieSummary | None:
        """Return the stored jar's summary, or ``None`` if there is none."""
        return await self._store.describe()


class DiscardCookies:
    """Remove the stored jar, so the engine browses anonymously again."""

    def __init__(self, *, store: CookieStore) -> None:
        """Wire the use case to wherever the jar is kept."""
        self._store = store

    async def execute(self, *, requested_by: str) -> bool:
        """Delete the jar, returning whether there was one to delete."""
        removed = await self._store.discard()
        if removed:
            logger.bind(actor=requested_by).info("Cookie jar discarded")
        return removed
