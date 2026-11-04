# -*- coding: utf-8 -*-
"""⚖️ متن‌ها و قالب‌های دادگاه روباه.

قواعد قالب (خواستهٔ صریح کارفرما):

* همهٔ متن‌ها فارسی، رسمی، کوتاه و **کاملاً Bold** هستند. هیچ پیامی در
  قالب **نقل‌قول** (Blockquote) ارسال نمی‌شود؛ نقل‌قول در این بازی
  خواسته نشده است.
* همه‌چیز **ردیفی** است: بین خطوط هیچ خط خالی‌ای نیست.
  تنها استثنا «پیام آغاز دادگاه» است؛ آنجا تنها یک خط خالی پیش از
  فهرست شماره‌دار بازیکنان گذاشته می‌شود (جایی که کاربران یکی‌یکی
  نوشته شده‌اند).
* نام کاربر: یوزرنیم اگر دارد، وگرنه نام نمایشی (سیاست ``format_user``
  پروژه). شناسهٔ عددی هرگز نمایش داده نمی‌شود.

این ماژول هیچ state ندارد؛ فقط رشته می‌سازد تا تست‌ها بتوانند متن‌ها را
بدون هیچ I/O بررسی کنند.
"""
from __future__ import annotations

try:  # pragma: no cover - بسته به نصب کلاینت
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:  # pragma: no cover
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


JOIN_WORD = "شرکت"

BULLET = "•"
TICK = "⏳"

# برچسب فارسی نقش‌ها (ترتیب رسمی دادگاه).
ROLE_LABELS = {
    "plaintiff": "شاکی",
    "defendant": "متهم",
    "witness_p": "شاهد شاکی",
    "witness_d": "شاهد متهم",
    "lawyer": "وکیل",
}

CRIMES = ("دزدی", "قتل", "توهین", "دعوا و درگیری", "کلاهبرداری")


def u16(text) -> int:
    """طول رشته با واحد UTF-16 (همان واحدی که MessageEntity استفاده می‌کند)."""
    return len(str(text or "").encode("utf-16-le")) // 2


def bold_entities(text):
    """کل پیام را **Bold** می‌کند — بدون نقل‌قول.

    پیام‌های دادگاه عمداً در قالب نقل‌قول (Blockquote) فرستاده نمی‌شوند:
    تمام متن باید Bold باشد و ردیف‌به‌ردیف و ساده دیده شود.
    """
    length = u16(text)
    if length <= 0:
        return []
    return [MessageEntityBold(offset=0, length=length)]


def full_quote_entities(text):
    """نام قدیمی (سازگاری) — همان Bold، بدون نقل‌قول."""
    return bold_entities(text)


def persian_digits(value) -> str:
    digits = "۰۱۲۳۴۵۶۷۸۹"
    return "".join(digits[int(ch)] if ch.isdigit() else ch
                   for ch in str(value))


def seconds_label(value) -> str:
    """ثانیه‌ها بدون اعشار و با ارقام فارسی («۶۰» نه «۶۰.۰»)."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = 0
    return persian_digits(number)


# ---------------------------------------------------------------------------
# پیام‌های ثبت‌نام
# ---------------------------------------------------------------------------
def join_prompt(players_need, join_seconds) -> str:
    return "\n".join([
        "⚖️ دادگاه روباه",
        "پرونده جدید در انتظار تشکیل است",
        f"برای شرکت بنویسید: {JOIN_WORD}",
        f"تعداد مورد نیاز: {persian_digits(players_need)} نفر",
        f"{TICK} مهلت ثبت‌نام: {seconds_label(join_seconds)} ثانیه",
    ])


def join_confirmed(count, players_need) -> str:
    return (f"✅ ثبت‌نام شما در پرونده انجام شد "
            f"({persian_digits(count)} از {persian_digits(players_need)})")


def join_duplicate() -> str:
    return "⚠️ شما قبلاً در این پرونده ثبت‌نام کرده‌اید."


def join_full() -> str:
    return "⚠️ ظرفیت این پرونده تکمیل شده است."


def private_only() -> str:
    return "⚖️ دادگاه روباه فقط داخل گروه برگزار می‌شود."


def already_running() -> str:
    return "⚖️ یک دادگاه همین حالا در این گروه در جریان است."


def join_resumed(count, players_need) -> str:
    """پروندهٔ نیمه‌باز این گروه (مثلاً بعد از ری‌استارت) ادامه پیدا می‌کند."""
    return "\n".join([
        "⚖️ پروندهٔ این گروه باز است و ثبت‌نام آن ادامه دارد",
        f"ثبت‌نام‌شده‌ها: {persian_digits(count)} از {persian_digits(players_need)}",
        f"برای شرکت بنویسید: {JOIN_WORD}",
    ])


def other_game_running() -> str:
    return "⚖️ برای تشکیل دادگاه، ابتدا بازی در جریان این گروه باید تمام شود."


def not_enough_players(count, players_need) -> str:
    return "\n".join([
        "⚖️ پرونده تشکیل نشد",
        (f"تعداد شرکت‌کنندگان ({persian_digits(count)}) به "
         f"{persian_digits(players_need)} نفر نرسید."),
        "دادگاه لغو شد و جایزه‌ای پرداخت نشد.",
    ])


# ---------------------------------------------------------------------------
# آغاز دادگاه
# ---------------------------------------------------------------------------
def roster_message(bot_username, players) -> str:
    """پیام آغاز رسمی دادگاه.

    تنها جای مجموعه که یک خط خالی دارد: پیش از فهرست شماره‌دار بازیکنان.
    ورودی ``players`` نگاشت نقش → نام نمایشی است.
    """
    lines = [
        "⚖️ پرونده ساخته شد و دادگاه رسمی آغاز گردید",
        "",
        f"1 - قاضی: {bot_username or 'ربات'}",
    ]
    for index, role in enumerate(
            ("plaintiff", "defendant", "witness_p", "witness_d", "lawyer"), 2):
        lines.append(f"{index} - {ROLE_LABELS[role]}: "
                     f"{players.get(role, '—')}")
    return "\n".join(lines)


def crime_prompt(name, seconds) -> str:
    lines = [
        f"⚖️ کاربر شاکی: {name}",
        ("شما باید مشخص کنید متهم را به کدام یک از جرائم زیر متهم "
         "می‌کنید و شکایت شما مربوط به کدام مورد است:"),
    ]
    lines.extend(f"{BULLET} {crime}" for crime in CRIMES)
    lines.append("لطفاً یکی از گزینه‌ها را ارسال کنید.")
    lines.append(f"{TICK} مهلت پاسخ: {seconds_label(seconds)} ثانیه")
    return "\n".join(lines)


def crime_invalid() -> str:
    return "\n".join([
        "⚠️ گزینه ارسال‌شده معتبر نیست.",
        ("لطفاً فقط نام یکی از جرائم فهرست‌شده را ارسال کنید: "
         + "، ".join(CRIMES)),
    ])


def complaint_prompt(name, seconds) -> str:
    return "\n".join([
        f"⚖️ کاربر شاکی: {name}",
        ("لطفاً توضیح کوتاهی درباره اتفاق رخ‌داده بنویسید و مشخص کنید "
         "متهم چه کاری انجام داده و شکایت شما بر چه اساسی است."),
        f"{TICK} مهلت پاسخ: {seconds_label(seconds)} ثانیه",
    ])


def defense_prompt(name, seconds) -> str:
    return "\n".join([
        f"⚖️ کاربر متهم: {name}",
        "شما در برابر اتهامات مطرح‌شده فرصت دفاع دارید.",
        ("لطفاً در چند جمله توضیح دهید که آیا این اتهام‌ها را قبول "
         "دارید یا خیر و حقیقت ماجرا از نظر شما چیست."),
        f"{TICK} مهلت پاسخ: {seconds_label(seconds)} ثانیه",
    ])


def witness_p_prompt(name, seconds) -> str:
    return "\n".join([
        f"⚖️ کاربر شاهد شاکی: {name}",
        "شما به‌عنوان شاهد شاکی در این پرونده حضور دارید.",
        "آیا درباره اتفاق مطرح‌شده شهادتی دارید؟",
        ("لطفاً توضیح دهید چه چیزی مشاهده کرده‌اید یا از ماجرا "
         "چه اطلاعی دارید."),
        f"{TICK} مهلت پاسخ: {seconds_label(seconds)} ثانیه",
    ])


def witness_d_prompt(name, seconds) -> str:
    return "\n".join([
        f"⚖️ کاربر شاهد متهم: {name}",
        "شما به‌عنوان شاهد متهم در این پرونده حضور دارید.",
        ("لطفاً توضیح دهید چه چیزی از اتفاق مشاهده کرده‌اید و آیا "
         "می‌توانید از متهم دفاع کنید؟"),
        f"{TICK} مهلت پاسخ: {seconds_label(seconds)} ثانیه",
    ])


def lawyer_prompt(name, seconds) -> str:
    return "\n".join([
        f"⚖️ کاربر وکیل: {name}",
        "شما وکیل متهم هستید.",
        ("لطفاً دفاعیه نهایی خود را درباره این پرونده در چند جمله "
         "بنویسید و دلایل خود را برای بی‌گناهی متهم مطرح کنید."),
        f"{TICK} مهلت پاسخ: {seconds_label(seconds)} ثانیه",
    ])


def recorded() -> str:
    return "پیام شما با موفقیت در پرونده ثبت شد. ⚖️"


def wrong_turn(role_label) -> str:
    return (f"⚠️ در این مرحله فقط {role_label} باید پیام بفرستد. "
            "لطفاً تا پایان مهلت صبر کنید.")


def too_short() -> str:
    return "⚠️ متن ارسال‌شده بسیار کوتاه است؛ لطفاً کمی توضیح بدهید."


def too_many_invalid(max_invalid) -> str:
    return (f"⚖️ به دلیل {persian_digits(max_invalid)} پاسخ نامعتبر، "
            "پرونده بسته شد و جایزه‌ای پرداخت نشد.")


def step_timeout(role_label) -> str:
    return "\n".join([
        "⚖️ پرونده لغو شد",
        (f"{role_label} در مهلت تعیین‌شده پیامی ارسال نکرد."),
        "طبق سیاست دادگاه، پرونده بدون جایزه خاتمه یافت.",
    ])


def judging() -> str:
    return "⚖️ دادگاه در حال بررسی شکایت، دفاعیات و شهادت‌ها است..."


def ai_error() -> str:
    return "\n".join([
        "⚖️ رأی دادگاه صادر نشد",
        "رکورد پرونده ناقص یا نامعتبر بود.",
        "طبق سیاست بازی، هیچ جایزه‌ای پرداخت نشد و مجازاتی اجرا نشد.",
        "برای تشکیل پرونده جدید دوباره «دادگاه» را ارسال کنید.",
    ])


def verdict_acquittal(defendant_display, reasons, coins, note=None) -> str:
    lines = [
        "⚖️ رأی نهایی دادگاه روباه",
        f"کاربر متهم: {defendant_display}",
        "متهم از اتهامات وارده تبرئه شد.",
        ("دادگاه پس از بررسی شکایت، دفاعیات و شهادت شاهدان، دلایل "
         "ارائه‌شده را برای اثبات اتهام کافی ندانست."),
        "دلایل رأی:",
    ]
    lines.extend(f"{index} - {reason}"
                 for index, reason in enumerate(reasons, 1))
    lines.append("نتیجه: دادگاه روباه رأی به بی‌گناهی متهم صادر کرد.")
    lines.append(f"🎁 جایزه: {persian_digits(coins)} سکه برنز به متهم تعلق گرفت.")
    if note:
        lines.append(note)
    lines.append("⚠️ این یک بازی نمایشی است و رأی دادگاه سند اثبات جرم واقعی نیست.")
    return "\n".join(lines)


def verdict_conviction(plaintiff_display, reasons, coins, note=None) -> str:
    lines = [
        "⚖️ رأی نهایی دادگاه روباه",
        f"کاربر شاکی: {plaintiff_display}",
        "متهم در این پرونده گناهکار شناخته شد.",
        ("دادگاه پس از بررسی اظهارات، دفاعیات و شهادت شاهدان، دلایل "
         "شاکی را معتبرتر و مرتبط‌تر با موضوع پرونده تشخیص داد."),
        "دلایل رأی:",
    ]
    lines.extend(f"{index} - {reason}"
                 for index, reason in enumerate(reasons, 1))
    lines.append("نتیجه: دادگاه روباه رأی به محکومیت متهم صادر کرد.")
    lines.append(f"🎁 جایزه: {persian_digits(coins)} سکه برنز به شاکی تعلق گرفت.")
    if note:
        lines.append(note)
    lines.append("⚠️ این یک بازی نمایشی است و رأی دادگاه سند اثبات جرم واقعی نیست.")
    return "\n".join(lines)


def punishment_notice(defendant_display) -> str:
    return "\n".join([
        "⚖️ حکم دادگاه اجرا شد.",
        (f"کاربر متهم: {defendant_display}، پیام‌های شما به مدت یک دقیقه "
         "پاک می‌شوند تا تنبیهی برای رفتار ثبت‌شده در این پرونده باشد."),
    ])


def punishment_report(deleted, failed) -> str:
    lines = [
        "⚖️ پایان اجرای حکم.",
        f"تعداد پیام‌های پاک‌شده: {persian_digits(deleted)}",
    ]
    if failed:
        lines.append(
            "⚠️ بخشی از پیام‌ها به دلیل محدودیت دسترسی ربات پاک نشد "
            f"({persian_digits(failed)} پیام)."
        )
    return "\n".join(lines)


def punishment_report_no_messages() -> str:
    return "\n".join([
        "⚖️ پایان اجرای حکم.",
        "در این یک دقیقه پیامی از متهم برای پاک کردن ثبت نشد.",
    ])


def reward_failed() -> str:
    return "⚠️ پرداخت جایزه با خطا مواجه شد و در گزارش ربات ثبت گردید."


def game_list_entry() -> str:
    """ردیف «لیست بازی» برای دادگاه روباه (موضوع بازی)."""
    return ("⚖️ دادگاه روباه\n"
            "پرونده شکایت رو حل کن و برنده شو\n\n")
