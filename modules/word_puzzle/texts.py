"""🧩 متن‌های بازی «پازل کلمه» — فارسی و دوستانه."""
from __future__ import annotations

import re

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

#: فاصلهٔ بین حروفِ جدول — حروف نباید به هم بچسبند.
SEPARATOR = " - "

#: مهلتِ هر مرحله (ثانیه).
TIME_LIMIT = 45

#: جایزهٔ هر مرحله — ۳ سکهٔ نقره از سیستمِ اقتصادِ خودِ ربات.
REWARD_AMOUNT = 3

HEADER_TEMPLATE = "🧩 مرحله {stage} از {total}"
INSTRUCTION_TEMPLATE = "کلمه‌ی مخفی رو پیدا کن ({letters} حرفی)"
TIME_TEMPLATE = "🎮 مهلت زمانی {seconds} ثانیه"

CORRECT_TEMPLATE = "آفرین ✅ {amount} سکه نقره به موجودیت اضافه شد 🔖"
WRONG_TEXT = "❌ این کلمه درست نیست.\nهنوز وقت داری — دوباره تلاش کن ⏳"

TIMEOUT_TEMPLATE = (
    "⏰ زمان تمام شد!\n"
    "\n"
    "✅ کلمه‌ی درست: «{word}»\n"
    "\n"
    "برای ادامه، دوباره بنویس: پازل کلمه"
)

FINISHED_TEMPLATE = (
    "🏆 تبریک! همه‌ی {total} مرحله‌ی پازل کلمه را تمام کردی 🎉\n"
    "\n"
    "دیگه هیچ کلمه‌ای برای تو مخفی نمانده ⛄️"
)

NOT_AVAILABLE_TEXT = "🧩 پازل کلمه همین حالا در دسترس نیست؛ بعداً دوباره امتحان کن."


def fa(value) -> str:
    """عدد با رقم‌های فارسی."""
    return str(value).translate(_PERSIAN_DIGITS)


def _utf16_len(text: str) -> int:
    """طولِ متن بر حسبِ واحدِ UTF-16 (همان چیزی که فرمت‌بندیِ پیام می‌خواهد)."""
    return len(text.encode("utf-16-le")) // 2


def stage_text(stage, total, rows, letters_count):
    """متنِ کاملِ یک مرحله + بازهٔ حروفی که باید Bold شوند.

    ساختار:

        🧩 مرحله ۱ از ۲۰۰

        پ - ع - ش
        د - ل - ق
        کلمه‌ی مخفی رو پیدا کن (۳ حرفی)

        🎮 مهلت زمانی ۴۵ ثانیه
    """
    lines: list[str] = []
    spans: list[tuple[int, int]] = []

    def push_plain(line):
        lines.append(line)

    def push_row(row):
        # طولِ متنِ ساخته‌شده تا قبل از این خط (به‌همراهِ \n پیش از آن)
        base = _utf16_len("\n".join(lines))
        if lines:
            base += 1
        position = base
        for index, char in enumerate(row):
            if index:
                position += _utf16_len(SEPARATOR)
            spans.append((position, _utf16_len(char)))
            position += _utf16_len(char)
        lines.append(SEPARATOR.join(row))

    push_plain(HEADER_TEMPLATE.format(stage=fa(stage), total=fa(total)))
    push_plain("")
    for row in rows:
        push_row(row)
    push_plain(INSTRUCTION_TEMPLATE.format(letters=fa(letters_count)))
    push_plain("")
    push_plain(TIME_TEMPLATE.format(seconds=fa(TIME_LIMIT)))
    return "\n".join(lines), spans


def correct_text(amount=REWARD_AMOUNT) -> str:
    return CORRECT_TEMPLATE.format(amount=fa(amount))


def timeout_text(word) -> str:
    return TIMEOUT_TEMPLATE.format(word=word)


def finished_text(total) -> str:
    return FINISHED_TEMPLATE.format(total=fa(total))


def shift_spans(spans, prefix: str):
    """بازه‌های Bold را بعد از افزودنِ یک متن به ابتدای پیام جابه‌جا می‌کند."""
    offset = _utf16_len(prefix)
    return [(start + offset, length) for start, length in spans]


_DIGIT_CHECK = re.compile(r"[0-9۰-۹]")
