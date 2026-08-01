"""In-memory persistence adapters.

Fast, dependency-free implementations used by the test suite and by throwaway
demo runs. They mirror the SQL adapters' observable behaviour - filtering,
ordering, transactional isolation - so a test that passes here means something.

**All data is lost when the process exits.** The configuration layer refuses to
start production with this backend selected.
"""

from __future__ import annotations
