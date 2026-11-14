# -*- coding: utf-8 -*-
"""🕰 قالب‌های پیامِ قابلیت «تایم جفت» (Mirror Time).

متن دقیقاً همان چیزی است که کارفرما خواسته:

    ♡ ↻ تایم : 5:5 🦊🦋

و **کلِ متن Bold** است. برای Bold از سیستمِ قالب‌بندیِ موجودِ پروژه استفاده
می‌شود (``modules.entertainment_control``: هم ``MessageEntityBold`` و هم
``full_bold_span`` که آفست را بر حسبِ واحدهای UTF-16 حساب می‌کند) — هیچ روشِ
جدید/ناسازگاری ساخته نشده است. هیچ ``parse_mode``/مارک‌داون هم در کار نیست.

پاسخِ اختصاصیِ «فدات» هم این‌جاست: «فدامون 🥲»
"""
from __future__ import annotations

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


def render(hour, minute) -> str:
    """متنِ «♡ ↻ تایم : 5:5 🦊🦋» برای ساعتِ داده‌شده (بدونِ صفرِ پیشتاز)."""
    return TIME_TEMPLATE.format(hour=int(hour), minute=int(minute))


def bold_entities(text: str) -> list:
    """``[MessageEntityBold]`` روی کلِ متن (واحدِ محاسبه: UTF-16)."""
    offset, length = full_bold_span(text)
    if length <= 0:
        return []
    return [MessageEntityBold(offset=offset, length=length)]


def payload(hour, minute):
    """``(متن, entityها)`` آمادهٔ ارسال."""
    text = render(hour, minute)
    return text, bold_entities(text)


__all__ = [
    "TIME_TEMPLATE",
    "FADAT_REPLY",
    "FADAT_STEM",
    "render",
    "bold_entities",
    "payload",
]
