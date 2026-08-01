"""Use cases for download orchestration.

**Scope, deliberately.** This package manages the *lifecycle* of download jobs:
requesting one, reading it, listing many, cancelling one. It does not transfer
a single byte.

The transfer itself sits behind
:class:`~mediahub.application.download.ports.DownloaderPort`. Until an adapter
implements that port, the container wires in
:class:`~mediahub.infrastructure.downloader.null_downloader.NullDownloader`,
which fails loudly rather than pretending to work. Everything around the seam -
persistence, state machine, API, events - is complete and tested, so adding an
engine later is an additive change, not a redesign.
"""

from __future__ import annotations
