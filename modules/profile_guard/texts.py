"""🎨 قالب پیام‌های قابلیت «ضد کانال و بیوگرافی».

قالب‌ها **عیناً** مطابق اسکرین‌شات‌های کارفرما هستند:

1) پیام حذف به‌خاطر نام کانالِ پروفایل::

    ⛔ کاربر : @username            ← این خط Blockquote + Bold است
    پیام شما پاک شد | دلیل : کانال فیلتر شده
                                    ← یک خط خالی
    ↻- نام کانال بیوگرافی : Foxora Films   ← مقدار اسپویلر است
    ↻- تعداد اعضا : 1245                    ← مقدار اسپویلر است
    ↻- مجازات : سکوت دائمی                  ← مقدار عادی

2) پیام حذف به‌خاطر جملهٔ بیوگرافی::

    ⛔ کاربر : @username
    پیام شما پاک شد | دلیل : فیلتر بیوگرافی

    ↻- کلمه فیلتر بیوگرافی : بیا تو کانال ما فیلم هست   ← اسپویلر
    ↻- مجازات : 4 ساعت سکوت                             ← عادی

3) پیام روشن‌شدن::

    ⚠️ ضد کانال و بیوگرافی کاربران فعال شد
    نام ها و جملات فیلتر شده « 67 »

4) پیام خاموش‌شدن::

    ▪️ضد فیلتر کانال و بیوگرافی روباه خاموش شد

شیوهٔ «پررنگ/عادی» در ثابت‌های ``BOLD_*`` قابل تغییر است؛ همه‌جا
متن + entity صریح ساخته می‌شود (بدون parse_mode)، هم‌سبکِ
``modules/ad_spam_guard/texts.py``.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

try:  # pragma: no cover — در محیط بدونِ کتابخانه هم باید ایمپورت شود
    from splusthon.tl.types import (
        MessageEntityBlockquote,
        MessageEntityBold,
        MessageEntitySpoiler,
    )
except ImportError:  # pragma: no cover
    class MessageEntityBlockquote:  # type: ignore[no-redef]
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBold:  # type: ignore[no-redef]
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntitySpoiler:  # type: ignore[no-redef]
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


def u16(text) -> int:
    """طول متن به واحدِ UTF-16 (آفستِ entityها)."""
    return len(str(text).encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# تنظیمات «پررنگ / عادی» (برای تغییرِ سلیقه‌ای فقط این‌ها را عوض کنید)
# ---------------------------------------------------------------------------
BOLD_HEADER = True       # خط «⛔ کاربر : @…»
BOLD_REASON = True       # «کانال فیلتر شده» / «فیلتر بیوگرافی»
BOLD_VALUES = True       # مقدارِ جلوی هر ↻-
SPOILER_VALUES = True    # پنهان‌بودنِ نامِ کانال / جملهٔ فیلتر / تعداد اعضا (اسپویلر)

#: حالتِ پنهان‌سازیِ مقدارها:
#:   ``spoiler`` (پیش‌فرض) — فقط entity اسپویلر (با یک ضربه در کلاینت باز می‌شود)
#:   ``mask``                — خودِ متن با «••••••» جایگزین می‌شود (در همهٔ
#:                             کلاینت‌ها پنهان است، حتی اگر اسپویلر پشتیبانی نشود)
#:   ``both``                — هم ماسک، هم اسپویلر
#: با ``PROFILE_GUARD_HIDE=mask`` در ``.env`` عوض می‌شود (بدون تغییرِ کد).
HIDE_MASK = "••••••"
BOLD_ENABLE_TITLE = True  # خط اولِ پیام روشن‌شدن

# ---------------------------------------------------------------------------
# متن‌های ثابت
# ---------------------------------------------------------------------------
HEADER = "⛔ کاربر : {user}"

REASON_CHANNEL = "کانال فیلتر شده"
REASON_BIO = "فیلتر بیوگرافی"

BODY = "پیام شما پاک شد | دلیل : {reason}"

BULLET = "↻- {label} : {value}"
LABEL_CHANNEL_NAME = "نام کانال بیوگرافی"
LABEL_MEMBERS = "تعداد اعضا"
LABEL_PUNISH = "مجازات"
LABEL_BIO_WORD = "کلمه فیلتر بیوگرافی"

PUNISH_PERMANENT = "سکوت دائمی"
PUNISH_TEMPORARY = "{value} سکوت"

UNKNOWN_VALUE = "نامشخص"

ENABLE_TITLE = "⚠️ ضد کانال و بیوگرافی کاربران فعال شد"
ENABLE_COUNT = "نام ها و جملات فیلتر شده « {count} »"
DISABLED_MESSAGE = "▪️ضد فیلتر کانال و بیوگرافی روباه خاموش شد"


# ---------------------------------------------------------------------------
# سازندهٔ متن + entityها
# ---------------------------------------------------------------------------
class _Builder:
    """متن و entityها را با آفستِ درست (UTF-16) می‌سازد."""

    def __init__(self) -> None:
        self._text = ""
        self._entities: List[object] = []

    def add(self, text: str, *, bold: bool = False, quote: bool = False,
            spoiler: bool = False) -> "_Builder":
        if not text:
            return self
        offset = u16(self._text)
        self._text += text
        length = u16(text)
        if quote:
            self._entities.append(
                MessageEntityBlockquote(offset=offset, length=length)
            )
        if bold:
            self._entities.append(
                MessageEntityBold(offset=offset, length=length)
            )
        if spoiler:
            self._entities.append(
                MessageEntitySpoiler(offset=offset, length=length)
            )
        return self

    def build(self) -> Tuple[str, List[object]]:
        return self._text, self._entities


def punish_label(duration_seconds: Optional[int]) -> str:
    """متنِ مجازات: «سکوت دائمی» یا «4 ساعت سکوت»."""
    if duration_seconds is None:
        return PUNISH_PERMANENT
    try:
        seconds = int(duration_seconds)
    except (TypeError, ValueError):
        return PUNISH_PERMANENT
    if seconds <= 0:
        return PUNISH_PERMANENT
    hours, rest = divmod(seconds, 3600)
    if hours and not rest:
        amount = f"{hours} ساعت"
    elif hours:
        amount = f"{hours} ساعت و {rest // 60} دقیقه"
    else:
        amount = f"{max(1, rest // 60)} دقیقه"
    return PUNISH_TEMPORARY.format(value=amount)


def format_members(value) -> str:
    """تعداد اعضا به شکل خوانا (بدون ارقام فارسی، طبق قالب)."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return UNKNOWN_VALUE


def hide_mode() -> str:
    """حالتِ پنهان‌سازی (``spoiler`` / ``mask`` / ``both``)."""
    value = str(os.getenv("PROFILE_GUARD_HIDE", "spoiler") or "spoiler").strip().lower()
    return value if value in ("spoiler", "mask", "both") else "spoiler"


def _bullets(builder: _Builder, rows: Sequence[tuple]) -> None:
    """هر ردیف: ``(label, value)`` یا ``(label, value, spoiler)``."""
    mode = hide_mode()
    for index, row in enumerate(rows):
        label, value = row[0], row[1]
        hidden = bool(row[2]) if len(row) > 2 else False
        if hidden and mode in ("mask", "both"):
            shown = HIDE_MASK
        else:
            shown = str(value)
        line = BULLET.format(label=label, value="")
        builder.add(("" if index == 0 else "\n") + line)
        # ⚠️ روی محدودهٔ اسپویلر، Bold نمی‌گذاریم: بعضی کلاینت‌ها ترکیبِ
        # Bold+Spoiler را نادیده می‌گیرند و متن را ساده نشان می‌دهند.
        builder.add(shown, bold=BOLD_VALUES and not (hidden and mode in ("spoiler", "both")),
                    spoiler=hidden and SPOILER_VALUES and mode in ("spoiler", "both"))


def notice_channel(user: str, channel_title: Optional[str],
                   members=None, *, duration_seconds: Optional[int] = None
                   ) -> Tuple[str, List[object]]:
    """پیام حذف به‌خاطر نام کانالِ متصل به پروفایل."""
    builder = _Builder()
    builder.add(HEADER.format(user=user), bold=BOLD_HEADER, quote=True)
    builder.add("\n" + BODY.format(reason=""), bold=False)
    builder.add(REASON_CHANNEL, bold=BOLD_REASON)
    builder.add("\n\n")
    _bullets(builder, [
        (LABEL_CHANNEL_NAME, channel_title or UNKNOWN_VALUE, True),
        (LABEL_MEMBERS, format_members(members), True),
        (LABEL_PUNISH, punish_label(duration_seconds), False),
    ])
    return builder.build()


def notice_bio(user: str, matched: Optional[str], *,
               duration_seconds: Optional[int] = None
               ) -> Tuple[str, List[object]]:
    """پیام حذف به‌خاطر جملهٔ فیلترشده در بیوگرافی."""
    builder = _Builder()
    builder.add(HEADER.format(user=user), bold=BOLD_HEADER, quote=True)
    builder.add("\n" + BODY.format(reason=""), bold=False)
    builder.add(REASON_BIO, bold=BOLD_REASON)
    builder.add("\n\n")
    _bullets(builder, [
        (LABEL_BIO_WORD, matched or UNKNOWN_VALUE, True),
        (LABEL_PUNISH, punish_label(duration_seconds), False),
    ])
    return builder.build()


def enabled(count: int) -> Tuple[str, List[object]]:
    """پیام «فعال شد» همراهِ تعداد نام‌ها/جمله‌های فیلترشده."""
    builder = _Builder()
    builder.add(ENABLE_TITLE, bold=BOLD_ENABLE_TITLE)
    builder.add("\n" + ENABLE_COUNT.format(count=int(count)))
    return builder.build()


def disabled() -> str:
    return DISABLED_MESSAGE


def notice_for(kind: str, user: str, *, value: Optional[str] = None,
               members=None, duration_seconds: Optional[int] = None
               ) -> Tuple[str, List[object]]:
    """انتخاب قالب بر اساس نوع تخلف (``channel`` یا ``bio``)."""
    if kind == "channel":
        return notice_channel(user, value, members,
                              duration_seconds=duration_seconds)
    return notice_bio(user, value, duration_seconds=duration_seconds)


__all__ = [
    "u16", "MessageEntityBlockquote", "MessageEntityBold",
    "MessageEntitySpoiler", "HEADER", "REASON_CHANNEL", "REASON_BIO", "BODY", "BULLET",
    "LABEL_CHANNEL_NAME", "LABEL_MEMBERS", "LABEL_PUNISH", "LABEL_BIO_WORD",
    "PUNISH_PERMANENT", "PUNISH_TEMPORARY", "UNKNOWN_VALUE",
    "ENABLE_TITLE", "ENABLE_COUNT", "DISABLED_MESSAGE",
    "hide_mode", "HIDE_MASK", "punish_label", "format_members", "notice_channel", "notice_bio",
    "enabled", "disabled", "notice_for",
]
