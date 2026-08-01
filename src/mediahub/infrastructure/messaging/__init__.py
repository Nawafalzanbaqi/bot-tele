"""Adapters for publishing domain events.

Today the only implementation writes events to the log, which is enough to
audit what the system did and to debug it. The port
(:class:`~mediahub.application.common.ports.EventPublisher`) is deliberately
async so that swapping in a broker - Redis streams, NATS, RabbitMQ - is an
infrastructure change and nothing more.
"""

from __future__ import annotations
