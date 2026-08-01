"""Contracts shared by every use case.

* :mod:`~mediahub.application.common.use_case` - the ``execute`` contract.
* :mod:`~mediahub.application.common.ports` - clock, id generation, event
  publishing: the ambient capabilities a use case may need.
* :mod:`~mediahub.application.common.unit_of_work` - the transaction boundary.
* :mod:`~mediahub.application.common.errors` - orchestration-level failures.
"""

from __future__ import annotations
