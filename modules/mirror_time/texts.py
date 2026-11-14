# -*- coding: utf-8 -*-
"""🕰 قالب‌های پیامِ قابلیت «تایم جفت» (Mirror Time).

متن دقیقاً همان چیزی است که کارفرما خواسته:

    ♡ ↻ تایم : 5:5 🦊🦋

ساعت به شکلِ **۱۲ساعته** نمایش داده می‌شود: تایمِ ۱۸:۱۸ (۶:۶ عصر) هم پیامِ
«♡ ↻ تایم : 6:6 🦊🦋» می‌گیرد — یعنی هم ساعتِ ۶:۶ صبح هم ۶:۶ عصر. اگر خواستی
۲۴ساعته باشد، ``MIRROR_TIME_CLOCK=24`` بگذار.

و **کلِ متن Bold** است. برای Bold از سیستمِ قالب‌بندیِ موجودِ پروژه استفاده
می‌شود (``modules.entertainment_control``: هم ``MessageEntityBold`` و هم
``full_bold_span`` که آفست را بر حسبِ واحدهای UTF-16 حساب می‌کند) — هیچ روشِ
جدید/ناسازگاری ساخته نشده است. هیچ ``parse_mode``/مارک‌داون هم در کار نیست.

پاسخِ اختصاصیِ «فدات» هم این‌جاست: «فدامون 🥲»
"""
from __future__ import annotations

import os

#: قالبِ پیامِ تایم جفت (بدونِ صفرِ پیشتاز، مثلِ «5:5»).
TIME_TEMPLATE = "♡ ↻ تایم : {hour}:{minute} 🦊🦋"

#: پاسخِ اختصاصی به «فدات» — فقط روی ریپلایِ همان پیامِ تایم.
FADAT_REPLY = "فدامون 🥲"

#: هستهٔ واژه‌ایِ تشخیص (هر پیامی که این را داشته باشد: فدات/فداتم/فدات شم …).
FADAT_STEM = "فدات"

# ---------------------------------------------------------------------------
# Bold — از سیستمِ موجودِ پروژه (بدونِ ساختنِ روشِ تازه)
# ---------------------------------------------------------------------------
try:  # pragma: no cover — اگر پروژه در دسترس نبود، همان قراردادِ خودش
    from modules.entertainment_control import MessageEntityBold, full_bold_span
except Exception:  # pragma: no cover
    try:
        from splusthon.tl.types import MessageEntityBold
    except ImportError:
        class MessageEntityBold:  # type: ignore[no-redef]
            def __init__(self, offset=0, length=0):
                self.offset = offset
                self.length = length

    def full_bold_span(text):
        return 0, len(str(text).encode("utf-16-le")) // 2


#: نحوهٔ نمایشِ ساعت: ``12`` (پیش‌فرض — ۱۸:۱۸ ⇒ 6:6) یا ``24`` (۱۸:۱۸ ⇒ 18:18).
CLOCK_ENV = "MIRROR_TIME_CLOCK"
DEFAULT_CLOCK = "12"


def clock_mode(clock=None) -> str:
    """حالتِ نمایشِ ساعت: ``"12"`` یا ``"24"`` (قابلِ تغییر با env)."""
    value = str(clock if clock is not None
                else os.getenv(CLOCK_ENV, DEFAULT_CLOCK)).strip()
    return "24" if value.startswith("24") else "12"


def display_hour(hour, clock=None) -> int:
    """ساعتِ نمایشی: در حالتِ ۱۲ساعته «۱۸:۱۸» به شکلِ «6:6» نشان داده می‌شود."""
    hour = int(hour)
    if clock_mode(clock) == "24":
        return hour
    return hour % 12 or 12


def display_minute(hour, minute, clock=None) -> int:
    """دقیقهٔ نمایشی — در تایمِ جفت هم‌شکل با ساعت (۱۸:۱۸ ⇒ 6:6 و نه 6:18)."""
    hour, minute = int(hour), int(minute)
    if clock_mode(clock) == "24" or minute != hour:
        return minute
    return hour % 12 or 12


def render(hour, minute, clock=None) -> str:
    """متنِ «♡ ↻ تایم : 5:5 🦊🦋» برای ساعتِ داده‌شده (بدونِ صفرِ پیشتاز)."""
    return TIME_TEMPLATE.format(hour=display_hour(hour, clock),
                                minute=display_minute(hour, minute, clock))


def bold_entities(text: str) -> list:
    """``[MessageEntityBold]`` روی کلِ متن (واحدِ محاسبه: UTF-16)."""
    offset, length = full_bold_span(text)
    if length <= 0:
        return []
    return [MessageEntityBold(offset=offset, length=length)]


def payload(hour, minute, clock=None):
    """``(متن, entityها)`` آمادهٔ ارسال."""
    text = render(hour, minute, clock)
    return text, bold_entities(text)


__all__ = [
    "CLOCK_ENV",
    "DEFAULT_CLOCK",
    "clock_mode",
    "display_hour",
    "display_minute",
    "TIME_TEMPLATE",
    "FADAT_REPLY",
    "FADAT_STEM",
    "render",
    "bold_entities",
    "payload",
]
