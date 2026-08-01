"""Telegram as a delivery destination.

This package and :mod:`mediahub.presentation.telegram` are the only two places
in MediaHub that know Telegram exists, and they share nothing but a client.
This one sends media *out*; that one translates updates *in*. Deleting both
must break Telegram and nothing else - an architecture test asserts that the
words ``chat_id``, ``file_id`` and ``message_id`` appear nowhere else.

Modules:

* :mod:`~mediahub.infrastructure.delivery.telegram.client` - the narrow slice
  of the Bot API MediaHub uses, plus the real implementation.
* :mod:`~mediahub.infrastructure.delivery.telegram.provider` - the
  ``DeliveryProvider`` implementation.
* :mod:`~mediahub.infrastructure.delivery.telegram.errors` - Telegram failures
  mapped into the shared taxonomy.
"""

from __future__ import annotations
