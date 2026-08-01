"""Application contracts for ephemeral scratch space.

The download engine must write somewhere, and that somewhere must be temporary,
contained, and reclaimed whatever happens. This package declares the port; the
filesystem implementation lives in
:mod:`mediahub.infrastructure.workspace.filesystem`.

Nothing here knows what a media file is.
"""

from __future__ import annotations
