"""SQLite persistence: the system of record on the target device.

SQLite is the right store for a self-hosted appliance
([ADR-0006](../../../../../docs/adr/0006-sqlite-system-of-record.md)): no second
process, no administration, one file to back up, and a transactional enqueue for
free because the queue shares the connection the repositories commit through.

Everything in this package exists because SQLite is *not* PostgreSQL in four
ways that do not raise an error - they simply behave differently:

* it has no timezone-aware column type (:mod:`.types`);
* it ignores foreign keys unless asked, per connection (:mod:`.engine`);
* it drops a ``postgresql_where`` partial index silently (see
  ``models.py``, which now declares ``sqlite_where`` alongside it);
* it has no ``SELECT ... FOR UPDATE SKIP LOCKED`` (:mod:`.queue`).

Each is corrected once, here, so the layers above never learn which database
they are sitting on.
"""

from __future__ import annotations
