"""The media aggregate - MediaHub's library of catalogued items.

A :class:`~mediahub.domain.media.entities.MediaItem` is the record of one piece
of content: where it came from, what it is, whether its bytes are available
locally, and where they live. It is the system of record that every other part
of MediaHub refers to.

Modules:

* :mod:`~mediahub.domain.media.enums` - closed sets of media types/states.
* :mod:`~mediahub.domain.media.value_objects` - identifiers, URLs, sizes, hashes.
* :mod:`~mediahub.domain.media.entities` - the ``MediaItem`` aggregate root.
* :mod:`~mediahub.domain.media.events` - facts published by the aggregate.
* :mod:`~mediahub.domain.media.errors` - failures the aggregate can express.
* :mod:`~mediahub.domain.media.repository` - the persistence port.
"""

from __future__ import annotations
