"""Structured logging built on Loguru.

One sink, one format, one place to configure it. The rules that keep logs
useful over a long project life:

* **Never call ``print``.** Ruff's ``T20`` rule enforces this.
* **Never configure logging outside :func:`~mediahub.shared.logging.setup.configure_logging`.**
  It is called exactly once, at process start.
* **Log structured context, not interpolated prose.** Prefer
  ``logger.bind(media_id=str(media_id)).info("Registered media")`` over an
  f-string, so a collector can index the fields.
* **Every record carries a correlation id.** It is injected automatically from
  a context variable (see :mod:`~mediahub.shared.logging.context`), so a single
  request can be traced across every layer.

Everything the standard library, uvicorn and SQLAlchemy log is intercepted and
routed through the same sink - see :mod:`~mediahub.shared.logging.intercept`.
"""

from __future__ import annotations
