# -*- coding: utf-8 -*-
"""📊 قالب «آمار کل گروه» — دقیقاً مثل تصویر کارفرما، با Bold/نقل‌قول واقعی.

ساختار پیام (هر خطِ آماری یک نقل‌قولِ جداست):

    📊 آمار کل گروه

    «کل پیام‌ها: 13»
    «پیام های حذف شده: 4»
    «اخراج شده ها : 0»
    «سکوت شدها : 0»
    «تعداد اعضا گروه : 3»
    «تعداد کل گیف های ارسالی : 7»

    ↶ کاربران فعال گروه :

    ⬢ کاربر 1 : @username
    ⬢ کاربر 2 : نام نمایشی
    ...

قواعدی که کارفرما صریح گفت:

* کل متن Bold است.
* هر کدام از خط‌های آماری داخل نقل‌قول سروش پلاس است، و خودِ خط
  (نه فقط عدد) — عیناً مثل عکس.
* «تعداد کل گیف های ارسالی» یک شمارندهٔ واقعی است (``group_stats.add_gif``).
* نام کاربر: اول یوزرنیم، اگر نبود نام نمایشی. شناسهٔ عددی هرگز چاپ نمی‌شود.
* رتبه‌بندی فقط بر اساس تعداد پیام ارسالی، از بیشترین به کمترین، با
  شکستنِ تساوی بر اساس شناسه؛ یعنی خروجی همیشه یکسان و بی‌خطا است.
* اگر کاربری کمتر از ۱۰ نفر باشد، بقیهٔ ردیف‌ها «⬢ کاربر N : ----» می‌شوند.

آفست‌ها UTF-16 هستند (استاندارد سروش پلاس/تلگرام) تا متن و entity هیچ‌وقت
از هم جدا نیفتند.
"""
from __future__ import annotations

try:  # pragma: no cover - کتابخانه در محیط تست موجود است
    from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote
except ImportError:  # pragma: no cover
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

from modules import group_stats

TITLE = "📊 آمار کل گروه"
USERS_TITLE = "↶ کاربران فعال گروه :"
RANK_MARK = "⬟"
EMPTY_NAME = "----"
MAX_USERS = 10

#: برچسب‌های آماری — عیناً مثل تصویر کارفرما (فاصله‌ها هم حفظ شده‌اند).
LABEL_MESSAGES = "کل پیام‌ها"
LABEL_DELETED = "پیام های حذف شده"
LABEL_KICKED = "اخراج شده ها :"
LABEL_MUTED = "سکوت شدها :"
LABEL_MEMBERS = "تعداد اعضا گروه :"
LABEL_GIFS = "تعداد کل گیف های ارسالی :"


def u16(text) -> int:
    """طول متن بر حسب واحد UTF-16 (همان چیزی که سروش می‌شمارد)."""
    return len(str(text).encode("utf-16-le")) // 2


def fa_digits(value) -> str:
    """۰۱۲۳۴۵۶۷۸۹ — مثل بقیهٔ متن‌های فارسی ربات."""
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def _stat_line(label, value):
    # برچسب‌هایی که در تصویر با «:» تمام می‌شوند همان‌طور می‌مانند؛ بقیه یک
    # «: » می‌گیرند تا همهٔ ردیف‌ها یک‌شکل باشند.
    if label.endswith(":"):
        return f"{label} {fa_digits(value)}"
    return f"{label}: {fa_digits(value)}"


#: مقادیر جای‌نگهدارِ قدیمی که «نام واقعی» نیستند.
_PLACEHOLDER_NAMES = {"", "unknown", "none", "null", "ناشناس", "کاربر ناشناس"}


def display_name(info, fallback=EMPTY_NAME):
    """یوزرنیم، و اگر نبود نام نمایشی — شناسهٔ عددی هرگز چاپ نمی‌شود.

    اگر کاربر نه یوزرنیم داشته باشد و نه نام نمایشی، ``----`` چاپ می‌شود
    (هیچ‌وقت شناسهٔ عددی و هیچ‌وقت «unknown»).
    """
    from modules.user_display import format_user

    if not isinstance(info, dict):
        return fallback
    username = str(info.get("username") or "").strip().lstrip("@")
    if username.lower() in _PLACEHOLDER_NAMES or username.isdigit():
        username = ""
    name = str(info.get("name") or "").strip()
    if name.lower() in _PLACEHOLDER_NAMES or name.isdigit():
        name = ""
    if not username and not name:
        return fallback
    shown = format_user(
        {"username": username, "first_name": name}, fallback="")
    return shown or fallback


def build_report(chat_id, member_count=None, limit=MAX_USERS):
    """متن + entityهای «آمار کل گروه»."""
    group = group_stats.get_stats(chat_id)

    members = 0
    try:
        members = int(member_count or 0)
    except (TypeError, ValueError):
        members = 0
    if members <= 0:
        members = int(group.get("members", 0) or 0)

    rows = [
        _stat_line(LABEL_MESSAGES, group.get("messages", 0)),
        _stat_line(LABEL_DELETED, group.get("deleted", 0)),
        _stat_line(LABEL_KICKED, group.get("kicked", 0)),
        _stat_line(LABEL_MUTED, group.get("muted", 0)),
        _stat_line(LABEL_MEMBERS, members),
        _stat_line(LABEL_GIFS, group.get("gifs", 0)),
    ]

    top = group_stats.top_users(chat_id, limit=limit)

    # ساخت متن + آفست‌ها با هم؛ آفست هر خط دقیقاً بعد از خط‌های قبلی، پس هیچ
    # محاسبهٔ دستی‌ای در کار نیست و امکان خطا در آفست صفر می‌شود.
    body = [TITLE]
    quoted = []          # (offset, length) برای نقل‌قول‌ها

    def append(line, quote=False):
        start = u16("\n".join(body)) + 1
        body.append(line)
        if quote:
            quoted.append((start, u16(line)))

    append("")           # خطِ خالیِ زیر عنوان

    for row in rows:
        append(row, quote=True)
    append("")
    append(USERS_TITLE)
    for index in range(1, limit + 1):
        if index <= len(top):
            _user_id, info = top[index - 1]
            name = display_name(info)
        else:
            name = EMPTY_NAME
        append(f"{RANK_MARK} کاربر {fa_digits(index)} : {name}")

    text = "\n".join(body)

    entities = [MessageEntityBold(offset=0, length=u16(text))]
    for offset, length in quoted:
        entities.append(MessageEntityBlockquote(offset=offset, length=length))
    return text, entities


def report_text(chat_id, member_count=None, limit=MAX_USERS) -> str:
    return build_report(chat_id, member_count, limit)[0]
