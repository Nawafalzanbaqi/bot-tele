"""Pydantic schemas for version 1 of the API.

Schemas are the *transport* contract and nothing else. They never contain
business rules: a rule expressed in a schema applies only to HTTP callers,
while the same rule in a value object applies to every caller forever. Length
caps and type checks belong here; "which state transitions are legal" does not.

Every response model is built from an application DTO through ``from_dto``, so
there is exactly one conversion point per resource.
"""

from __future__ import annotations
