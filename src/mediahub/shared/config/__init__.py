"""Typed, validated application configuration.

Configuration is read once from the environment (and an optional ``.env``
file), validated by Pydantic, and then treated as immutable. Nothing in the
codebase reads ``os.environ`` directly - if a value is worth configuring, it is
worth being a typed field on :class:`~mediahub.shared.config.settings.Settings`.

See :mod:`mediahub.shared.config.settings` for the full schema and
``.env.example`` for every supported variable.
"""

from __future__ import annotations
