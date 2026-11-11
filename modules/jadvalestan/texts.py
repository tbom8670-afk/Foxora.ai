"""⛄️ متن‌های بازی «جدولستان» — فارسی و دوستانه."""
from __future__ import annotations

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(value) -> str:
    """عدد را با رقم‌های فارسی برمی‌گرداند (۳۲۰ → ۳۲۰)."""
    return str(value).translate(_PERSIAN_DIGITS)


#: سقفِ زمانِ هر مرحله (ثانیه). نمایشِ «مهلت زمانی» هم از همین عدد می‌آید.
TIME_LIMIT = 60

#: جایزهٔ هر مرحله — سکهٔ نقره (از جدولِ رسمیِ اقتصاد خوانده می‌شود).
REWARD_AMOUNT = 3
REWARD_COIN_NAME = "نقره"

HEADER_TEMPLATE = "🧩 جدولستان | مرحله {stage} از {total}"
TIME_TEMPLATE = "🎮 مهلت زمانی {seconds} ثانیه"

CORRECT_TEMPLATE = "آفرین ✅ {amount} سکه نقره به موجودیت اضافه شد 🔖"

WRONG_TEXT = "❌ نه، این جواب درست نیست.\nهنوز وقت داری — دوباره تلاش کن ⏳"

TIMEOUT_TEMPLATE = (
    "⏰ زمان تمام شد!\n"
    "\n"
    "✅ جواب درست: «{answer}»\n"
    "\n"
    "برای ادامه، دوباره بنویس: جدولستان"
)

FINISHED_TEMPLATE = (
    "🏆 تبریک! همهٔ {total} مرحلهٔ جدولستان را تمام کردی 🎉\n"
    "\n"
    "تو یک جدول‌بازِ حرفه‌ای هستی ⛄️"
)

BUSY_TEXT = "⏳ یک مرحلهٔ باز از جدولستان داری؛ اول همان را جواب بده."

NOT_AVAILABLE_TEXT = "🧩 جدولستان همین حالا در دسترس نیست؛ بعداً دوباره امتحان کن."


def stage_text(stage, total, question, letters) -> str:
    """متنِ کاملِ یک مرحله — دقیقاً به شکلِ جدولِ روزنامه.

    تعدادِ حروف و جای خالی‌ها از روی خودِ جواب حساب می‌شوند (ورودیِ
    ``letters``)؛ هیچ عددی دستی نوشته نمی‌شود.
    """
    blanks = " ".join("_" for _ in range(int(letters)))
    return (
        f"{HEADER_TEMPLATE.format(stage=fa(stage), total=fa(total))}\n"
        "\n"
        f"{question}\n"
        "\n"
        f"{blanks}  ({fa(letters)} حرفی)\n"
        "\n"
        f"{TIME_TEMPLATE.format(seconds=fa(TIME_LIMIT))}"
    )


def correct_text(amount=REWARD_AMOUNT) -> str:
    return CORRECT_TEMPLATE.format(amount=fa(amount))


def timeout_text(answer) -> str:
    return TIMEOUT_TEMPLATE.format(answer=answer)


def finished_text(total) -> str:
    return FINISHED_TEMPLATE.format(total=fa(total))
