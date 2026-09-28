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

from mediahub.application.credentials.ports import SESSION_COOKIES

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

_STAGE_LABELS: Final[dict[str, str]] = {
    "validating": "جارٍ الفحص",
    "selecting": "اختيار الجودة",
    "downloading": "جارٍ التحميل",
    "processing": "جارٍ المعالجة",
    "verifying": "جارٍ التحقّق",
    "completed": "اكتمل",
}
"""Engine stage names, in the language the replies are written in."""
"""Enough to confirm the right export; short enough to stay one readable line."""

ERROR_MESSAGES: Final[dict[str, str]] = {
    # Every entry answers two questions, in this order: what happened, and what
    # to do about it. A message that only names the fault leaves the reader
    # re-sending the same link and hoping.
    "invalid_url": (
        "هذا لا يبدو رابطًا أستطيع جلبه.\n\n" "تأكد أنك نسخت الرابط كاملًا وأنه يبدأ بـ http."
    ),
    "invalid_cookie_jar": (
        "⚠️ هذا ليس ملف كوكيز صالحًا.\n\n"
        "صدّره بصيغة Netscape — أغلب إضافات المتصفح تدعمها، وهو ملف نصي "
        "فيه سطر لكل كوكي."
    ),
    "cookie_store_unavailable": (
        "لا يوجد مكان لحفظ الكوكيز.\n\n" "اضبط MEDIAHUB_DOWNLOAD__COOKIES_FILE ثم أعد تشغيلي."
    ),
    "upload_failed": "تعذّرت قراءة الملف. أرسله مرة أخرى.",
    "unsupported_url_scheme": "أستطيع جلب روابط http و https فقط.",
    "blocked_address": "هذا العنوان غير مسموح بالوصول إليه من هنا.",
    "unsupported_provider": (
        "لا أعرف كيف أتعامل مع هذا الموقع.\n\n"
        "تأكد أن الرابط يشير إلى مقطع أو منشور واحد، لا إلى صفحة حساب أو قائمة."
    ),
    "authentication_required": (
        "🔒 هذا المحتوى يحتاج تسجيل دخول.\n\n"
        "المنشور موجود على الأغلب، لكن الموقع لا يعرضه لزائر غير مسجّل."
    ),
    "no_playable_media": (
        "🖼 هذا المنشور ليس فيه فيديو ولا صوت — صور فقط.\n\n"
        "محرّك التحميل يجلب الفيديو والصوت، والصور الثابتة في منشورات X "
        "وإنستقرام وتيك توك لا تُعرَض له إطلاقًا.\n\n"
        "💡 الحل: افتح المنشور، اضغط على الصورة، انسخ رابط الصورة نفسها "
        "وأرسله لي — الروابط المباشرة للصور تعمل."
    ),
    "geo_restricted": (
        "🌍 هذا المحتوى غير منشور في دولة هذا الجهاز.\n\n"
        "الكوكيز لن تفيد — الحجب مبني على موقع الجهاز نفسه."
    ),
    "content_removed": (
        "🗑 هذا المحتوى لم يعد موجودًا — محذوف أو الحساب موقوف أو الرابط خطأ.\n\n"
        "جرّب فتح الرابط في متصفحك للتأكد."
    ),
    "connection_blocked": (
        "🚧 الاتصال بالموقع يُقطع قبل أن يرسل شيئًا.\n\n"
        "هذا حجب في الشبكة بين الجهاز والموقع، وليس عطلًا في الموقع.\n\n"
        "💡 الحل: فعّل نفق الخروج — اضبط MEDIAHUB_DOWNLOAD__PROXY."
    ),
    "rate_limited": (
        "⏳ الموقع يطلب منّي التمهّل.\n\n" "انتظر دقائق قليلة ثم أرسل الرابط مرة أخرى."
    ),
    "metadata_unavailable": (
        "تعذّرت قراءة هذا الرابط.\n\n" "قد يكون خاصًّا، أو مقيّدًا بالعمر، أو خلف اشتراك مدفوع."
    ),
    "format_unavailable": ("هذه الجودة لم تعد متاحة.\n\nأرسل الرابط مرة أخرى لقائمة جديدة."),
    "provider_error": "الموقع يواجه مشكلة مؤقتة الآن.\n\nجرّب بعد دقائق.",
    "download_failed": (
        "لم يكتمل التحميل.\n\n" "جرّب بعد دقائق؛ إن تكرّر فالمصدر نفسه هو المشكلة."
    ),
    "download_timeout": "استغرق وقتًا أطول ممّا يجب فأوقفته.",
    "download_cancelled": "تمّ الإلغاء.",
    "size_limit_exceeded": "الملف أكبر ممّا أستطيع تحميله.",
    "artifact_too_large": "الملف أكبر ممّا يقبله تلجرام هنا.",
    "delivery_rate_limited": "تلجرام يطلب التمهّل. جرّب بعد قليل.",
    "delivery_quota_exceeded": "لا يوجد متّسع لدى الوجهة الآن.",
    "provider_unavailable": "الوجهة غير متاحة. جرّب بعد دقائق.",
    "delivery_authentication_failed": "فشل التحقّق مع تلجرام.",
    "reference_not_usable": ("لم يعد ممكنًا إعادة إرسال هذا الملف. أرسل الرابط من جديد."),
    "resend_not_supported": "هذه الوجهة لا تدعم إعادة الإرسال.",
    "no_provider_for_target": "لا توجد وجهة مضبوطة لهذا.",
    "live_source_not_allowed": (
        "البث المباشر غير مدعوم.\n\n" "انتظر انتهاء البث ثم أرسل رابط التسجيل."
    ),
    "playlist_not_allowed": (
        "هذا الرابط قائمة وليس مقطعًا واحدًا.\n\n" "افتح المقطع الذي تريده وانسخ رابطه وحده."
    ),
    "insufficient_disk_space": (
        "لا توجد مساحة كافية الآن.\n\n" "انتظر انتهاء التحميلات الجارية ثم أعد المحاولة."
    ),
    "delivery_target_unreachable": "تعذّر الإرسال إلى هذه المحادثة.",
    "delivery_provider_error": "فشل الإرسال. جرّب بعد دقائق.",
    "downloader_not_configured": "التحميل غير مفعّل على هذه النسخة.",
    "delivery_not_configured": "الإرسال غير مفعّل على هذه النسخة.",
    "permission_denied": "غير مصرّح لك باستخدام هذه الخدمة.",
    "quota_exceeded": "وصلت إلى حدّك المسموح حاليًا.",
}

GENERIC_ERROR: Final[str] = "حدث خطأ غير متوقّع. جرّب مرة أخرى، وإن تكرّر فأبلِغني بالرابط."


def render_start(display_name: str | None) -> str:
    """Render the greeting."""
    who = f" يا {display_name}" if display_name else ""
    return f"أهلًا{who}. أرسل لي رابطًا وسأجلبه لك.\n\n" "اكتب /help لتعرف ما أفهمه."


def render_help() -> str:
    """Render the command reference."""
    return (
        "أرسل لي رابطًا وسأجلبه بأعلى جودة متاحة تلقائيًا.\n\n"
        "/start — تحية\n"
        "/help — هذه الرسالة\n"
        "/settings — ما تستطيعه هذه النسخة\n"
        "/history — ما حمّلته مؤخرًا\n"
        "/cookies — الكوكيز المحفوظة (للمالك فقط)\n\n"
        "بعض المواقع — X وتيك توك وإنستقرام الخاص — لا تعرض شيئًا لزائر غير "
        "مسجّل. أرسل لي ملف cookies.txt بصيغة Netscape وسأستخدمه.\n\n"
        "لكل منصة ملفها المستقل: أرسل ملف كل منصة على حدة، وسأدمجه بجانب "
        "البقية دون أن يمحوها. وأحذف رسالتك بعد الحفظ."
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
    lines = [_clip(summary.title, _MAX_TITLE)]

    facts: list[str] = [summary.provider]
    if summary.duration_seconds is not None:
        facts.append(_duration(summary.duration_seconds))
    if summary.expected_bytes is not None:
        facts.append(_bytes(summary.expected_bytes))
    lines.append(" · ".join(facts))

    if summary.is_live:
        lines.append("\nهذا بث مباشر ولا يمكن جلبه.")
    elif summary.is_playlist:
        lines.append("\nهذا الرابط قائمة. أرسل رابط مقطع واحد.")
    elif not summary.qualities:
        lines.append("\nلا يوجد هنا ما يمكن جلبه.")
    elif automatic:
        lines.append("\n⏳ جارٍ التحميل بأعلى جودة يمكن إرسالها…")
    else:
        lines.append("\nاختر الجودة:")

    return "\n".join(lines)


def render_queued(title: str) -> str:
    """Say that a request is waiting for a free slot.

    A silent wait is indistinguishable from a bot that dropped the message, and
    the natural response to that is to send the link again - which is how a
    queue of one becomes a queue of three.
    """
    return f"{_clip(title, _MAX_TITLE)}\n⏸ في الانتظار حتى يفرغ مسار…"


def render_progress(progress: DownloadProgress, *, title: str) -> str:
    """Render a progress line for an in-flight download."""
    header = _clip(title, _MAX_TITLE)
    percent = progress.percentage

    if percent is None:
        detail = _bytes(progress.downloaded_bytes)
        return f"{header}\n{_STAGE_LABELS.get(progress.stage.value, 'جارٍ')}… {detail}"

    filled = int(percent / 100 * _BAR_WIDTH)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    parts = [f"{bar} {percent:.0f}%"]
    if progress.total_bytes:
        parts.append(f"{_bytes(progress.downloaded_bytes)} / {_bytes(progress.total_bytes)}")
    if progress.speed_bps:
        parts.append(f"{_bytes(int(progress.speed_bps))}/s")
    if progress.eta_seconds:
        parts.append(f"يتبقّى ~{_duration(progress.eta_seconds)}")

    return f"{header}\n⬇️ {' · '.join(parts)}"


def render_delivery_progress(progress: DeliveryProgress, *, title: str) -> str:
    """Render a progress line for an in-flight upload.

    Kept separate from download progress because the two are different halves
    of the operation and a user watching a slow upload deserves to be told that
    is what is happening.
    """
    header = _clip(title, _MAX_TITLE)
    percent = progress.percentage

    if percent is None:
        return f"{header}\n⬆️ جارٍ الإرسال… {_bytes(progress.sent_bytes)}"

    filled = int(percent / 100 * _BAR_WIDTH)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    parts = [f"{bar} {percent:.0f}%"]
    if progress.total_bytes:
        parts.append(f"{_bytes(progress.sent_bytes)} / {_bytes(progress.total_bytes)}")
    return f"{header}\n⬆️ إرسال · {' · '.join(parts)}"


def render_delivered(summary: AcquisitionSummary) -> str:
    """Render the confirmation shown once the file has been sent."""
    return (
        f"{_clip(summary.title, _MAX_TITLE)}\n"
        f"✅ تم الإرسال · {summary.quality_label} · "
        f"{_bytes(summary.bytes_delivered)} · {_duration(summary.elapsed_seconds)}\n"
        "🗑 حُذفت النسخة من الجهاز."
    )


def render_history(entries: Sequence[HistoryEntrySummary]) -> str:
    """Render a principal's recent acquisitions."""
    if not entries:
        return "لم تحمّل شيئًا بعد."

    lines = ["آخر ما حمّلت:"]
    lines.extend(
        f"• {_clip(entry.title, 60)} — {entry.quality_label} · "
        f"{_bytes(entry.bytes_delivered)} · {entry.delivered_at:%Y-%m-%d %H:%M} UTC"
        for entry in entries
    )
    return "\n".join(lines)


def render_settings(capabilities: CapabilitiesSummary) -> str:
    """Render what this instance can currently do."""
    yes, no = "نعم", "لا"
    return (
        "هذه النسخة:\n"
        f"المحرّك · {capabilities.engine} {capabilities.engine_version}\n"
        f"يرسل إلى · {capabilities.delivery_provider}\n"
        f"أكبر ملف · {_bytes(capabilities.effective_max_bytes)}\n"
        f"صوت فقط · {yes if capabilities.supports_audio_only else no}\n"
        f"بث مباشر · {yes if capabilities.allow_live else no}\n"
        f"القوائم · {yes if capabilities.allow_playlist else no}"
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
        return f"{hours}س {minutes:02d}د"
    if minutes:
        return f"{minutes}د {secs:02d}ث"
    return f"{secs}ث"


def _clip(text: str, limit: int) -> str:
    """Trim text to ``limit`` characters, with an ellipsis when trimmed."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


# Messages are sent as plain text - the client never sets a parse mode - so
# titles need no markdown escaping and are shown exactly as the source named
# them. The previous ``*title*`` wrapping was rendered literally, asterisks and
# all, on every message.


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
    site = _host_of(url)

    if code == "no_playable_media":
        return _render_no_playable_media(base, site=site, cookies=cookies)

    certain = code == "authentication_required"
    if not certain and (code not in _SESSION_FAILURES or not _needs_session(url)):
        return base
    return f"{base}\n\n{_session_advice(site, cookies)}"


def _session_advice(site: str, cookies: CookieSummary | None) -> str:
    """Return the one instruction that actually applies to this jar.

    Four states, four different answers, and giving the wrong one wastes real
    time: no jar at all, a jar that has lapsed, a jar that covers other sites,
    and - the invisible one - a jar that lists this site but carries no login
    session for it.
    """
    if cookies is None:
        return _ADVICE_NO_JAR
    if _has_lapsed(cookies):
        when = f"{cookies.earliest_expiry:%Y-%m-%d}" if cookies.earliest_expiry else "مؤخرًا"
        return (
            f"⚠️ الكوكيز المحفوظة انتهت في {when}.\n"
            "💡 الحل: صدّر ملفًا جديدًا من متصفح مسجّل الدخول وأرسله لي."
        )
    if site and not _covered_by(cookies, site):
        covered = "، ".join(cookies.domains[:4]) or "لا شيء"
        return (
            f"الكوكيز المحفوظة تغطّي {covered} — وليس {site}.\n"
            f"💡 الحل: صدّر cookies.txt و{site} مفتوح وأرسله لي."
        )
    # Covered but not signed in. This is the failure that otherwise has no
    # explanation at all: the jar lists the site, reports a healthy cookie
    # count, and holds nothing that says who you are - which is what an export
    # that skipped httpOnly cookies produces. Everything looks right and
    # nothing works.
    if site and not _is_signed_in(cookies, site):
        return (
            f"⚠️ الكوكيز المحفوظة لـ {site} لا تحتوي على جلسة دخول.\n\n"
            "الملف يذكر الموقع لكنه لا يحمل كوكي تسجيل الدخول — غالبًا لأن "
            "الإضافة كانت مضبوطة على تخطّي كوكيز httpOnly.\n\n"
            "💡 الحل: صدّر الملف مرة أخرى مع تفعيل تضمين httpOnly، وأنت مسجّل دخول."
        )
    return (
        "قد تكون الكوكيز المحفوظة لهذا الموقع توقّفت عن العمل.\n"
        "💡 الحل: أرسل ملفًا جديدًا، أو اكتب /cookies لترى المحفوظ."
    )


def _render_no_playable_media(base: str, *, site: str, cookies: CookieSummary | None) -> str:
    """Add the one caveat that "no video in this post" genuinely carries.

    Session state changes the *meaning* of this failure rather than the advice.
    Signed in, it is simply a photo post and there is nothing to fix. Signed
    out, a restricted video looks exactly the same from here - so the
    possibility is named once, without pretending to know which it was.
    """
    if site and _needs_session(site) and not _is_signed_in(cookies, site):
        return (
            f"{base}\n\n⚠️ وليس لديّ جلسة دخول لـ {site}. لو كنت تتوقّع فيديو "
            "هنا، فالمقطع المقيّد يبدو بنفس الشكل تمامًا — أرسل لي ملف "
            "cookies.txt محدّثًا وجرّب مرة أخرى."
        )
    return base


def _is_signed_in(cookies: CookieSummary | None, host: str) -> bool:
    """Return whether a login session is stored for ``host``.

    A platform whose sign-in cookie this system cannot name is treated as
    signed in, so an unrecognised site never provokes advice about a cookie
    nobody can check for.
    """
    if cookies is None:
        return False
    known = any(host == name or host.endswith(f".{name}") for name in SESSION_COOKIES)
    return cookies.is_signed_in(host) if known else True


_ADVICE_NO_JAR: Final[str] = (
    "لا توجد كوكيز محفوظة عندي.\n"
    "💡 الحل: صدّر ملف cookies.txt بصيغة Netscape من متصفح مسجّل الدخول "
    "لهذا الموقع وأرسله لي — التفاصيل في /help."
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
            "لا توجد كوكيز محفوظة.\n\n"
            "المواقع التي تخفي محتواها عن الزائر غير المسجّل — X وتيك توك "
            "وإنستقرام الخاص — ستستمر في الرفض.\n"
            "💡 الحل: أرسل لي ملف cookies.txt بصيغة Netscape."
        )

    lines = [
        "🍪 *الكوكيز المحفوظة*",
        f"عدد الكوكيز: {summary.cookie_count}",
        f"المواقع: {_sites(summary)}",
        _signed_in_line(summary),
        f"آخر تحديث: {summary.installed_at:%Y-%m-%d %H:%M} UTC",
        _expiry_line(summary),
        "",
        "أرسل ملف منصة أخرى وسيُضاف بجانب الموجود دون أن يمحوه.",
        "لحذف الكل: /cookies clear",
    ]
    return "\n".join(line for line in lines if line is not None)


def _signed_in_line(summary: CookieSummary) -> str:
    """Report which platforms the jar can actually sign in to.

    The distinction that matters and that a cookie count hides: a jar can list
    a site and hold nothing that proves who you are. Naming the platforms that
    are *not* signed in turns a story that silently refuses into something with
    a visible cause, before anyone sends a link and waits.
    """
    known = [
        platform
        for platform in SESSION_COOKIES
        if any(domain == platform or domain.endswith(f".{platform}") for domain in summary.domains)
    ]
    if not known:
        return "مسجّل الدخول في: لا شيء أعرفه"
    missing = [platform for platform in known if platform not in summary.signed_in]
    signed = "، ".join(summary.signed_in) if summary.signed_in else "لا شيء"
    if not missing:
        return f"✅ مسجّل الدخول في: {signed}"
    return f"✅ مسجّل الدخول في: {signed}\n" f"⚠️ بلا جلسة دخول: {'، '.join(missing)}"


def render_cookies_installed(summary: CookieSummary, *, removed: bool) -> str:
    """Confirm a jar was stored, and say what it covers.

    The site list is the useful part: exporting the wrong tab is the mistake
    people actually make, and it otherwise shows up days later as "the bot
    still cannot fetch X".
    """
    lines = [
        "✅ *تم تحديث الكوكيز*",
        f"عدد الكوكيز: {summary.cookie_count}",
        f"المواقع: {_sites(summary)}",
        _signed_in_line(summary),
        _expiry_line(summary),
        "",
        ("🗑 حذفت الملف الذي أرسلته." if removed else "⚠️ لم أستطع حذف ملفك — احذفه بنفسك."),
    ]
    return "\n".join(line for line in lines if line is not None)


def render_cookies_discarded(*, removed: bool) -> str:
    """Confirm the jar is gone."""
    if removed:
        return "🗑 حُذفت الكوكيز. سأتصفّح بدون تسجيل دخول من الآن."
    return "لا توجد كوكيز محفوظة أصلًا."


def render_cookie_too_large(declared: int, limit: int) -> str:
    """Refuse an upload before any of it is transferred."""
    return (
        f"حجم الملف {_bytes(declared)} والحد المسموح {_bytes(limit)}.\n\n"
        "ملف الكوكيز عادةً بضعة كيلوبايت — تأكد أنك صدّرت الكوكيز "
        "وليس شيئًا آخر."
    )


def _sites(summary: CookieSummary) -> str:
    """Render the domains a jar covers, bounded so it stays readable."""
    shown = summary.domains[:_MAX_SITES_SHOWN]
    if not shown:
        return "غير معروفة"
    extra = len(summary.domains) - len(shown)
    suffix = f" (+{extra} أخرى)" if extra else ""
    return "، ".join(shown) + suffix


def _expiry_line(summary: CookieSummary) -> str | None:
    """Render when the jar starts to lapse, or nothing if it never declares.

    A jar does not fail loudly - the platform simply starts refusing again -
    so this turns a future mystery into a date.
    """
    if summary.earliest_expiry is None:
        return None
    days = (summary.earliest_expiry - datetime.now(UTC)).days
    if days < 0:
        return "⚠️ أول كوكي انتهت صلاحيته — أرسل تصديرًا جديدًا."
    if days == 0:
        return "⚠️ أول انتهاء: اليوم"
    return f"أول انتهاء: بعد {days} يومًا"
