"""Download engine adapters.

* :mod:`~mediahub.infrastructure.download.shared` - engine-agnostic helpers.
* :mod:`~mediahub.infrastructure.download.ytdlp` - the yt-dlp engine.

Nothing outside these packages may import ``yt_dlp``, and nothing inside them
may import a delivery provider, a repository or an interface. The engine's only
inbound contract is
:class:`~mediahub.application.download.ports.DownloaderPort`.
"""

from __future__ import annotations
