"""Renders application DTOs as text for a chat.

Every function here is pure: DTO in, string out. That is what makes the
gateway's user-visible behaviour testable without a network, and it is where
the rule "never expose internals" is actually enforced.

Error rendering deserves particular care. Application errors carry messages
that may contain a URL, a provider name or an engine's own words; none of that
belongs in a chat. :func:`render_error` therefore renders from the error's
stable **code** and falls back to a generic sentence for anything unrecognised.
An unmapped error tells the user something went wrong and tells the log
everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from mediahub.application.credentials.ports import CookieSummary
    from mediahub.application.delivery.ports import DeliveryProgress
    from mediahub.application.download.dto import (
        AcquisitionSummary,
        CapabilitiesSummary,
        HistoryEntrySummary,
        SourceSummary,
    )
    from mediahub.application.download.ports import DownloadProgress

_KIB: Final[int] = 1024
_BAR_WIDTH: Final[int] = 12
_MAX_TITLE: Final[int] = 120
_MAX_SITES_SHOWN: Final[int] = 8
"""Enough to confirm the right export; short enough to stay one readable line."""

ERROR_MESSAGES: Final[dict[str, str]] = {
    "invalid_url": "That does not look like a link I can fetch.",
    "invalid_cookie_jar": (
        "That is not a usable cookie file. Export it in Netscape format - most "
        "cookie-exporter extensions offer that, and it is a text file with one "
        "cookie per line."
    ),
    "cookie_store_unavailable": (
        "I have nowhere to keep cookies. Set MEDIAHUB_DOWNLOAD__COOKIES_FILE and " "restart me."
    ),
    "upload_failed": "I could not read that file. Try sending it again.",
    "unsupported_url_scheme": "I can only fetch http and https links.",
    "blocked_address": "That address is not reachable from here.",
    "unsupported_provider": (
        "I do not know how to handle that site. Check the link is a direct link "
        "to one post or video."
    ),
    "authentication_required": (
        "🔒 This needs a signed-in session.\n\n"
        "The post is probably there — the site just will not show it to a "
        "logged-out visitor."
    ),
    "geo_restricted": (
        "🌍 This is not published in this device's country.\n\n"
        "Cookies will not help; the block is on where the machine is."
    ),
    "content_removed": (
        "🗑 That no longer exists — deleted, suspended, or the link is wrong.\n\n"
        "Check the link opens in your own browser."
    ),
    "rate_limited": (
        "⏳ The site is asking me to slow down. Wait a few minutes and send it " "again."
    ),
    "metadata_unavailable": (
        "I could not read that link. It may be private, age-restricted or " "behind a paywall."
    ),
    "format_unavailable": "That quality is no longer available. Send the link again.",
    "provider_error": "The site is having trouble right now. Try again in a few minutes.",
    "download_failed": "The download did not finish. Try again in a few minutes.",
    "download_timeout": "That took too long and was stopped.",
    "download_cancelled": "Cancelled.",
    "size_limit_exceeded": "That file is larger than I can handle.",
    "artifact_too_large": "That file is too large to send here.",
    "delivery_rate_limited": "The destination asked me to slow down. Try again shortly.",
    "delivery_quota_exceeded": "The destination has no room right now.",
    "provider_unavailable": "The destination is unavailable. Try again in a few minutes.",
    "delivery_authentication_failed": "I could not authenticate with the destination.",
    "reference_not_usable": "That item can no longer be re-sent. Send the link again.",
    "resend_not_supported": "This destination cannot re-send items.",
    "no_provider_for_target": "There is no destination configured for that.",
    "live_source_not_allowed": "Live streams are not supported.",
    "playlist_not_allowed": "That link is a playlist. Send a link to a single item.",
    "insufficient_disk_space": "There is not enough free space right now. Try again later.",
    "delivery_target_unreachable": "I could not send it to this chat.",
    "delivery_provider_error": "Sending failed. Try again in a few minutes.",
    "downloader_not_configured": "Downloading is not enabled on this instance.",
    "delivery_not_configured": "Sending is not enabled on this instance.",
    "permission_denied": "You are not authorised to use this service.",
    "quota_exceeded": "You have reached your limit for now.",
}

GENERIC_ERROR: Final[str] = "Something went wrong. Please try again."


def render_start(display_name: str | None) -> str:
    """Render the greeting."""
    who = f", {display_name}" if display_name else ""
    return (
        f"Hello{who}. Send me a link and I will fetch it for you.\n\n"
        "Use /help to see what I understand."
    )


def render_help() -> str:
    """Render the command reference."""
    return (
        "Send me a link and I will show you what is there, then ask which "
        "quality you want.\n\n"
        "/start — say hello\n"
        "/help — this message\n"
        "/settings — what this instance can do\n"
        "/history — what you have fetched recently\n"
        "/cookies — show the stored sign-in cookies (owner only)\n\n"
        "Some sites — X, TikTok, private Instagram — show nothing to a "
        "logged-out visitor. Send me a Netscape-format cookies.txt and I will "
        "use it. I delete the message afterwards."
    )


def render_source(summary: SourceSummary, *, automatic: bool = False) -> str:
    """Render what was found at a link, before anything is downloaded.

    Args:
        summary: What the probe reported.
        automatic: Whether acquisition is already starting at the best
            deliverable quality. The line changes from an instruction to a
            statement, because telling someone to choose when no keyboard is
            coming is the worst of both.
    """
    lines = [f"*{_escape(_clip(summary.title, _MAX_TITLE))}*"]

    facts: list[str] = [summary.provider]
    if summary.duration_seconds is not None:
        facts.append(_duration(summary.duration_seconds))
    if summary.expected_bytes is not None:
        facts.append(_bytes(summary.expected_bytes))
    lines.append(" · ".join(facts))

    if summary.is_live:
        lines.append("\nThis is a live stream and cannot be fetched.")
    elif summary.is_playlist:
        lines.append("\nThis link is a collection. Send a link to a single item.")
    elif not summary.qualities:
        lines.append("\nNothing here can be fetched.")
    elif automatic:
        lines.append("\nFetching at the highest quality that will send…")
    else:
        lines.append("\nChoose a quality:")

    return "\n".join(lines)


def render_queued(title: str) -> str:
    """Say that a request is waiting for a free slot.

    A silent wait is indistinguishable from a bot that dropped the message, and
    the natural response to that is to send the link again - which is how a
    queue of one becomes a queue of three.
    """
    return f"*{_escape(_clip(title, _MAX_TITLE))}*\nWaiting for a free slot…"


def render_progress(progress: DownloadProgress, *, title: str) -> str:
    """Render a progress line for an in-flight download."""
    header = f"*{_escape(_clip(title, _MAX_TITLE))}*"
    percent = progress.percentage

    if percent is None:
        detail = _bytes(progress.downloaded_bytes)
        return f"{header}\n{progress.stage.value}… {detail}"

    filled = int(percent / 100 * _BAR_WIDTH)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    parts = [f"{bar} {percent:.0f}%"]
    if progress.total_bytes:
        parts.append(f"{_bytes(progress.downloaded_bytes)} / {_bytes(progress.total_bytes)}")
    if progress.speed_bps:
        parts.append(f"{_bytes(int(progress.speed_bps))}/s")
    if progress.eta_seconds:
        parts.append(f"~{_duration(progress.eta_seconds)} left")

    return f"{header}\n{' · '.join(parts)}"


def render_delivery_progress(progress: DeliveryProgress, *, title: str) -> str:
    """Render a progress line for an in-flight upload.

    Kept separate from download progress because the two are different halves
    of the operation and a user watching a slow upload deserves to be told that
    is what is happening.
    """
    header = f"*{_escape(_clip(title, _MAX_TITLE))}*"
    percent = progress.percentage

    if percent is None:
        return f"{header}\nSending… {_bytes(progress.sent_bytes)}"

    filled = int(percent / 100 * _BAR_WIDTH)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    parts = [f"{bar} {percent:.0f}%"]
    if progress.total_bytes:
        parts.append(f"{_bytes(progress.sent_bytes)} / {_bytes(progress.total_bytes)}")
    return f"{header}\nSending · {' · '.join(parts)}"


def render_delivered(summary: AcquisitionSummary) -> str:
    """Render the confirmation shown once the file has been sent."""
    return (
        f"*{_escape(_clip(summary.title, _MAX_TITLE))}*\n"
        f"Sent · {summary.quality_label} · {_bytes(summary.bytes_delivered)} · "
        f"{_duration(summary.elapsed_seconds)}"
    )


def render_history(entries: Sequence[HistoryEntrySummary]) -> str:
    """Render a principal's recent acquisitions."""
    if not entries:
        return "You have not fetched anything yet."

    lines = ["*Recent*"]
    lines.extend(
        f"• {_escape(_clip(entry.title, 60))} — {entry.quality_label} · "
        f"{_bytes(entry.bytes_delivered)} · {entry.delivered_at:%Y-%m-%d %H:%M} UTC"
        for entry in entries
    )
    return "\n".join(lines)


def render_settings(capabilities: CapabilitiesSummary) -> str:
    """Render what this instance can currently do."""
    return (
        "*This instance*\n"
        f"Engine · {capabilities.engine} {capabilities.engine_version}\n"
        f"Sends to · {capabilities.delivery_provider}\n"
        f"Largest file · {_bytes(capabilities.effective_max_bytes)}\n"
        f"Audio only · {'yes' if capabilities.supports_audio_only else 'no'}\n"
        f"Live streams · {'yes' if capabilities.allow_live else 'no'}\n"
        f"Playlists · {'yes' if capabilities.allow_playlist else 'no'}"
    )


def render_error(code: str) -> str:
    """Render a user-facing message for an error code.

    Only the code is consulted. The error's own message may contain a URL, a
    file path or a provider's internal wording, none of which belongs in a
    chat.
    """
    return ERROR_MESSAGES.get(code, GENERIC_ERROR)


def _bytes(size: int) -> str:
    """Render a byte count in binary units."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < _KIB or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= _KIB
    return f"{value:.1f} TiB"  # pragma: no cover - unreachable guard


def _duration(seconds: float) -> str:
    """Render a duration compactly."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _clip(text: str, limit: int) -> str:
    """Trim text to ``limit`` characters, with an ellipsis when trimmed."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _escape(text: str) -> str:
    """Neutralise the characters that would break simple markdown.

    Titles are attacker-controlled: a well-placed asterisk or underscore
    otherwise mangles every message that follows it.
    """
    for character in ("*", "_", "`", "[", "]"):
        text = text.replace(character, "")
    return text


# -- Credentials -------------------------------------------------------------
#
# Everything below reports on a cookie jar without ever rendering one. Counts,
# domains and dates answer the questions a person actually has - did the right
# site get exported, and when will it stop working - and none of them is a
# credential.


COOKIE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "x.com",
        "twitter.com",
        "tiktok.com",
        "vm.tiktok.com",
        "instagram.com",
        "facebook.com",
        "fb.watch",
        "threads.net",
        "reddit.com",
    }
)
"""Hosts that routinely refuse a logged-out visitor.

Used only to make a failure explain itself. Without this the user sees "I could
not read that link", concludes the bot is broken, and has no way to discover
that thirty seconds of exporting cookies would fix it.
"""

_SESSION_FAILURES: Final[frozenset[str]] = frozenset(
    {"metadata_unavailable", "unsupported_provider", "download_failed", "provider_error"}
)
"""Codes that *may* mean a missing session. All have innocent causes too, so on
these the advice is offered only for a host known to gate on one.

``authentication_required`` is deliberately absent: it is not a maybe."""


def render_source_failure(code: str, *, url: str, cookies: CookieSummary | None) -> str:
    """Render a probe failure together with what is likely behind it.

    Args:
        code: The stable error code.
        url: What the user sent, used only to recognise the host.
        cookies: The stored jar, if any. Three states produce three different
            instructions, and getting this wrong wastes real time: with no jar
            the answer is to export one; with an *expired* jar the answer is to
            replace it; with a live jar for other sites the answer is that this
            particular site is not covered by it.
    """
    base = render_error(code)
    certain = code == "authentication_required"
    if not certain and (code not in _SESSION_FAILURES or not _needs_session(url)):
        return base

    if cookies is None:
        return f"{base}\n\n{_ADVICE_NO_JAR}"
    if _has_lapsed(cookies):
        when = f"{cookies.earliest_expiry:%Y-%m-%d}" if cookies.earliest_expiry else "recently"
        return (
            f"{base}\n\n⚠️ Your stored cookies expired on {when}. Send me a "
            "fresh export from a signed-in browser."
        )
    site = _host_of(url)
    if site and not _covered_by(cookies, site):
        covered = ", ".join(cookies.domains[:4]) or "nothing"
        return (
            f"{base}\n\nMy stored cookies cover {covered} — not {site}. Export "
            f"a cookies.txt while {site} is open and send it to me."
        )
    return (
        f"{base}\n\nMy cookies for this site may have stopped working. Send a "
        "fresh export, or /cookies to see what is stored."
    )


_ADVICE_NO_JAR: Final[str] = (
    "I have no sign-in cookies stored. Export a Netscape-format cookies.txt "
    "from a browser that is signed in to this site and send me the file — "
    "see /help."
)


def _has_lapsed(cookies: CookieSummary) -> bool:
    """Return whether the stored jar's first cookie has already expired."""
    return cookies.earliest_expiry is not None and cookies.earliest_expiry < datetime.now(UTC)


def _covered_by(cookies: CookieSummary, host: str) -> bool:
    """Return whether the stored jar holds anything for ``host``.

    Exporting the wrong browser tab is the mistake people actually make, and it
    is invisible: the jar installs cleanly, reports a healthy cookie count, and
    does nothing for the site being asked about.
    """
    return any(host == domain or host.endswith(f".{domain}") for domain in cookies.domains)


def _host_of(url: str) -> str:
    """Return a URL's host, without ``www.``."""
    authority = url.split("//", maxsplit=1)[-1]
    host = authority.split("/", maxsplit=1)[0].split("?", maxsplit=1)[0].lower()
    return host.removeprefix("www.")


def _needs_session(url: str) -> bool:
    """Return whether a URL's host is one that refuses logged-out visitors."""
    authority = url.split("//", maxsplit=1)[-1]
    host = authority.split("/", maxsplit=1)[0].split("?", maxsplit=1)[0].lower()
    host = host.removeprefix("www.")
    return any(host == known or host.endswith(f".{known}") for known in COOKIE_HOSTS)


def render_cookie_status(summary: CookieSummary | None) -> str:
    """Describe the stored cookie jar."""
    if summary is None:
        return (
            "No sign-in cookies stored.\n\n"
            "Sites that hide media from logged-out visitors — X, TikTok, "
            "private Instagram — will keep refusing. Send me a Netscape-format "
            "cookies.txt to fix that."
        )

    lines = [
        "🍪 *Sign-in cookies stored*",
        f"Cookies: {summary.cookie_count}",
        f"Sites: {_sites(summary)}",
        f"Updated: {summary.installed_at:%Y-%m-%d %H:%M} UTC",
        _expiry_line(summary),
        "",
        "Send a new file to replace it, or /cookies clear to remove it.",
    ]
    return "\n".join(line for line in lines if line is not None)


def render_cookies_installed(summary: CookieSummary, *, removed: bool) -> str:
    """Confirm a jar was stored, and say what it covers.

    The site list is the useful part: exporting the wrong tab is the mistake
    people actually make, and it otherwise shows up days later as "the bot
    still cannot fetch X".
    """
    lines = [
        "✅ *Sign-in cookies updated*",
        f"Cookies: {summary.cookie_count}",
        f"Sites: {_sites(summary)}",
        _expiry_line(summary),
        "",
        (
            "I deleted your upload."
            if removed
            else "⚠️ I could not delete your upload — please delete it yourself."
        ),
    ]
    return "\n".join(line for line in lines if line is not None)


def render_cookies_discarded(*, removed: bool) -> str:
    """Confirm the jar is gone."""
    if removed:
        return "Sign-in cookies removed. I will browse anonymously from now on."
    return "There were no sign-in cookies stored."


def render_cookie_too_large(declared: int, limit: int) -> str:
    """Refuse an upload before any of it is transferred."""
    return (
        f"That file is {_bytes(declared)} and I accept up to {_bytes(limit)}. "
        "A cookies.txt is normally a few kilobytes — check you exported "
        "cookies rather than something else."
    )


def _sites(summary: CookieSummary) -> str:
    """Render the domains a jar covers, bounded so it stays readable."""
    shown = summary.domains[:_MAX_SITES_SHOWN]
    if not shown:
        return "unknown"
    extra = len(summary.domains) - len(shown)
    suffix = f" (+{extra} more)" if extra else ""
    return ", ".join(shown) + suffix


def _expiry_line(summary: CookieSummary) -> str | None:
    """Render when the jar starts to lapse, or nothing if it never declares.

    A jar does not fail loudly - the platform simply starts refusing again -
    so this turns a future mystery into a date.
    """
    if summary.earliest_expiry is None:
        return None
    days = (summary.earliest_expiry - datetime.now(UTC)).days
    if days < 0:
        return "⚠️ The first cookie has already expired — send a fresh export."
    return f"First expiry: in {days} day{'s' if days != 1 else ''}"
