"""Reusable domain building blocks shared by every aggregate.

Nothing in this package is MediaHub-specific; it is the vocabulary the rest of
the domain is written in:

* :mod:`~mediahub.domain.common.entity` - identity and aggregate-root bases.
* :mod:`~mediahub.domain.common.value_object` - immutable value-object base.
* :mod:`~mediahub.domain.common.events` - domain event base.
* :mod:`~mediahub.domain.common.errors` - the domain error hierarchy.
* :mod:`~mediahub.domain.common.pagination` - page request/response primitives
  used by repository ports.
"""

from __future__ import annotations
