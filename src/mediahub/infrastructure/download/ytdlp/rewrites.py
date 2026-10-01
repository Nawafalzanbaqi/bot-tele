"""Link shapes the engine does not recognise, rewritten to the shape it does.

A share sheet does not always produce the URL an extractor was written for.
The rewrite is applied at the moment the URL is handed to the engine and
nowhere else: validation, the egress policy and the journal all keep the
link the user actually sent.
"""

from __future__ import annotations

import re
from typing import Final

_SNAPCHAT_PROFILE_SPOTLIGHT: Final[re.Pattern[str]] = re.compile(
    r"^(https?://(?:www\.)?snapchat\.com)/@[^/]+/spotlight/([A-Za-z0-9_-]+)", re.IGNORECASE
)
"""``snapchat.com/@<user>/spotlight/<id>``: the form Snapchat's share sheet and
its web pages produce. yt-dlp's Spotlight extractor matches only
``snapchat.com/spotlight/<id>``, so this form fell through to the generic
extractor, which scraped a player URL with an unusual extension and refused it
"for safety reasons" (2026-10-01). Same clip, same id, canonical form: fine."""


def engine_url(url: str) -> str:
    """Return the URL to hand to the engine for ``url``."""
    match = _SNAPCHAT_PROFILE_SPOTLIGHT.match(url)
    if match is not None:
        return f"{match.group(1)}/spotlight/{match.group(2)}"
    return url
