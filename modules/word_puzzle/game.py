"""🧩 بازی «پازل کلمه» — ۲۰۰ مرحله، تک‌نفره.

در هر مرحله یک کلمهٔ مخفیِ فارسی داریم. ربات حروفِ آن کلمه را به‌همراهِ چند
حرفِ الکی در یک جدولِ کوچک نشان می‌دهد و کاربر باید کلمه را پیدا کند.

قاعده‌ها:

* ۲۰۰ مرحله، ۲۰۰ کلمهٔ متفاوت (از ۳ حرفی تا ۹ حرفی، ساده → سخت).
* تعدادِ حروفِ الکی با بالا رفتنِ مرحله بیشتر می‌شود (۳، ۴، ۵ … تا ۱۲).
* حرفِ الکی هیچ‌وقت از حروفِ خودِ کلمه نیست و بینِ خودشان هم تکراری نیستند.
* ۵ مرحلهٔ اول: حروفِ کلمه به ترتیبِ درست در جدول‌اند؛ از مرحلهٔ ۶ همه قاطی.
* فقط صاحبِ بازی جواب می‌دهد؛ ۴۵ ثانیه مهلت؛ جوابِ درست = ۳ سکهٔ نقره.
* پیشرفت برای همیشه در دیسک می‌ماند و کلمه هرگز برای همان کاربر تکرار نمی‌شود.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import unicodedata
import uuid
from pathlib import Path

from modules.word_puzzle import storage
from modules.word_puzzle import texts as T

__all__ = [
    "GAME_KEY",
    "WORDS",
    "TOTAL",
    "TIME_LIMIT",
    "ORDERED_STAGES",
    "STAGE_COLUMNS",
    "fake_count",
    "grid_rows",
    "stage_payload",
    "normalize",
    "compact",
    "letters_of",
    "word_for_stage",
    "start",
    "answer",
    "timeout",
    "active",
    "current",
    "reset",
    "completed_stage",
    "next_stage_number",
]

logger = logging.getLogger("word_puzzle")

GAME_KEY = "word_puzzle"
WORDS_FILE = Path(__file__).resolve().parent / "words.json"
TIME_LIMIT = T.TIME_LIMIT

#: در این مرحله‌ها حروفِ کلمه به ترتیبِ درست می‌آیند؛ بعد از آن همه قاطی.
ORDERED_STAGES = 5

#: جدول: تا ۹ خانه ۳ ستون (دقیقاً مثلِ نمونهٔ کارفرما)، بعد از آن ستون‌ها
#: طوری انتخاب می‌شوند که سطرِ آخر تک‌حرفی و بی‌ریخت نماند.
STAGE_COLUMNS = 3
TINY_GRID = 9
COLUMN_CANDIDATES = (3, 4, 5)

#: سقفِ حروفِ الکی تا جدول از کنترل خارج نشود.
MAX_FAKES = 12

MAX_IDLE_SECONDS = 6 * 60 * 60

#: الفبای فارسی برای ساختنِ حروفِ الکی.
ALPHABET = "ابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی"


# ---------------------------------------------------------------------------
# بارگذاریِ کلمه‌ها
# ---------------------------------------------------------------------------
def _load_words():
    try:
        raw = json.loads(WORDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("word_puzzle words.json is unavailable")
        return []
    items = raw.get("words") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    words = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        words.append({"stage": int(item.get("stage") or index), "word": word})
    words.sort(key=lambda item: item["stage"])
    return words


WORDS = _load_words()
TOTAL = len(WORDS)
_BY_STAGE = {item["stage"]: item for item in WORDS}

_PERSIAN_WORD = re.compile(r"^[\u0621-\u06CC]+$")


def word_for_stage(stage):
    try:
        return _BY_STAGE[int(stage)]
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# نرمال‌سازیِ ورودی
# ---------------------------------------------------------------------------
_CHAR_MAP = {
    "\u064a": "\u06cc",  # ي → ی
    "\u0649": "\u06cc",  # ى → ی
    "\u0643": "\u06a9",  # ك → ک
    "\u06c0": "\u0647",  # ۀ → ه
    "\u0629": "\u0647",  # ة → ه
    "\u0623": "\u0627",  # أ → ا
    "\u0625": "\u0627",  # إ → ا
    "\u0622": "\u0627",  # آ → ا
    "\u0624": "\u0648",  # ؤ → و
    "\u0626": "\u06cc",  # ئ → ی
}
_INVISIBLE = ("\u200c", "\u200b", "\u200d", "\u200e", "\u200f", "\ufeff", "\u0640")
_DIACRITICS = re.compile(r"[\u064b-\u0652\u0670]")
_PUNCTUATION = frozenset("«»‹›\"'`.,:;!?؟،؛…-_+=*/\\|()[]{}#@&^%~$")


def normalize(text) -> str:
    """«ي/ك» عربی → فارسی، حذفِ اعراب و نیم‌فاصله و فاصله‌های اضافه."""
    value = unicodedata.normalize("NFC", str(text or "")).strip()
    if not value:
        return ""
    for source, target in _CHAR_MAP.items():
        value = value.replace(source, target)
    for char in _INVISIBLE:
        value = value.replace(char, "")
    value = _DIACRITICS.sub("", value)
    value = "".join(" " if char in _PUNCTUATION else char for char in value)
    return " ".join(value.split()).lower()


def compact(text) -> str:
    return normalize(text).replace(" ", "")


def letters_of(word) -> int:
    """تعدادِ حروفِ کلمه — همیشه از روی خودِ کلمه."""
    return len(compact(word))


# ---------------------------------------------------------------------------
# جدولِ حروف
# ---------------------------------------------------------------------------
def fake_count(stage) -> int:
    """تعدادِ حروفِ الکی — با هر ۱۰ مرحله یکی بیشتر (۳، ۳ … ۴ … ۱۲)."""
    try:
        stage = max(1, int(stage))
    except (TypeError, ValueError):
        stage = 1
    return min(MAX_FAKES, 3 + (stage - 1) // 10)


def _rng_for(stage) -> random.Random:
    """تولیدِ تصادفیِ پایدار: هر مرحله همیشه همان جدول را می‌دهد."""
    return random.Random(9176 + int(stage))


def _cells_for(stage, word) -> list[str]:
    """خانه‌های جدول: حروفِ کلمه + حروفِ الکی (بدونِ تکرار، بیگانه با کلمه)."""
    rng = _rng_for(stage)
    letters = list(word)
    for char in letters:
        if not _PERSIAN_WORD.match(char):
            raise ValueError(f"کلمه‌ی غیرِفارسی: {word!r}")
    forbidden = set(letters)
    available = [char for char in ALPHABET if char not in forbidden]
    need = fake_count(stage)
    fakes = rng.sample(available, need)
    if int(stage) <= ORDERED_STAGES:
        # مراحلِ اول: حروفِ کلمه به ترتیبِ درست، بعد حروفِ الکی.
        return letters + fakes
    cells = letters + fakes
    rng.shuffle(cells)
    return cells


def columns_for(cell_count) -> int:
    """تعدادِ ستون‌ها: سطرِ آخر نباید فقط یک حرف داشته باشد."""
    cells = int(cell_count)
    best_columns, best_score = STAGE_COLUMNS, None
    for columns in COLUMN_CANDIDATES:
        rows = -(-cells // columns)          # سقفِ تقسیم
        last = cells - (rows - 1) * columns
        ragged = 1 if (last == 1 and rows > 1) else 0
        score = (ragged, columns - last, abs(columns - 4), rows)
        if best_score is None or score < best_score:
            best_columns, best_score = columns, score
    return best_columns


def grid_rows(stage, word=None) -> list[list[str]]:
    """جدول به شکلِ سطرهایی از حروف."""
    item = word_for_stage(stage) if word is None else {"word": word}
    if item is None:
        return []
    cells = _cells_for(stage, item["word"])
    columns = columns_for(len(cells))
    return [
        cells[index:index + columns]
        for index in range(0, len(cells), columns)
    ]


def stage_payload(stage) -> dict | None:
    """اطلاعاتِ نمایشیِ یک مرحله (متن + بازهٔ حروفِ Bold)."""
    item = word_for_stage(stage)
    if item is None:
        return None
    rows = grid_rows(stage)
    letters = letters_of(item["word"])
    text, spans = T.stage_text(stage, TOTAL, rows, letters)
    return {
        "stage": int(stage),
        "total": TOTAL,
        "word": item["word"],
        "letters": letters,
        "rows": rows,
        "fakes": fake_count(stage),
        "text": text,
        "spans": spans,
    }


# ---------------------------------------------------------------------------
# وضعیتِ لحظه‌ای — به تفکیکِ (گروه، کاربر)
# ---------------------------------------------------------------------------
_ACTIVE = {}


def _key(chat_id, user_id) -> tuple:
    return (storage.chat_key(chat_id), str(user_id))


def current(chat_id, user_id):
    entry = _ACTIVE.get(_key(chat_id, user_id))
    if entry is None:
        return None
    if time.time() - float(entry.get("started") or 0) > MAX_IDLE_SECONDS:
        _ACTIVE.pop(_key(chat_id, user_id), None)
        return None
    return entry


def active(chat_id, user_id) -> bool:
    return current(chat_id, user_id) is not None


def reset() -> None:
    _ACTIVE.clear()


def completed_stage(chat_id, user_id) -> int:
    return storage.completed_stage(chat_id, user_id)


def next_stage_number(chat_id, user_id) -> int:
    return completed_stage(chat_id, user_id) + 1


def _open_stage(chat_id, user_id):
    stage = next_stage_number(chat_id, user_id)
    payload = stage_payload(stage)
    if payload is None:
        return None
    token = uuid.uuid4().hex
    _ACTIVE[_key(chat_id, user_id)] = {
        "stage": stage,
        "token": token,
        "started": time.time(),
        "tries": 0,
    }
    payload = dict(payload)
    payload["token"] = token
    return payload


# ---------------------------------------------------------------------------
# شروع
# ---------------------------------------------------------------------------
def start(chat_id, user_id):
    """بازی را از مرحلهٔ جاریِ کاربر باز می‌کند.

    * ``{"kind": "finished", ...}`` — هر ۲۰۰ مرحله تمام شده.
    * ``{"kind": "started", "stage", "text", "spans", "token", ...}``
    * ``None`` — فایلِ کلمه‌ها در دسترس نیست.
    """
    if not WORDS:
        return None
    if completed_stage(chat_id, user_id) >= TOTAL:
        text = T.finished_text(TOTAL)
        return {
            "kind": "finished",
            "message": text,
            "text": text,
            "spans": [],
            "total": TOTAL,
        }
    payload = _open_stage(chat_id, user_id)
    if payload is None:
        return None
    payload["kind"] = "started"
    return payload


# ---------------------------------------------------------------------------
# پاسخِ کاربر
# ---------------------------------------------------------------------------
def _award(chat_id, user_id, stage) -> bool:
    """۳ سکهٔ نقره از سیستمِ اقتصادِ ربات — مرجعِ یکتا ⇒ پاداشِ تکراری ممنوع."""
    reference = f"{GAME_KEY}:{storage.chat_key(chat_id)}:{user_id}:{int(stage)}"
    try:
        from economy import award_game

        award_game(chat_id, user_id, GAME_KEY, reference=reference)
        return True
    except Exception as error:  # noqa: BLE001
        logger.warning("word_puzzle award failed stage=%s error=%r", stage, error)
        return False


def answer(chat_id, user_id, text):
    """پاسخِ صاحبِ بازی را بررسی می‌کند.

    ``None`` ⇒ این پیام به بازیِ این کاربر مربوط نیست (فقط صاحبِ بازی).
    """
    entry = current(chat_id, user_id)
    if entry is None:
        return None
    guess = str(text or "").strip()
    if not guess:
        return None

    stage = int(entry["stage"])
    item = word_for_stage(stage)
    if item is None:
        return None

    if compact(guess) != compact(item["word"]):
        entry["tries"] = int(entry.get("tries") or 0) + 1
        return {
            "kind": "wrong",
            "stage": stage,
            "tries": entry["tries"],
            "message": T.WRONG_TEXT,
            "text": T.WRONG_TEXT,
            "spans": [],
        }

    awarded = _award(chat_id, user_id, stage)
    storage.complete_stage(chat_id, user_id, stage)
    _ACTIVE.pop(_key(chat_id, user_id), None)

    reward_line = T.correct_text() if awarded else "آفرین ✅ درست بود 🔖"
    if stage >= TOTAL:
        message = f"{reward_line}\n\n{T.finished_text(TOTAL)}"
        return {
            "kind": "finished",
            "stage": stage,
            "awarded": awarded,
            "message": message,
            "text": message,
            "spans": [],
        }

    following = _open_stage(chat_id, user_id)
    head = f"{reward_line}\n\n"
    message = head + following["text"]
    return {
        "kind": "correct",
        "stage": stage,
        "awarded": awarded,
        "next_stage": following["stage"],
        "next_token": following["token"],
        "next_text": following["text"],
        "message": message,
        "text": message,
        "spans": T.shift_spans(following["spans"], head),
    }


def timeout(chat_id, user_id, token):
    """پایانِ مهلت: کلمهٔ درست اعلام و دور بسته می‌شود.

    اگر توکن عوض شده باشد (کاربر در همین فاصله درست جواب داد) ``None``.
    """
    entry = current(chat_id, user_id)
    if entry is None:
        return None
    if token is not None and str(entry.get("token")) != str(token):
        return None
    stage = int(entry["stage"])
    item = word_for_stage(stage)
    _ACTIVE.pop(_key(chat_id, user_id), None)
    if item is None:
        return None
    message = T.timeout_text(item["word"])
    return {
        "kind": "timeout",
        "stage": stage,
        "word": item["word"],
        "message": message,
        "text": message,
        "spans": [],
    }
