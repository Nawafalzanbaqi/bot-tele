"""PornHub with its browser challenge solved in deno instead of PhantomJS.

PornHub sometimes answers a page request with a 1.5 kB interstitial: a script
that derives a number from two constants, sets ``KEY=<n>*<p/n>:<s>:...`` as a
cookie and reloads. yt-dlp's extractor recognises that page and hands it to
PhantomJS, a dead project with no arm64 build; on this deployment every such
page ended in "PhantomJS not found" (2026-10-01, every exit including the
tunnel). The script needs nothing a browser has - ``Math``, ``isNaN``, a
``document.cookie`` to write to - so it runs in deno, which the image already
carries for YouTube, in well under a second.

Everything else is the built-in extractor: this class subclasses it and
replaces only the page fetch, so the weekly yt-dlp bump keeps improving the
rest. The cookie is set on the request's own host, as the page would, and the
page is fetched once more.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.pornhub import PornHubIE
from yt_dlp.networking import Request
from yt_dlp.utils import ExtractorError

CHALLENGE_MARKERS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"<body\b[^>]+\bonload=[\"']go\(\)"),
    re.compile(r"document\.cookie\s*=\s*[\"'](?:RN)?KEY="),
    re.compile(r"document\.location\.reload\(true\)"),
)
"""The same three signatures yt-dlp's extractor looks for."""

_SCRIPT: Final[re.Pattern[str]] = re.compile(r"<script[^>]*>(.*?)</script>", re.S)
_COOKIE: Final[re.Pattern[str]] = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)=([^;\r\n]+)")
_SHIM: Final[str] = (
    "globalThis.document = { cookie: '', location: { reload() {} } };\n"
    "globalThis.window = globalThis;\n"
)
_RUNNER: Final[str] = "\ngo();\nconsole.log(document.cookie);\n"
DENO_TIMEOUT_SECONDS: Final[float] = 20.0
MAX_CHALLENGE_ROUNDS: Final[int] = 3
"""How many consecutive interstitials are solved before giving up.

The site ties a challenge to the connection that received it; through a
tunnel whose exit may change between requests the next page can be a fresh
challenge. Three rounds covers that; more would only delay an honest answer."""


def is_challenge(webpage: str) -> bool:
    """Return whether the page is the interstitial rather than the video page."""
    return any(marker.search(webpage) for marker in CHALLENGE_MARKERS)


def challenge_script(webpage: str) -> str | None:
    """Return the challenge's JavaScript, without its HTML comment wrapper."""
    for match in _SCRIPT.finditer(webpage):
        script = match.group(1).replace("<!--", "").replace("//-->", "").strip()
        if "function go()" in script:
            return script
    return None


def solve_challenge(webpage: str, run_js: Callable[[str], str]) -> tuple[str, str] | None:
    """Run the challenge and return the ``(name, value)`` cookie it sets, or ``None``."""
    script = challenge_script(webpage)
    if script is None:
        return None
    output = run_js(_SHIM + script + _RUNNER)
    match = _COOKIE.match(output or "")
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def run_with_deno(code: str, *, timeout: float = DENO_TIMEOUT_SECONDS) -> str:
    """Execute ``code`` in deno with no permissions and return its stdout.

    The script is written to a file in a private temporary directory, which
    also serves as deno's cache, so nothing is read or written anywhere else.
    """
    deno = shutil.which("deno")
    if deno is None:
        message = "deno is not installed; the PornHub challenge cannot be solved"
        raise ExtractorError(message, expected=True)
    with tempfile.TemporaryDirectory(prefix="mediahub-challenge-") as directory:
        script = Path(directory) / "challenge.js"
        script.write_text(code, encoding="utf-8")
        env = dict(os.environ, DENO_DIR=str(Path(directory) / "cache"), NO_COLOR="1")
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, script we wrote
            [deno, "run", "--no-prompt", "--quiet", str(script)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    if completed.returncode != 0:
        message = f"deno could not run the PornHub challenge: {completed.stderr.strip()[:200]}"
        raise ExtractorError(message)
    return completed.stdout


class PornHubMediahubIE(PornHubIE):  # type: ignore[misc]
    """The built-in PornHub extractor, challenge solved in deno."""

    IE_NAME = "pornhub:mediahub"

    def _download_webpage_handle(self, *args: Any, **kwargs: Any) -> Any:
        """Fetch a page; when it is the challenge, solve it and fetch again."""
        result = InfoExtractor._download_webpage_handle(self, *args, **kwargs)
        for _round in range(MAX_CHALLENGE_ROUNDS):
            if not result:
                return result
            webpage, _handle = result
            if not isinstance(webpage, str) or not is_challenge(webpage):
                return result
            solved = solve_challenge(webpage, run_with_deno)
            if solved is None:
                message = "PornHub answered with a browser challenge this extractor could not read"
                raise ExtractorError(message, expected=True)
            name, value = solved
            target = args[0]
            url = target.url if isinstance(target, Request) else str(target)
            host = urlparse(url).hostname or "www.pornhub.com"
            self._set_cookie(host, name, value)
            self.to_screen("Solved the browser challenge with deno; fetching the page again")
            result = InfoExtractor._download_webpage_handle(self, *args, **kwargs)
        if result and isinstance(result[0], str) and is_challenge(result[0]):
            message = "PornHub kept answering with its browser challenge after it was solved"
            raise ExtractorError(message, expected=True)
        return result
