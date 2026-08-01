"""Delivery provider adapters.

One package per destination. Each implements
:class:`~mediahub.application.delivery.ports.DeliveryProvider` and is the only
place its destination's vocabulary appears.

**Adding a destination.** Filesystem, NAS, S3, webhook and email are all the
same three steps, and none of them touches ``domain/`` or ``application/``:

1. Write a provider in a new sub-package. It accepts a temporary file, uploads
   it, and returns a
   :class:`~mediahub.application.delivery.ports.DeliveryReceipt`. Wrapping the
   artifact in
   :class:`~mediahub.infrastructure.delivery.shared.measured_reader.MeasuredReader`
   yields the size, the checksum and progress from a single pass over the bytes.
2. Declare its
   :class:`~mediahub.application.delivery.ports.DeliveryCapabilities` honestly -
   especially ``maximum_file_size``, which caps the *download*, and
   ``can_serve_back``, which is what authorises deleting the only local copy.
3. Hand it to
   :meth:`~mediahub.infrastructure.di.container.Container.delivery_router` at the
   composition root. Priority and the default destination come from
   configuration, so a deployment can install or prefer one without a code
   change.

The contract suite in ``tests/contract`` is parametrised over every
implementation, so a new provider is one line there and is then held to exactly
the same standard as the others.

**What a provider must never do.** Touch the database, change a job's status,
delete a file, decide whether to retry, or apply a policy. It reports what
happened - typed and classified - and the caller decides. The retry
classification lives in :mod:`mediahub.application.delivery.errors`; provider
health lives in :mod:`mediahub.infrastructure.delivery.registry`.
"""

from __future__ import annotations
