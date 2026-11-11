"""⛄️ بازی «اسم‌چین» — زنجیرهٔ نام‌های ایرانی با ربات (سروش پلاس).

قواعد، همان‌طور که کارفرما خواسته:

* ربات با یک حرفِ تصادفی شروع می‌کند و می‌گوید «یک اسم با «ل» بگو».
* **فقط** پیام‌هایی که به پیامِ ربات ریپلای شده‌اند پردازش می‌شوند؛ بقیه
  کاملاً نادیده گرفته می‌شوند (بازی هیچ‌وقت پیامِ آزادِ گروه را نمی‌خورد).
* اسم باید یک نامِ واقعیِ ایرانی (دخترانه یا پسرانه) باشد، با حرفِ درست
  شروع شود و در همین بازی قبلاً استفاده نشده باشد؛ وگرنه ربات **دلیل**
  ردشدن را می‌گوید و کاربر دوباره تلاش می‌کند.
* ربات با **حرفِ آخرِ** اسمِ کاربر یک نامِ ایرانی می‌گوید (لاله → هادی)،
  و حرفِ بعدی برای کاربر، حرفِ آخرِ اسمِ ربات است.
* هیچ نامی (نه از کاربر، نه از ربات) دوبار استفاده نمی‌شود.
* کاربر با «نمی‌دونم» (هر املایی) می‌بازد: «بازنده شدی ⛄️».
* اگر ربات برای حرفِ مورد نظر نامی نداشته باشد، «نمی‌دونم» می‌گوید و
  کاربر برنده می‌شود 🏆.

وضعیتِ بازی به تفکیکِ **چت** نگه‌داری می‌شود، پس بازیِ یک گروه هرگز با
گروهِ دیگر قاطی نمی‌شود.

دادهٔ نام‌ها: ``names.txt`` (چند صد نامِ ایرانی) که هنگامِ import یک‌بار
خوانده و در یک ``set`` و یک ``dict`` بر اساسِ حرفِ اول کش می‌شود.
"""
from __future__ import annotations

import random
import re
import time
import unicodedata
from pathlib import Path

from modules.name_chain import texts as T

__all__ = [
    "ALPHABET",
    "NAMES",
    "NAMES_BY_LETTER",
    "NAME_DISPLAY",
    "normalize",
    "compact",
    "is_unknown_phrase",
    "first_letter",
    "last_letter",
    "is_real_name",
    "is_available",
    "state",
    "is_active",
    "start",
    "record_prompt",
    "answer",
    "stop",
    "reset",
    "reply_message_id",
]

NAMES_FILE = Path(__file__).resolve().parent / "names.txt"

#: حروفِ الفبای فارسی به ترتیبی که برای شروعِ بازی معنی دارند.
ALPHABET = "ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی"

#: حداکثر فاصلهٔ زمانیِ یک بازیِ رهاشده (سه ساعت) تا حافظه بالا نرود.
MAX_IDLE_SECONDS = 3 * 60 * 60

#: حداکثر تعداد کلمهٔ یک نام (مثل «امیر علی»).
MAX_WORDS = 2

# ---------------------------------------------------------------------------
# نرمال‌سازی: ي/ك عربی → ی/ک فارسی، حذفِ اعراب و نیم‌فاصله و فاصله‌های اضافه
# ---------------------------------------------------------------------------
_CHAR_MAP = {
    "\u064a": "\u06cc",  # ي → ی
    "\u0649": "\u06cc",  # ى → ی
    "\u06c0": "\u0647",  # ۀ → ه
    "\u0643": "\u06a9",  # ك → ک
    "\u0629": "\u0647",  # ة → ه
    "\u0623": "\u0627",  # أ → ا
    "\u0625": "\u0627",  # إ → ا
    "\u0622": "\u0627",  # آ → ا (برای زنجیرهٔ حروف)
    "\u0624": "\u0648",  # ؤ → و
    "\u0626": "\u06cc",  # ئ → ی
}

#: نویسه‌های نامرئی/کنترل که حذف می‌شوند (نیم‌فاصله هم جزوِ همین‌هاست).
_INVISIBLE = (
    "\u200c", "\u200b", "\u200d", "\u200e", "\u200f",
    "\ufeff", "\u0640", "\u0654", "\u0655",
)

#: اعراب و تشدید.
_DIACRITICS = re.compile(r"[\u064b-\u0652\u0670]")

#: نشانه‌هایی که به فاصله تبدیل می‌شوند تا «لاله!» با «لاله» یکی شود.
_PUNCTUATION = frozenset(
    "«»‹›\"'`.,:;!?؟،؛…-_+=*/\\|()[]{}#@&^%~$✍️⛄️🤖🏆"
)

#: «نمی‌دونم» با هر املایی (پس از نرمال‌سازی و حذفِ فاصله‌ها مقایسه می‌شود).
_UNKNOWN_PHRASES = frozenset({
    "نمیدونم",
    "نمیدانم",
    "نمدونم",
    "نمیدونیم",
    "نمیدانیم",
    "بلدنیستم",
    "نمیفهمم",
})


def normalize(text) -> str:
    """شکلِ یکسان‌شدهٔ متن برای مقایسه (و برای کلیدِ نام‌ها).

    >>> normalize("  لالـه!  ")
    'لاله'
    >>> normalize("نمى‌دونم")
    'نمیدونم'
    """
    value = str(text or "").strip()
    if not value:
        return ""
    for source, target in _CHAR_MAP.items():
        value = value.replace(source, target)
    for ch in _INVISIBLE:
        value = value.replace(ch, "")
    value = _DIACRITICS.sub("", value)
    value = "".join(" " if ch in _PUNCTUATION else ch for ch in value)
    value = unicodedata.normalize("NFC", value)
    return " ".join(value.split()).lower()


def compact(text) -> str:
    """متنِ نرمال‌شده بدونِ فاصله — برای مقایسهٔ «نمی‌دونم»/«نمی دونم»."""
    return normalize(text).replace(" ", "")


def is_unknown_phrase(text) -> bool:
    """آیا کاربر گفت «نمی‌دونم» (با هر املایی)؟"""
    return compact(text) in _UNKNOWN_PHRASES


def first_letter(text) -> str:
    key = normalize(text)
    return key[0] if key else ""


def last_letter(text) -> str:
    key = normalize(text)
    return key[-1] if key else ""


# ---------------------------------------------------------------------------
# بارگذاریِ نام‌ها (یک‌بار، هنگامِ بالا آمدنِ ربات)
# ---------------------------------------------------------------------------
def _load_names():
    display = {}
    try:
        raw_lines = NAMES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        raw_lines = []
    for line in raw_lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        key = normalize(raw)
        if not key or key in display:
            continue
        display[key] = raw
    by_letter = {}
    for key in sorted(display):
        by_letter.setdefault(key[0], []).append(key)
    return display, {letter: tuple(keys) for letter, keys in by_letter.items()}


#: نامِ نمایشی هر کلید (حرفِ «آ» در نمایش حفظ می‌شود).
NAME_DISPLAY, NAMES_BY_LETTER = _load_names()

#: مجموعهٔ همهٔ نام‌ها (کلیدِ نرمال‌شده).
NAMES = frozenset(NAME_DISPLAY)
NAMES_BY_LETTER = dict(NAMES_BY_LETTER)


def is_available() -> bool:
    """آیا استخرِ نام‌ها بارگذاری شده است؟"""
    return bool(NAMES)


def is_real_name(text) -> bool:
    """آیا این متن یک نامِ واقعیِ ایرانی است؟

    اول استخرِ ``names.txt``، بعد دیتابیسِ بزرگِ ``modules.persian_names``
    (۲۴ هزار نام) تا کاربر بابتِ نامِ درست و خارج از استخر، جریمه نشود.
    """
    key = normalize(text)
    if not key:
        return False
    if key in NAMES:
        return True
    try:
        from modules.persian_names import is_known_name

        return bool(is_known_name(text))
    except Exception:  # noqa: BLE001 — نبودِ دیتابیس نباید بازی را بشکند
        return False


# ---------------------------------------------------------------------------
# وضعیت — به تفکیکِ چت
# ---------------------------------------------------------------------------
_STATE = {}


def _chat_key(chat_id) -> str:
    """کلیدِ یکسانِ گروه (``-100…`` و شکلِ کوتاه، یک رکورد بیشتر نسازند)."""
    try:
        from modules.group_id import normalize_group_id

        return str(normalize_group_id(int(chat_id)))
    except Exception:  # noqa: BLE001
        return str(chat_id)


def state(chat_id):
    """وضعیتِ بازیِ همین چت، یا ``None`` اگر بازی‌ای در جریان نیست."""
    entry = _STATE.get(_chat_key(chat_id))
    if entry is None:
        return None
    if time.time() - float(entry.get("started_at") or 0) > MAX_IDLE_SECONDS:
        _STATE.pop(_chat_key(chat_id), None)
        return None
    return entry


def is_active(chat_id) -> bool:
    return state(chat_id) is not None


def stop(chat_id) -> None:
    _STATE.pop(_chat_key(chat_id), None)


def reset() -> None:
    """پاک‌کردنِ کاملِ وضعیت (تست/ری‌استارت)."""
    _STATE.clear()


# ---------------------------------------------------------------------------
# شروعِ بازی
# ---------------------------------------------------------------------------
def _start_letters():
    """حرف‌هایی که شروعِ بازی با آن‌ها منصفانه است (دست‌کم سه نام)."""
    letters = [letter for letter in ALPHABET if NAMES_BY_LETTER.get(letter)]
    rich = [letter for letter in letters if len(NAMES_BY_LETTER.get(letter, ())) >= 3]
    return rich or letters


def start(chat_id, letter=None):
    """یک دورِ تازه شروع می‌کند و متنِ آمادهٔ ارسال را برمی‌گرداند.

    خروجی: ``{"letter":…, "text":…}`` یا ``None`` اگر استخرِ نام‌ها خالی باشد.
    """
    if not is_available():
        return None
    if letter is None:
        pool = _start_letters()
        if not pool:
            return None
        letter = random.choice(pool)
    else:
        letter = first_letter(letter)
        if not letter:
            return None
    _STATE[_chat_key(chat_id)] = {
        "letter": letter,
        "used": set(),
        "prompt_id": None,
        "last_bot": None,
        "turns": 0,
        "started_at": time.time(),
    }
    return {"letter": letter, "text": T.start_text(letter)}


def record_prompt(chat_id, message_id) -> bool:
    """شناسهٔ پیامِ ربات را ثبت می‌کند (همان پیامی که باید رویش ریپلای شود)."""
    entry = state(chat_id)
    if entry is None:
        return False
    try:
        value = int(message_id)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    entry["prompt_id"] = value
    return True


# ---------------------------------------------------------------------------
# پاسخِ کاربر
# ---------------------------------------------------------------------------
def _pick_bot_name(letter, used):
    """یک نامِ استفاده‌نشده با حرفِ ``letter`` انتخاب می‌کند.

    ترجیح با نام‌هایی است که حرفِ آخرشان هم نام دارد، تا بازی به‌طورِ
    طبیعی ادامه پیدا کند و ربات با «بن‌بست» یک‌طرفه برنده نشود. اگر چنین
    نامی نباشد، از میانِ بقیه انتخاب می‌شود (و ممکن است نوبت به کاربر
    بسته شود — همان قاعدهٔ بازی).
    """
    candidates = [key for key in NAMES_BY_LETTER.get(letter, ()) if key not in used]
    if not candidates:
        return None
    continuable = [
        key for key in candidates
        if any(other not in used for other in NAMES_BY_LETTER.get(key[-1], ()))
    ]
    return random.choice(sorted(continuable or candidates))


def answer(chat_id, text, *, reply_id=None, bot_user_id=None, reply_user_id=None):
    """پاسخِ یک پیامِ ریپلای‌شده را پردازش می‌کند.

    خروجی ``None`` یعنی «این پیام به بازی مربوط نیست» (بدونِ ریپلای، ریپلای
    روی پیامِ دیگری، ...). در غیرِ این صورت یک dict با کلیدِ ``kind``:

    * ``"invalid"``   — رد شد؛ ``message`` دلیل را می‌گوید.
    * ``"accepted"``  — قبول شد؛ ربات جواب داد و ``next_letter`` حرفِ بعدی است.
    * ``"user_lost"`` — کاربر «نمی‌دونم» گفت.
    * ``"bot_lost"``  — ربات اسمی نداشت؛ کاربر برنده شد.
    """
    entry = state(chat_id)
    if entry is None:
        return None

    raw = str(text or "").strip()
    if not raw:
        return None

    # ۱) فقط ریپلای روی پیامِ ربات.
    if reply_id is None:
        return None
    try:
        reply_id = int(reply_id)
    except (TypeError, ValueError):
        return None
    prompt_id = entry.get("prompt_id")
    if prompt_id is not None:
        if reply_id != int(prompt_id):
            return None
    elif bot_user_id is not None and reply_user_id is not None:
        # شناسهٔ پیام را نداریم؛ حداقل مطمئن می‌شویم ریپلای روی خودِ ربات است.
        try:
            if int(reply_user_id) != int(bot_user_id):
                return None
        except (TypeError, ValueError):
            return None

    # ۲) «نمی‌دونم» = پایانِ بازی و باختِ کاربر.
    if is_unknown_phrase(raw):
        stop(chat_id)
        return {"kind": "user_lost", "message": T.USER_LOST}

    key = normalize(raw)
    if not key:
        return None
    letter = entry["letter"]

    if len(key.split()) > MAX_WORDS:
        return {
            "kind": "invalid",
            "reason": "not-a-word",
            "message": T.not_a_word(letter),
        }
    if key[0] != letter:
        return {
            "kind": "invalid",
            "reason": "wrong-letter",
            "message": T.wrong_letter(raw, letter),
        }
    if key in entry["used"]:
        return {
            "kind": "invalid",
            "reason": "duplicate",
            "message": T.duplicate(raw, letter),
        }
    if not is_real_name(raw):
        return {
            "kind": "invalid",
            "reason": "not-a-name",
            "message": T.not_a_name(raw, letter),
        }

    # ۳) قبول: نامِ کاربر مصرف می‌شود و ربات با حرفِ آخر جواب می‌دهد.
    entry["used"].add(key)
    entry["turns"] = int(entry.get("turns") or 0) + 1
    link_letter = key[-1]

    bot_key = _pick_bot_name(link_letter, entry["used"])
    if bot_key is None:
        stop(chat_id)
        return {
            "kind": "bot_lost",
            "user_name": raw,
            "letter": link_letter,
            "message": T.bot_lost(link_letter),
        }

    entry["used"].add(bot_key)
    entry["last_bot"] = bot_key
    entry["letter"] = bot_key[-1]
    return {
        "kind": "accepted",
        "user_name": raw,
        "bot_name": NAME_DISPLAY.get(bot_key, bot_key),
        "link_letter": link_letter,
        "next_letter": entry["letter"],
        "message": T.bot_turn(
            raw, NAME_DISPLAY.get(bot_key, bot_key), entry["letter"]
        ),
    }


# ---------------------------------------------------------------------------
# کمکیِ هندلر: شناسهٔ پیامی که این پیام روی آن ریپلای شده
# ---------------------------------------------------------------------------
def reply_message_id(event):
    message = getattr(event, "message", None)
    message_reply = getattr(message, "reply_to", None)
    return (
        getattr(message, "reply_to_msg_id", None)
        or getattr(message_reply, "reply_to_msg_id", None)
        or getattr(event, "reply_to_msg_id", None)
        or getattr(getattr(event, "reply_to", None), "reply_to_msg_id", None)
    )


def reply_sender_id(event):
    """شناسهٔ فرستندهٔ پیامی که این پیام روی آن ریپلای شده (اگر باشد)."""
    message = getattr(event, "message", None)
    reply_to = getattr(message, "reply_to", None) or getattr(event, "reply_to", None)
    if reply_to is None:
        return None
    for attr in ("from_id", "user_id", "sender_id"):
        value = getattr(reply_to, attr, None)
        if value is None:
            continue
        inner = getattr(value, "user_id", value)
        try:
            return int(inner)
        except (TypeError, ValueError):
            continue
    return None
