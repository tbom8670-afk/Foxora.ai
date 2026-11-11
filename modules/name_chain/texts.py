"""⛄️ متن‌های بازی «اسم‌چین» — همه فارسی و دوستانه.

متنِ شروع **عیناً** همان چیزی است که کارفرما مشخص کرده است؛ تنها جای حرف
تصادفی عوض می‌شود (``{letter}``).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# شروع بازی — عیناً مطابقِ متنِ کارفرما
# ---------------------------------------------------------------------------
START_TEMPLATE = (
    "⛄️بازی اسم چین\n"
    "\n"
    "دقت کن دوست، هرجا که بلد نبودی اسمی بگی بگو «نمیدونم» تا بازنده بشی، "
    "یا برای تموم کردن بازی هم بگو «نمیدونم».\n"
    "\n"
    "برای جواب گرفتن باید هر پیام رو روی ربات ریپلای کنی.\n"
    "\n"
    "حالا برای شروع یک اسم با «{letter}» بگو"
)

# ---------------------------------------------------------------------------
# نوبتِ ربات
# ---------------------------------------------------------------------------
BOT_TURN_TEMPLATE = (
    "✅ «{name}» درست بود!\n"
    "\n"
    "🤖 من می‌گم: «{bot_name}»\n"
    "\n"
    "حالا تو یک اسم با «{next_letter}» بگو — یادت نره پیامت رو روی همین پیام "
    "ریپلای کنی."
)

# ---------------------------------------------------------------
# پاسخ‌های ردشدن (با دلیل، تا کاربر دوباره تلاش کند)
# ---------------------------------------------------------------
WRONG_LETTER_TEMPLATE = (
    "❌ «{name}» با «{letter}» شروع نمی‌شه.\n"
    "یک اسم ایرانی با «{letter}» بگو 🙏"
)

DUPLICATE_TEMPLATE = (
    "🔁 «{name}» قبلاً توی همین بازی استفاده شده.\n"
    "یک اسم دیگه با «{letter}» بگو 🙏"
)

NOT_A_NAME_TEMPLATE = (
    "🤔 «{name}» رو به‌عنوان اسم ایرانی نمی‌شناسم.\n"
    "یک اسم واقعی (دخترانه یا پسرانه) با «{letter}» بگو 🙏"
)

NOT_A_WORD_TEMPLATE = (
    "✍️ فقط اسم بفرست — یک یا دو کلمه.\n"
    "یک اسم ایرانی با «{letter}» بگو 🙏"
)

# ---------------------------------------------------------------
# پایان بازی
# ---------------------------------------------------------------
#: وقتی کاربر «نمی‌دونم» می‌گوید — عیناً همین جمله.
USER_LOST = "بازنده شدی ⛄️"

#: وقتی ربات برای حرفِ مورد نظر اسمی ندارد.
BOT_LOST_TEMPLATE = (
    "🤖 نمی‌دونم!\n"
    "\n"
    "برای «{letter}» اسمی به ذهنم نمی‌رسه.\n"
    "\n"
    "🏆 تو بردی! 🎉\n"
    "\n"
    "برای بازی دوباره بنویس: اسم چین"
)

#: اگر فایلِ نام‌ها در دسترس نباشد.
UNAVAILABLE = "⛄️ بازی اسم چین همین حالا در دسترس نیست؛ بعداً دوباره امتحان کن."


def start_text(letter):
    return START_TEMPLATE.format(letter=letter)


def bot_turn(name, bot_name, next_letter):
    return BOT_TURN_TEMPLATE.format(
        name=name, bot_name=bot_name, next_letter=next_letter
    )


def wrong_letter(name, letter):
    return WRONG_LETTER_TEMPLATE.format(name=name, letter=letter)


def duplicate(name, letter):
    return DUPLICATE_TEMPLATE.format(name=name, letter=letter)


def not_a_name(name, letter):
    return NOT_A_NAME_TEMPLATE.format(name=name, letter=letter)


def not_a_word(letter):
    return NOT_A_WORD_TEMPLATE.format(letter=letter)


def bot_lost(letter):
    return BOT_LOST_TEMPLATE.format(letter=letter)
