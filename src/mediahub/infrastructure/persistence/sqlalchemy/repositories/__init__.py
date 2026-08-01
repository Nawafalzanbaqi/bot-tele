"""SQLAlchemy repository implementations.

One module per aggregate, each bound to the session owned by the surrounding
unit of work. Repositories never commit: deciding when work becomes durable is
the use case's job, not the adapter's.
"""

from __future__ import annotations
