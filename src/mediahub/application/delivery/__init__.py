"""Application contracts for handing media to a destination.

Provider-agnostic by design (ADR-0009). The vocabulary here is
``DeliveryTarget``, ``RemoteArtifactRef`` and ``DeliveryReceipt``; the words
``chat_id``, ``file_id`` and ``message_id`` belong to one adapter and never
cross this line.

That is what makes two things possible later without touching the core: a
second destination (S3, NAS, a webhook), and re-delivering an asset that was
acquired months ago by reusing the reference the destination handed back.
"""

from __future__ import annotations
