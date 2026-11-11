"""🧩 بازی «جدولستان» — جدولِ روزنامه‌ای، تک‌نفره، ۳۲۰ مرحله.

قواعد:

* ربات یک سوال می‌پرسد، تعدادِ حروفِ جواب را (که **از روی خودِ جواب** حساب
  می‌شود) به شکلِ جای خالی نشان می‌دهد و ۶۰ ثانیه وقت می‌دهد.
* فقط صاحبِ بازی می‌تواند جواب بدهد؛ پیامِ بقیه در این بازی هیچ اثری ندارد.
* جوابِ درست ⇒ ۳ سکهٔ نقره از سیستمِ اقتصادِ خودِ ربات + مرحلهٔ بعد، همان لحظه.
* هر مرحله فقط یک‌بار پاداش دارد (مرجعِ تراکنش یکتاست و مرحله جلو می‌رود).
* پایانِ مهلت ⇒ ربات جوابِ درست را می‌گوید و دور تمام می‌شود.
* پیشرفت برای همیشه در دیسک می‌ماند؛ هر بار از همان مرحله ادامه می‌دهد.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from pathlib import Path

from modules.jadvalestan import storage
from modules.jadvalestan import texts as T

__all__ = [
    "GAME_KEY",
    "QUESTIONS",
    "TOTAL",
    "TIME_LIMIT",
    "normalize",
    "compact",
    "letters_of",
    "question_for_stage",
    "stage_payload",
    "start",
    "answer",
    "timeout",
    "active",
    "current",
    "reset",
    "completed_stage",
]

logger = logging.getLogger("jadvalestan")

GAME_KEY = "jadvalestan"
QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.json"
TIME_LIMIT = T.TIME_LIMIT

#: حداکثر فاصلهٔ زمانیِ یک مرحلهٔ رهاشده تا حافظه بالا نرود.
MAX_IDLE_SECONDS = 6 * 60 * 60


# ---------------------------------------------------------------------------
# بارگذاریِ سوال‌ها
# ---------------------------------------------------------------------------
def _load_questions():
    try:
        raw = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("jadvalestan questions.json is unavailable")
        return []
    items = raw.get("questions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    questions = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question or not answer:
            continue
        questions.append({
            "stage": int(item.get("stage") or index),
            "topic": str(item.get("topic") or ""),
            "difficulty": str(item.get("difficulty") or ""),
            "question": question,
            "answer": answer,
        })
    questions.sort(key=lambda item: item["stage"])
    return questions


QUESTIONS = _load_questions()
TOTAL = len(QUESTIONS)
_BY_STAGE = {item["stage"]: item for item in QUESTIONS}


# ---------------------------------------------------------------------------
# نرمال‌سازی
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
    for ch in _INVISIBLE:
        value = value.replace(ch, "")
    value = _DIACRITICS.sub("", value)
    value = "".join(" " if ch in _PUNCTUATION else ch for ch in value)
    return " ".join(value.split()).lower()


def compact(text) -> str:
    """نسخهٔ بدونِ فاصله — «اسلام آباد» و «اسلام‌آباد» یکی می‌شوند."""
    return normalize(text).replace(" ", "")


def letters_of(answer) -> int:
    """تعدادِ حروفِ جواب — همیشه از روی خودِ جواب، هیچ‌وقت دستی."""
    return len(compact(answer))


# ---------------------------------------------------------------------------
# مرحله‌ها
# ---------------------------------------------------------------------------
def question_for_stage(stage):
    try:
        return _BY_STAGE[int(stage)]
    except (KeyError, TypeError, ValueError):
        return None


def stage_payload(stage):
    """اطلاعاتِ نمایشیِ یک مرحله (بدونِ دست‌زدن به وضعیتِ فعال)."""
    item = question_for_stage(stage)
    if item is None:
        return None
    letters = letters_of(item["answer"])
    return {
        "stage": item["stage"],
        "total": TOTAL,
        "question": item["question"],
        "answer": item["answer"],
        "letters": letters,
        "text": T.stage_text(item["stage"], TOTAL, item["question"], letters),
    }


def completed_stage(chat_id, user_id) -> int:
    return storage.completed_stage(chat_id, user_id)


def next_stage_number(chat_id, user_id) -> int:
    return completed_stage(chat_id, user_id) + 1


# ---------------------------------------------------------------------------
# وضعیتِ لحظه‌ای — به تفکیکِ (گروه، کاربر)
# ---------------------------------------------------------------------------
_ACTIVE = {}


def _key(chat_id, user_id) -> tuple:
    return (storage.chat_key(chat_id), str(user_id))


def current(chat_id, user_id):
    """وضعیتِ مرحلهٔ بازِ همین کاربر، یا ``None``."""
    entry = _ACTIVE.get(_key(chat_id, user_id))
    if entry is None:
        return None
    import time

    if time.time() - float(entry.get("started") or 0) > MAX_IDLE_SECONDS:
        _ACTIVE.pop(_key(chat_id, user_id), None)
        return None
    return entry


def active(chat_id, user_id) -> bool:
    return current(chat_id, user_id) is not None


def owner(chat_id, user_id):
    """آیا این کاربر صاحبِ بازیِ بازِ خودش است؟ (ملاکِ پذیرشِ جواب)"""
    return current(chat_id, user_id) is not None


def reset() -> None:
    """پاک‌کردنِ وضعیتِ لحظه‌ای (تست/ری‌استارت)."""
    _ACTIVE.clear()


def _open_stage(chat_id, user_id):
    """مرحلهٔ جاریِ کاربر را با یک توکنِ تازه باز می‌کند."""
    stage = next_stage_number(chat_id, user_id)
    payload = stage_payload(stage)
    if payload is None:
        return None
    token = uuid.uuid4().hex
    import time

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
    """بازی را (از مرحلهٔ فعلیِ کاربر) شروع می‌کند.

    خروجی:
      * ``{"kind": "finished", "message": …}`` — همهٔ ۳۲۰ مرحله تمام شده.
      * ``{"kind": "started", "stage", "total", "text", "token", …}``
      * ``None`` — فایلِ سوال‌ها در دسترس نیست.
    """
    if not QUESTIONS:
        return None
    done = completed_stage(chat_id, user_id)
    if done >= TOTAL:
        return {"kind": "finished", "message": T.finished_text(TOTAL), "total": TOTAL}
    payload = _open_stage(chat_id, user_id)
    if payload is None:
        return None
    payload["kind"] = "started"
    return payload


# ---------------------------------------------------------------------------
# پاسخِ کاربر
# ---------------------------------------------------------------------------
def _award(chat_id, user_id, stage) -> bool:
    """۳ سکهٔ نقره از سیستمِ اقتصادِ ربات؛ مرجعِ یکتا ⇒ پاداشِ تکراری ممنوع."""
    reference = f"{GAME_KEY}:{storage.chat_key(chat_id)}:{user_id}:{int(stage)}"
    try:
        from economy import award_game

        award_game(chat_id, user_id, GAME_KEY, reference=reference)
        return True
    except Exception as error:  # noqa: BLE001
        logger.warning("jadvalestan award failed stage=%s error=%r", stage, error)
        return False


def answer(chat_id, user_id, text):
    """پاسخِ صاحبِ بازی را بررسی می‌کند.

    ``None`` یعنی «این پیام به بازی مربوط نیست» (کاربرِ دیگر یا مرحلهٔ باز
    ندارد). در غیرِ این صورت ``kind`` یکی از این‌هاست:

      * ``"wrong"``    — غلط بود؛ تا پایانِ مهلت می‌تواند دوباره تلاش کند.
      * ``"correct"``  — پاداش داده شد و مرحلهٔ بعد باز شد.
      * ``"finished"`` — مرحلهٔ ۳۲۰ تمام شد.
    """
    entry = current(chat_id, user_id)
    if entry is None:
        return None
    raw = str(text or "").strip()
    if not raw:
        return None

    stage = int(entry["stage"])
    item = question_for_stage(stage)
    if item is None:
        return None

    if compact(raw) != compact(item["answer"]):
        entry["tries"] = int(entry.get("tries") or 0) + 1
        return {
            "kind": "wrong",
            "stage": stage,
            "tries": entry["tries"],
            "message": T.WRONG_TEXT,
        }

    # ✅ درست: پاداش + پیشرفت + مرحلهٔ بعد (همان لحظه)
    awarded = _award(chat_id, user_id, stage)
    storage.complete_stage(chat_id, user_id, stage)
    _ACTIVE.pop(_key(chat_id, user_id), None)

    reward_line = T.correct_text() if awarded else "آفرین ✅ درست بود 🔖"
    if stage >= TOTAL:
        return {
            "kind": "finished",
            "stage": stage,
            "awarded": awarded,
            "message": f"{reward_line}\n\n{T.finished_text(TOTAL)}",
        }

    following = _open_stage(chat_id, user_id)
    message = f"{reward_line}\n\n{following['text']}"
    return {
        "kind": "correct",
        "stage": stage,
        "awarded": awarded,
        "next_stage": following["stage"],
        "next_token": following["token"],
        "next_text": following["text"],
        "message": message,
    }


def timeout(chat_id, user_id, token):
    """پایانِ مهلت: جوابِ درست اعلام و دور بسته می‌شود.

    اگر توکن عوض شده باشد (کاربر در همین فاصله جوابِ درست داده)، ``None``
    برمی‌گردد تا تایمرِ کهنه هیچ‌کاری نکند.
    """
    entry = current(chat_id, user_id)
    if entry is None:
        return None
    if token is not None and str(entry.get("token")) != str(token):
        return None
    stage = int(entry["stage"])
    item = question_for_stage(stage)
    _ACTIVE.pop(_key(chat_id, user_id), None)
    if item is None:
        return None
    return {
        "kind": "timeout",
        "stage": stage,
        "answer": item["answer"],
        "message": T.timeout_text(item["answer"]),
    }
