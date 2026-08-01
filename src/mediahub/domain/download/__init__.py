"""The download aggregate - MediaHub's record of acquisition work.

A :class:`~mediahub.domain.download.entities.DownloadJob` tracks the intent to
fetch one media item: its priority, how far it got, how many attempts it has
consumed and why it last failed.

Scope note: this package models the *lifecycle* of a job only. It contains no
transfer logic, no protocol handling and no provider integration - those arrive
behind the
:class:`~mediahub.application.download.ports.DownloaderPort` interface, so the
engine can be swapped without touching these rules.

Modules:

* :mod:`~mediahub.domain.download.enums` - job states and priorities.
* :mod:`~mediahub.domain.download.value_objects` - ids, progress, retry policy.
* :mod:`~mediahub.domain.download.entities` - the ``DownloadJob`` aggregate.
* :mod:`~mediahub.domain.download.events` - facts published by the aggregate.
* :mod:`~mediahub.domain.download.errors` - failures the aggregate expresses.
* :mod:`~mediahub.domain.download.repository` - the persistence port.
"""

from __future__ import annotations
