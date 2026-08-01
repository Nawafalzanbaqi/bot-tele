"""Source Intelligence - what MediaHub knows about a source before fetching it.

This context owns the rules that apply to a *source reference* rather than to a
catalogued item: which URLs the system is willing to touch, how a provider is
identified, and what a probe may report.

Phase 03 implements only what the download engine requires:

* :mod:`~mediahub.domain.sources.policies` - the URL policy, which is the SSRF
  gate described in ``docs/architecture/14-security-architecture.md`` §14.3.
* :mod:`~mediahub.domain.sources.value_objects` - a validated URL.

Provider identity is reported by the engine as a plain string on
:class:`~mediahub.application.download.ports.MediaMetadata`. A ``ProviderId``
value object is specified in the architecture but not built here: nothing yet
consumes it, and an untested type with no caller is a liability rather than a
head start. It arrives with the catalogue's ``SourceRef``.

The policy lives in the domain, not in middleware, so that the same rule applies
to HTTP, Telegram, the CLI and automation. A control that exists in only one
interface is not a control.
"""

from __future__ import annotations
