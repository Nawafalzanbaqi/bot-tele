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

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

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

ERROR_MESSAGES: Final[dict[str, str]] = {
    "invalid_url": "That does not look like a link I can fetch.",
    "unsupported_url_scheme": "I can only fetch http and https links.",
    "blocked_address": "That address is not reachable from here.",
    "unsupported_provider": "I do not know how to handle that site.",
    "metadata_unavailable": (
        "I could not read that link. It may be private, removed or region-locked."
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
        "/history — what you have fetched recently"
    )


def render_source(summary: SourceSummary) -> str:
    """Render what was found at a link, before anything is downloaded."""
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
    elif summary.qualities:
        lines.append("\nChoose a quality:")
    else:
        lines.append("\nNothing here can be fetched.")

    return "\n".join(lines)


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
