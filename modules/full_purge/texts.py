# -*- coding: utf-8 -*-
"""🧬 قالب‌های پیامِ «پاکسازی کامل» — Bold و نقل‌قولِ سروش پلاس.

قواعد این فایل همان استاندارد موجودِ پروژه است (``modules/admin_help_builder``
و ``modules/entertainment_control``): هیچ مارک‌داون و ``parse_mode``ای استفاده
نمی‌شود؛ متن یک‌جا ساخته می‌شود و هر بخش با ``MessageEntityBold`` /
``MessageEntityBlockquote`` و آفستِ **UTF-16** علامت می‌خورد.

قالب‌ها عیناً از روی تصویرِ کارفرما پیاده شده‌اند:

* قالب ۱ (پیامِ تأیید، پیش از شروع): عنوان و توضیح Bold، سه گزینهٔ پاکسازی
  هر کدام در نقل‌قولِ جداگانه، «تایید»/«لغو» Bold.
* قالب ۲ (شروع عملیات): عنوانِ کامل Bold و داخل نقل‌قول، و شمارنده/مدت/وضعیت
  به‌صورت Bold.

هیچ زیرخطی جعل نمی‌شود: کتابخانه ``MessageEntityUnderline`` دارد، اما در
قالب‌های کارفرما زیرخط خواسته نشده و متنِ کج‌شده در تصویر فقط نمایشِ
راست‌به‌چپِ سروش است. تابع ``underline_entities`` فقط برای استفادهٔ آیندهٔ
صریح نگه داشته شده و در این قالب‌ها صدا زده نمی‌شود.
"""
from __future__ import annotations

try:  # pragma: no cover - بسته به نصب کتابخانهٔ کلاینت
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold

    UNDERLINE_SUPPORTED = True
except ImportError:  # pragma: no cover
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    UNDERLINE_SUPPORTED = False

# ---------------------------------------------------------------------------
# متن‌های ثابت (عیناً از قالب کارفرما — فاصله‌ها و ایموجی‌ها دست‌نخورده)
# ---------------------------------------------------------------------------
CONFIRM_TITLE = "⚙️ : پاکسازی کامل گروہ"
CONFIRM_BODY = (
    "تمام پیام های در این گروه پاک خواهند شد\n"
    "از جمله سنجاق ها و ... لطفا قبل از پاکسازی موارد رو\n"
    "تایید کنید"
)
CONFIRM_OPTIONS = (
    "پاکسازی همه پیاما",
    "پاکسازی پیام های سنجاق",
    "پاکسازی لینک های گروه",
)
CONFIRM_ACCEPT = "تایید 💎"
CONFIRM_CANCEL = "لغو 💎"

START_TITLE = "🔐 گروه برای پاکسازی کامل قفل شد"
START_STATUS = "آغاز پاکسازی خودکار"
START_COUNT_LABEL = "تعداد پیام در حال پاک شدن"
START_DURATION_LABEL = "مدت زمان"

DONE_TITLE = "✅ پاکسازی کامل انجام شد"
PARTIAL_TITLE = "⚠️ پاکسازی کامل انجام شد"
PARTIAL_NOTE = "پیام‌های بالا توسط سرور قابل حذف نبودند"
DONE_LOCK_NOTE = "قفل گروه به حالت قبل برگشت"
CANCEL_TITLE = "لغو شد ❌"
CANCEL_BODY = "پاکسازی انجام نشد و گروه دست‌نخورده ماند."
BUSY_TITLE = "⏳ یک پاکسازی در حال اجراست"
BUSY_BODY = "پاکسازی کاملِ این گروه هنوز تمام نشده است."
NO_PERMISSION = "❌ فقط مالک یا ادمین‌های ثبت‌شده اجازهٔ پاکسازی کامل دارند."
PRIVATE_NOTICE = "❌ این دستور فقط داخل گروه کار می‌کند."


def u16(text) -> int:
    """طول رشته بر حسب واحدهای UTF-16 (واحدی که entityها می‌شناسند)."""
    return len(str(text).encode("utf-16-le")) // 2


def _bold(text, needle, entities, limit=1):
    """همهٔ رخدادهای ``needle`` در ``text`` را Bold می‌کند (تا سقف ``limit``)."""
    cursor = 0
    found = 0
    while found < limit:
        index = text.find(needle, cursor)
        if index < 0:
            return
        entities.append(MessageEntityBold(offset=u16(text[:index]),
                                          length=u16(needle)))
        cursor = index + len(needle)
        found += 1


def _quote(text, needle, entities, limit=1):
    cursor = 0
    found = 0
    while found < limit:
        index = text.find(needle, cursor)
        if index < 0:
            return
        entities.append(MessageEntityBlockquote(offset=u16(text[:index]),
                                                length=u16(needle)))
        cursor = index + len(needle)
        found += 1


# ---------------------------------------------------------------------------
# قالب ۱: پیام تأیید پیش از شروع
# ---------------------------------------------------------------------------
def confirm_text() -> str:
    return (
        f"{CONFIRM_TITLE}\n\n"
        f"{CONFIRM_BODY}\n"
        f"{CONFIRM_OPTIONS[0]}\n"
        f"{CONFIRM_OPTIONS[1]}\n"
        f"{CONFIRM_OPTIONS[2]}\n\n"
        f"{CONFIRM_ACCEPT}\n"
        f"{CONFIRM_CANCEL}"
    )


def confirm_entities(text=None) -> list:
    """Bold + نقل‌قول برای قالب تأیید: عنوان و توضیح، سه گزینه و دو دکمه."""
    text = confirm_text() if text is None else text
    entities = []
    # عنوان + توضیح: هر دو Bold (مثل تصویر)
    _bold(text, CONFIRM_TITLE, entities)
    _bold(text, CONFIRM_BODY, entities)
    # هر سه گزینهٔ پاکسازی، هر کدام نقل‌قولِ جداگانه
    for option in CONFIRM_OPTIONS:
        _quote(text, option, entities)
    # دکمه‌های متنی
    _bold(text, CONFIRM_ACCEPT, entities)
    _bold(text, CONFIRM_CANCEL, entities)
    return entities


# ---------------------------------------------------------------------------
# قالب ۲: پیام وضعیت زنده (همان پیام ویرایش می‌شود)
# ---------------------------------------------------------------------------
def format_duration(seconds) -> str:
    """زمان خوانا: «۲ ثانیه»، «۱ دقیقه و ۲۰ ثانیه»، «۲ دقیقه»."""
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    fa = _fa_digits
    if hours:
        head = f"{fa(hours)} ساعت"
        if minutes:
            return f"{head} و {fa(minutes)} دقیقه"
        return head
    if minutes:
        head = f"{fa(minutes)} دقیقه"
        if secs:
            return f"{head} و {fa(secs)} ثانیه"
        return head
    return f"{fa(secs)} ثانیه"


def _fa_digits(value) -> str:
    """ارقام لاتین → فارسی (بدون وابستگی به هندلر)."""
    table = {ord(str(i)): str(i).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
             for i in range(10)}
    return str(value).translate(table)


def status_text(*, state="running", deleted=0, duration=0.0,
                remaining=None, total=None, lock_released=False,
                scanned=0) -> str:
    """متن پیام وضعیت؛ ``state`` یکی از ``running``/``done``/``failed``/``locked``."""
    if state == "done":
        lines = [DONE_TITLE, ""]
        lines.append(f"{START_COUNT_LABEL} : {_fa_digits(deleted)}")
        lines.append(f"{START_DURATION_LABEL} : {format_duration(duration)}")
        if lock_released:
            lines.append(DONE_LOCK_NOTE)
        return "\n".join(lines)
    if state == "partial":
        lines = [PARTIAL_TITLE, ""]
        lines.append(f"{START_COUNT_LABEL} : {_fa_digits(deleted)}")
        lines.append(f"{START_DURATION_LABEL} : {format_duration(duration)}")
        if remaining:
            lines.append(f"قابل‌حذف نبود : {_fa_digits(remaining)}")
            lines.append(PARTIAL_NOTE)
        if lock_released:
            lines.append(DONE_LOCK_NOTE)
        return "\n".join(lines)
    if state == "failed":
        lines = [f"⛔ پاکسازی نیمه‌کاره ماند", ""]
        lines.append(f"{START_COUNT_LABEL} : {_fa_digits(deleted)}")
        lines.append(f"{START_DURATION_LABEL} : {format_duration(duration)}")
        lines.append("ادامهٔ پاکسازی: دوباره دستور بزنید")
        if lock_released:
            lines.append(DONE_LOCK_NOTE)
        return "\n".join(lines)
    if state == "locked":
        # پیامِ «قفل شد» لحظهٔ اول؛ پیش از شروع شمارش
        return f"{START_TITLE}\n\n{START_STATUS}\n"
    lines = [START_TITLE, "", START_STATUS]
    lines.append(f"{START_COUNT_LABEL} : {_fa_digits(deleted)}")
    lines.append(f"{START_DURATION_LABEL} : {format_duration(duration)}")
    if total is not None:
        lines.append(f"کل شناسایی‌شده : {_fa_digits(total)}")
    if remaining is not None:
        lines.append(f"در صف : {_fa_digits(remaining)}")
    return "\n".join(lines)


def status_entities(text) -> list:
    """عنوانِ پیام وضعیت Bold + نقل‌قول، و هر خطِ دیگر یک‌بار Bold می‌شود.

    هیچ span تکراری یا هم‌پوشان ساخته نمی‌شود (سروش با spanهای روی‌هم
    رفتار غیرقابل‌پیش‌بینی دارد).
    """
    entities = []
    seen = set()

    def add(entity):
        key = (type(entity).__name__, entity.offset, entity.length)
        if key not in seen:
            seen.add(key)
            entities.append(entity)

    lines = text.splitlines()
    title = lines[0] if lines else text
    if title:
        add(MessageEntityBold(offset=0, length=u16(title)))
        add(MessageEntityBlockquote(offset=0, length=u16(title)))
    offset = u16(title) + 1 if title else 0
    for line in lines[1:]:
        if line.strip():
            add(MessageEntityBold(offset=offset, length=u16(line)))
        offset += u16(line) + 1
    return entities


# ---------------------------------------------------------------------------
# پیام‌های کوتاه
# ---------------------------------------------------------------------------
def simple(text) -> tuple:
    """پیام کوتاه: کل متن Bold."""
    return text, [MessageEntityBold(offset=0, length=u16(text))]


def underline_entities(text, needles) -> list:
    """فقط اگر کتابخانه ``MessageEntityUnderline`` داشته باشد و صریحاً لازم شد."""
    if not UNDERLINE_SUPPORTED:
        return []
    from splusthon.tl.types import MessageEntityUnderline  # pragma: no cover
    entities = []
    for needle in needles:
        index = str(text).find(needle)
        if index >= 0:
            entities.append(MessageEntityUnderline(
                offset=u16(str(text)[:index]), length=u16(needle)))
    return entities
