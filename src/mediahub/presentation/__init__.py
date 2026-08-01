"""Presentation layer - how the outside world drives MediaHub.

Today there is one delivery mechanism, the HTTP API in
:mod:`mediahub.presentation.api`. A CLI, a Telegram bot or a message consumer
would each be a sibling package here, calling the same use cases.

The layer's job is narrow and worth defending:

1. Parse and validate transport input (Pydantic schemas).
2. Translate it into an application command or query.
3. Await the use case.
4. Serialise the resulting DTO, or map the raised error to a status code.

Anything else - a business rule, a database query, a retry - has leaked from
another layer and should be moved back.
"""

from __future__ import annotations
