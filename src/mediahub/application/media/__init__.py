"""Use cases for the media library.

Everything an operator can do to the catalogue: register an item, read one,
list many, archive one. Each operation is a class in
:mod:`~mediahub.application.media.use_cases`; the commands, queries and DTOs
they exchange live in :mod:`~mediahub.application.media.dto`.
"""

from __future__ import annotations
