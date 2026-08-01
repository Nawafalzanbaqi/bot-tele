"""Application layer - the use cases of MediaHub.

Each use case is a single, named intention ("register media", "cancel a
download job"). It orchestrates domain objects, opens exactly one transaction,
and publishes whatever events the aggregates recorded. It contains no business
rules of its own - those belong to the domain - and no I/O details - those
belong to adapters behind ports.

What this layer may import:

* :mod:`mediahub.domain` and :mod:`mediahub.shared`;
* Loguru, for observability.

What it may **never** import: FastAPI, SQLAlchemy, HTTP, or anything from
:mod:`mediahub.infrastructure` / :mod:`mediahub.presentation`. Everything it
needs from the outside world is declared as a port here and satisfied by an
adapter at wiring time.

Sub-packages:

* :mod:`mediahub.application.common` - use case contract, ports, unit of work.
* :mod:`mediahub.application.media` - library use cases.
* :mod:`mediahub.application.download` - download orchestration use cases.
"""

from __future__ import annotations
