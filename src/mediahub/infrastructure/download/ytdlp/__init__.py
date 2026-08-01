"""The yt-dlp download engine.

This package is the **only** place in MediaHub that knows yt-dlp exists. It
implements :class:`~mediahub.application.download.ports.DownloaderPort` and
translates in both directions:

* out: engine-neutral intent (:class:`FormatSelection`, byte ceilings,
  cancellation) becomes yt-dlp options;
* in: yt-dlp's info dictionaries, progress hooks and exceptions become
  MediaHub DTOs and typed, classified errors.

Modules:

* :mod:`~mediahub.infrastructure.download.ytdlp.options` - option construction.
* :mod:`~mediahub.infrastructure.download.ytdlp.format_selection` - selection to
  format expression.
* :mod:`~mediahub.infrastructure.download.ytdlp.mapping` - info dict to DTOs.
* :mod:`~mediahub.infrastructure.download.ytdlp.progress` - hook to progress,
  plus the ceiling, deadline and cancellation checks that run on every tick.
* :mod:`~mediahub.infrastructure.download.ytdlp.errors` - failure classification.
* :mod:`~mediahub.infrastructure.download.ytdlp.downloader` - the adapter.

Every module except ``downloader`` is pure: no I/O, no yt-dlp import, fully
unit-testable. That is deliberate - it is what allows the interesting logic
(format choice, metadata mapping, error classification) to be tested exhaustively
without a network or a fixture zoo.
"""

from __future__ import annotations
