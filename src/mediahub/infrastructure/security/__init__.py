"""Security adapters.

The *rules* live in the domain as policies; this package supplies the I/O they
need - DNS resolution today, secret redaction and API keys later. Keeping the
split means the rules stay unit-testable against hostile corpora with no
network involved.
"""

from __future__ import annotations
