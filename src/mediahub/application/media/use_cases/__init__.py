"""One module per media use case.

Splitting them keeps each file small enough to hold in your head and makes the
call graph obvious: a route depends on exactly the operations it invokes, not
on a service class that grew to a thousand lines.
"""

from __future__ import annotations
