"""Workspace - ephemeral, per-job scratch space.

Generic context: it has no idea what a media file is. It leases space, hands out
names that are guaranteed to stay inside the lease, verifies what was written,
and reclaims everything afterwards.

* :mod:`~mediahub.domain.workspace.identifiers` - the generated lease id.
* :mod:`~mediahub.domain.workspace.entities` - the lease and its lifecycle.
* :mod:`~mediahub.domain.workspace.value_objects` - ownership, disk budget,
  integrity expectations.
* :mod:`~mediahub.domain.workspace.policies` - filename safety, disk admission,
  crash recovery.
* :mod:`~mediahub.domain.workspace.errors` - the failures it can express.

Leases are recorded as a manifest inside each lease directory, which is what
lets a process that starts after a crash decide ownership rather than guess. A
lease *row* in the database, and the periodic janitor that would use it
(``docs/architecture/11-storage-strategy.md`` §11.6), belong to the scheduler
phase; until then recovery runs at startup, over the manifests.
"""

from __future__ import annotations
