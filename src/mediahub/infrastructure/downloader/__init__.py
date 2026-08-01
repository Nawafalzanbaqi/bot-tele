"""Download engine adapters.

**This package is intentionally empty of transfer logic.**

:mod:`~mediahub.infrastructure.downloader.null_downloader` is the only adapter
today: it satisfies
:class:`~mediahub.application.download.ports.DownloaderPort` by refusing every
request with a clear, typed error. That keeps the container fully wired, keeps
the type checker honest, and makes the missing capability visible in the API
(``501 Not Implemented``) instead of hidden behind a crash.

Adding a real engine is an additive change:

1. Create a module here (for example ``http_downloader.py``) with a class
   implementing ``supports`` and ``fetch``.
2. Return it from the container instead of ``NullDownloader``.
3. Nothing in ``domain``, ``application`` or ``presentation`` changes.
"""

from __future__ import annotations
