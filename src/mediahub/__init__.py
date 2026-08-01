"""MediaHub - a production-grade, self-hosted media hub.

The package is organised in four concentric layers (Clean Architecture). The
dependency rule is absolute and enforced by `tests/architecture`:

    presentation ─┐
                  ├─> application ─> domain
    infrastructure┘

* :mod:`mediahub.domain` - enterprise rules. Pure Python, zero dependencies.
* :mod:`mediahub.application` - use cases orchestrating the domain. Depends on
  the domain only, and talks to the outside world through ports.
* :mod:`mediahub.infrastructure` - adapters implementing those ports
  (databases, filesystems, clocks, downloaders).
* :mod:`mediahub.presentation` - delivery mechanisms; today an HTTP API.
* :mod:`mediahub.shared` - cross-cutting concerns (configuration, logging) that
  any layer may import but which import nothing from the layers themselves.

Nothing in the inner layers may import an outer layer. Wiring happens once, in
the composition root (:mod:`mediahub.infrastructure.di.container`).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
