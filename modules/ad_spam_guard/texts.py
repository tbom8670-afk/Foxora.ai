"""قالب پیام‌های قابلیت «ضد ربات تبلیغاتی» (تبلیغ گر).

همهٔ قالب‌ها **عیناً** مطابق اسکرین‌شاتِ کارفرما هستند:

* کارتِ تأییدیهٔ روشن‌کردن: عنوان داخلِ نقل‌قول، فهرستِ پنج‌خطیِ پشتِ‌سرهم،
  «آیا این قابلیت فعال شود ؟» و دو خطِ «▪️تایید» / «▫️ لغو».
* پیامِ هشدارِ حذف: عنوان داخلِ نقل‌قول، خطِ دلیل ساده، و سه خطِ «■» که
  فقط **ارقامشان** Bold است («تعداد اخطار : 1/5»، «درصد تشخیص : 85/32»)؛
  شمارنده از **سیستم اخطارِ خودِ ربات**. کارت با یادآوریِ «۶۰ ثانیهٔ دیگر
  پاک می‌شود» تمام می‌شود و خودش هم پس از ۶۰ ثانیه حذف می‌گردد.

هیچ ``parse_mode``/مارک‌داونی در کار نیست؛ متن + entity صریح ساخته می‌شود
(هم‌سبکِ ``modules/manage_features/texts.py``).
"""

from __future__ import annotations

import re

try:  # pragma: no cover — در تست‌ها/محیطِ بدونِ کتابخانه هم باید ایمپورت شود
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:  # pragma: no cover
    class MessageEntityBlockquote:  # type: ignore[no-redef]
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBold:  # type: ignore[no-redef]
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


def u16(text) -> int:
    """طولِ متن به واحدِ UTF-16 (آفستِ entityها)."""
    return len(str(text).encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# ۱) کارتِ تأییدیهٔ روشن‌کردن (پیش از فعال‌سازی واقعی)
# ---------------------------------------------------------------------------
CONFIRM_TITLE = "🦠ضد تبلیغ گر پیام های کاربران و ربات ها"

#: پنج خطِ فهرست — عیناً مثل عکس، با نشانهٔ «» در ابتدای هر خط و بدونِ خطِ
#: خالیِ میانی.
CONFIRM_ITEMS = (
    "» شناسایی پیام های تبلیغاتی ربات ها",
    "» شناسایی پیام های عادی تبلیغات",
    "» شناسایی هرزنامه های ربات ها",
    "» شناسایی با دقت 85٪ هوش مصنوعی",
    "» شناسایی پیام های عادی ربات ها",
)

CONFIRM_QUESTION = "آیا این قابلیت فعال شود ؟"
CONFIRM_ACCEPT_LINE = "▪️تایید"
CONFIRM_CANCEL_LINE = "▫️ لغو"

#: متنِ کاملِ کارت (بدونِ entity) — برای تست/لاگ/سازگاریِ عقب‌رو.
CONFIRM_ENABLE_TEMPLATE = "\n".join(
    [CONFIRM_TITLE, ""]
    + list(CONFIRM_ITEMS)
    + ["", CONFIRM_QUESTION, CONFIRM_ACCEPT_LINE, CONFIRM_CANCEL_LINE]
)


def confirm_enable():
    """``(متن, entityها)`` کارتِ تأییدیه — فقط عنوان داخلِ نقل‌قول است."""
    entities = [MessageEntityBlockquote(offset=0, length=u16(CONFIRM_TITLE))]
    return CONFIRM_ENABLE_TEMPLATE, entities


# ---------------------------------------------------------------------------
# ۲) پیام‌های نتیجه
# ---------------------------------------------------------------------------
ENABLED_MESSAGE = "▫️ضد تبلیغ گر هوش مصنوعی روباه فعال شد"
DISABLED_MESSAGE = "▪️ضد تبلیغ گر هوش مصنوعی روباه خاموش شد"
CANCELLED_MESSAGE = "▫️لغو شد."

# ---------------------------------------------------------------------------
# ۳) پیام هشدار هنگام حذف پیام تبلیغاتی
# ---------------------------------------------------------------------------
# قالب — عیناً مطابقِ اسکرین‌شاتِ کارفرما:
#
#   ┌ نقل‌قول (Blockquote) ───────────────────┐
#   │ ⛔ کاربر  @user                         │
#   └─────────────────────────────────────────┘
#   پیام شما پاک شد | دلیل : هرزنامه تبلیغات   ← ساده (بدونِ Bold)
#                                              ← یک خطِ خالی
#   ■ تعداد اخطار : 1/5                        ← فقط ارقام Bold
#   ■ انجام دهنده : هوش مصنوعی                 ← ساده
#   ■ درصد تشخیص : 85/32                       ← فقط ارقام Bold
#   ■ این هشدار ۶۰ ثانیهٔ دیگر پاک می‌شود        ← یادآوری (ساده)
WARN_REASON_DEFAULT = "هرزنامه\u200c تبلیغات"
WARN_HEADER = "⛔ کاربر  {user}"
WARN_BODY = "پیام شما پاک شد | دلیل : {reason}"
WARN_BULLET = "■ "
WARN_COUNT_LABEL = "تعداد اخطار"
WARN_ACTOR_LABEL = "انجام دهنده"
WARN_PERCENT_LABEL = "درصد تشخیص"
WARN_ACTOR_NAME = "هوش مصنوعی"
#: آستانهٔ پیش‌فرض وقتی سیستمِ اخطارِ ربات در دسترس نیست.
WARN_DEFAULT_THRESHOLD = 5
#: یادآوریِ کوتاه که «همین حالا» به چشمِ متخلف بیاید (چون کارت خودش
#: ۶۰ ثانیه بعد پاک می‌شود).
WARN_REMINDER = "این هشدار ۶۰ ثانیهٔ دیگر خودکار پاک می‌شود"

#: متنِ کاملِ هشدار (بدونِ entity) — برای تست/لاگ/سازگاریِ عقب‌رو.
WARN_TEMPLATE = (
    WARN_HEADER + "\n"
    + WARN_BODY + "\n"
    "\n"
    + WARN_BULLET + WARN_COUNT_LABEL + " : {count}/{threshold}" + "\n"
    + WARN_BULLET + WARN_ACTOR_LABEL + " : " + WARN_ACTOR_NAME + "\n"
    + WARN_BULLET + WARN_PERCENT_LABEL + " : {percent}/{score}" + "\n"
    + WARN_BULLET + WARN_REMINDER
)

#: ارقامی که داخلِ خطوط Bold می‌شوند (مثلِ «1/5» و «85/32»).
_NUMBER_RE = re.compile(r"[0-9]+(?:/[0-9]+)?")


def detect_percent(score, *, maximum: int = 13) -> int:
    """درصدِ تشخیصِ هوش مصنوعی (۱ تا ۹۹) از امتیازِ خامِ موتورِ تبلیغات.

    امتیازِ خام از مجموعِ وزن‌های ``patterns.WEIGHTS`` (پیش‌فرض ۱۳) بیشتر
    نمی‌شود؛ درصد = سهمِ امتیاز از کل.
    """
    try:
        raw = float(score)
        top = float(maximum) or 1.0
    except (TypeError, ValueError):
        return 0
    if raw <= 0:
        return 0
    return max(1, min(99, int(round(raw / top * 100))))


def _as_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def render_warn(user: str, count: int,
                reason: str = WARN_REASON_DEFAULT,
                threshold: int = WARN_DEFAULT_THRESHOLD,
                *, percent=None, score=None,
                reminder: str | None = WARN_REMINDER) -> str:
    """متنِ پیامِ هشدار با مقدارهای واقعی (بدونِ entity)."""
    threshold = _as_int(threshold, WARN_DEFAULT_THRESHOLD) or WARN_DEFAULT_THRESHOLD
    count = max(0, _as_int(count))
    score = max(0, _as_int(score))
    if percent is None:
        percent = detect_percent(score)
    percent = max(0, min(100, _as_int(percent)))
    text = WARN_TEMPLATE.format(
        user=user, reason=reason,
        count=count, threshold=threshold,
        percent=percent, score=score,
    )
    if not reminder:
        text = "\n".join(text.split("\n")[:-1])
    return text


def _number_spans(line: str):
    """بازه‌های (offset, length) ارقامِ داخلِ یک خط، بر حسبِ UTF-16."""
    spans = []
    position = 0
    for match in _NUMBER_RE.finditer(line):
        before = line[:match.start()]
        spans.append((u16(before), u16(match.group(0))))
    del position
    return spans


def warn_payload(user: str, count: int,
                 reason: str = WARN_REASON_DEFAULT,
                 threshold: int = WARN_DEFAULT_THRESHOLD,
                 *, percent=None, score=None,
                 reminder: str | None = WARN_REMINDER):
    """``(متن, entityها)`` پیامِ هشدار.

    * خطِ اول («⛔ کاربر  @…») فقط ``Blockquote``.
    * خطِ دوم («پیام شما پاک شد | دلیل : …») ساده.
    * خطوطِ ``■`` ساده‌اند و **فقط ارقامشان** ``Bold`` است.
    """
    text = render_warn(user, count, reason, threshold,
                       percent=percent, score=score, reminder=reminder)
    entities = []
    position = 0
    for index, line in enumerate(text.split("\n")):
        length = u16(line)
        if index == 0:
            entities.append(MessageEntityBlockquote(offset=position,
                                                    length=length))
        elif line.startswith(WARN_BULLET):
            for offset, number_len in _number_spans(line):
                if number_len:
                    entities.append(MessageEntityBold(
                        offset=position + offset, length=number_len))
        position += length + 1        # +1 برای \n
    return text, entities


# ---------------------------------------------------------------------------
# ۴) راهنمای دستور (برای لیست ادمینی)
# ---------------------------------------------------------------------------
HELP_HEADER = "💥برای فعال کردن ضد ربات های تبلیغ گر بنویسید"
HELP_ON = "تبلیغ گر روشن"
HELP_OFF = "تبلیغ گر خاموش"

# ---------------------------------------------------------------------------
# کلماتِ پاسخ به تأییدیه
# ---------------------------------------------------------------------------
#: همهٔ شکل‌های رایج — نشانه‌ها/فاصله/نیم‌فاصله/اعراب در
#: ``detector._clean_word`` حذف می‌شوند، پس این‌ها فقط «ریشه»ها هستند.
CONFIRM_WORDS = frozenset({
    "تایید", "تأیید", "✅ تایید", "▪️تایید", "تایید.", "تایید!",
})
CANCEL_WORDS = frozenset({"لغو", "❌ لغو", "▫️ لغو", "▫️لغو"})

#: سازگاریِ عقب‌رو — خطوطِ هشدار به‌ترتیبِ نمایش.
def warn_lines(user: str, count: int,
               reason: str = WARN_REASON_DEFAULT,
               threshold: int = WARN_DEFAULT_THRESHOLD,
               *, percent=None, score=None, reminder: str | None = WARN_REMINDER):
    """خطوطِ هشدار به‌ترتیبِ نمایش (برای ساختِ entityها)."""
    return render_warn(user, count, reason, threshold, percent=percent,
                       score=score, reminder=reminder).split("\n")
