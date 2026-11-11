"""قالب پیام‌های قابلیت «ضد ربات تبلیغاتی» (تبلیغ گر).

همهٔ قالب‌ها **عیناً** مطابق اسکرین‌شاتِ کارفرما هستند:

* کارتِ تأییدیهٔ روشن‌کردن: عنوان داخلِ نقل‌قول، فهرستِ پنج‌خطیِ پشتِ‌سرهم،
  «آیا این قابلیت فعال شود ؟» و دو خطِ «▪️تایید» / «▫️ لغو».
* پیامِ هشدارِ حذف: عنوان داخلِ نقل‌قول، کلِ متن **Bold**، و شمارندهٔ اخطار
  به‌شکلِ «تعداد اخطار : 1/5» از **سیستم اخطارِ خودِ ربات**.

هیچ ``parse_mode``/مارک‌داونی در کار نیست؛ متن + entity صریح ساخته می‌شود
(هم‌سبکِ ``modules/manage_features/texts.py``).
"""

from __future__ import annotations

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
#   ┌ نقل‌قول ────────────────────────────────┐
#   │ ⚠️ کاربر  @user                         │  ← این خط Blockquote است
#   └─────────────────────────────────────────┘
#   پیام شما پاک شد | دلیل : تبلیغات گروهی       ← چسبیده به نقل‌قول
#                                                ← یک خطِ خالی
#   تعداد اخطار : 1/5                            ← شمارندهٔ سیستم اخطار + آستانهٔ گروه
#   انجام دهنده : هوش مصنوعی ضد تبلیغ            ← چسبیده به خطِ قبل
#
# **کلِ متن Bold است** (خواستِ کارفرما) و فقط خطِ عنوان نقل‌قول دارد.
WARN_REASON_DEFAULT = "تبلیغات گروهی"
WARN_HEADER = "⚠️ کاربر  {user}"
WARN_BODY = "پیام شما پاک شد | دلیل : {reason}"
#: شمارنده از ``bot.tracker`` (سیستمِ اخطارِ ربات) و آستانه از
#: ``modules.warning_threshold`` — دقیقاً مثلِ «تعداد اخطار : 2/5» در گزارش.
WARN_COUNT_LINE = "تعداد اخطار : {count}/{threshold}"
WARN_ACTOR_LINE = "انجام دهنده : هوش مصنوعی ضد تبلیغ"
#: آستانهٔ پیش‌فرض وقتی سیستم اخطار در دسترس نیست (همان پیش‌فرضِ ربات).
WARN_DEFAULT_THRESHOLD = 5

WARN_TEMPLATE = (
    WARN_HEADER + "\n"
    + WARN_BODY + "\n"
    "\n"
    + WARN_COUNT_LINE + "\n"
    + WARN_ACTOR_LINE
)

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


def render_warn(user: str, count: int,
                reason: str = WARN_REASON_DEFAULT,
                threshold: int = WARN_DEFAULT_THRESHOLD) -> str:
    """متنِ پیامِ هشدار را با مقدارهای واقعی می‌سازد."""
    try:
        return WARN_TEMPLATE.format(
            user=user, count=int(count), reason=reason,
            threshold=int(threshold) or WARN_DEFAULT_THRESHOLD,
        )
    except Exception:
        # قالبِ خراب نباید کل قابلیت را از کار بیندازد.
        return WARN_TEMPLATE


def warn_payload(user: str, count: int,
                 reason: str = WARN_REASON_DEFAULT,
                 threshold: int = WARN_DEFAULT_THRESHOLD):
    """``(متن, entityها)`` پیامِ هشدار.

    * **کلِ متن Bold است** (خواستِ کارفرما).
    * خطِ اول («⚠️ کاربر @…») علاوه‌بر Bold، ``MessageEntityBlockquote`` هم
      دارد تا مثلِ عکس با نوارِ رنگی نمایش داده شود.
    """
    text = render_warn(user, count, reason, threshold)
    entities = [MessageEntityBold(offset=0, length=u16(text))]
    header = WARN_HEADER.format(user=user)
    if text.startswith(header):
        entities.append(MessageEntityBlockquote(offset=0, length=u16(header)))
    return text, entities
