"""The Telegram gateway - a driver, not the product.

Its entire job is four steps: receive an update, authorise the sender, build
**one** application command, format the answer. Everything it knows about is in
:mod:`mediahub.application`; it has never heard of yt-dlp, the workspace, a
repository, a policy or a domain aggregate.

That constraint is what makes the claim "Telegram is one interface" true rather
than aspirational: the same commands are what a REST API, a CLI or a web UI
will call, so a feature added here is not a feature that exists only here.

Modules:

* :mod:`~mediahub.presentation.telegram.api` - the messenger contract the
  gateway needs, declared here so it depends on no adapter.
* :mod:`~mediahub.presentation.telegram.updates` - raw update dictionaries to
  typed intents.
* :mod:`~mediahub.presentation.telegram.keyboards` - inline buttons and the
  callback payloads they carry.
* :mod:`~mediahub.presentation.telegram.sessions` - short-lived state between a
  posted question and a tapped answer.
* :mod:`~mediahub.presentation.telegram.formatters` - DTOs to text.
* :mod:`~mediahub.presentation.telegram.progress` - throttled message editing.
* :mod:`~mediahub.presentation.telegram.handlers` - one function per intent.
* :mod:`~mediahub.presentation.telegram.gateway` - the long-poll loop.
"""

from __future__ import annotations
