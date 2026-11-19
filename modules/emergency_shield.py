"""🌡 حالت بحرانی «سپر محافظتی» — فعال‌سازی موقت و بازگشت خودکار.

این ماژول یک «سپر» گروهی است: وقتی گروه در حالِ عضوگیری/هجومِ اسپمر است،
مالک یا ادمینِ ثبتشده با ``سپر روشن 20`` آن را برای ۲۰ دقیقه روشن می‌کند.
سپر:

  * **سه قابلیتِ پایه را روشن می‌کند:** کلمات ممنوعه، ضد کانال (بیوگرافی)،
    تبلیغ‌گر (ضد تبلیغ هوش مصنوعی) — با سوییچ‌هایِ موجودِ خودِ ربات.
  * **یک لایهٔ اختصاصی فقط در طولِ سپر فعال می‌کند:** تشخیص لینک در بیوگرافی،
    حساسیتِ بالاترِ پیام‌های مرزی، قفلِ ارسالِ گیف و رسانه، و ضد تکرارِ سریع
    (۳ پیام یکسان در ۱۰ ثانیه).
  * **مجازات را سکوت دائم می‌کند** (``punishment_mode``).
  * **آستانهٔ اخطار را به ۱** می‌رساند (بیشترین حساسیت).

دو شکلِ دستور پذیرفته می‌شود: ``سپر روشن 20 دقیقه`` یک‌جا، یا ``سپر روشن``
(بدونِ عدد) و بعد فقط ``20 دقیقه`` — در شکلِ دوم ربات أولاً پیامِ «تایم را
تعیین کنید» را می‌فرستد و همان پیامِ بعدیِ همان کاربر در همان گروه به‌عنوانِ
زمان شناخته می‌شود.

قبل از روشن‌شدن، وضعیتِ همهٔ این تنظیمات در همان گروه **snapshot** می‌شود و
پس از پایانِ تایمر یا دستورِ ``سپر خاموش`` **دقیقاً** همان وضعیت برمی‌گردد
(هر قابلیتی که خاموش بوده خاموش، هر کدام که روشن بوده روشن).

ذخیره‌سازی: ``config/emergency_shield.json`` — اتمیک و با کشِ mtime، مثلِ
بقیهٔ storageهای پروژه. تایمر با یک ناظرِ ۳۰ ثانیه‌ای پایدار است: اگر ربات
وسطِ سپر ری‌استارت شود، ناظر بعد از اتمامِ زمانِ باقی‌مانده تنظیمات را
برمی‌گرداند.

هیچ متنِ شخصی، شناسهٔ نشست یا محتوای پیام لاگ نمی‌شود.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Optional

from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file

try:  # pragma: no cover - در محیطِ تست کتابخانه نیست
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


FILE = runtime_config_file("emergency_shield.json")

#: پیش‌فرضِ تایمر و بازهٔ مجاز (دقیقه).
DEFAULT_MINUTES = 20
MIN_MINUTES = 1
MAX_MINUTES = 1440

#: هر چند ثانیه ناظرِ پایانِ تایمر اجرا می‌شود.
POLL_SECONDS = 30.0

#: لایهٔ اختصاصیِ سپر — ضد تکرارِ سریع (حالتِ عادی: ۳ پیام در ۳۰ ثانیه).
SHIELD_REPEAT_WINDOW = 10.0
SHIELD_REPEAT_LIMIT = 3
#: آستانهٔ اخطار در حالت سپر (بیشترین حساسیت).
SHIELD_WARNING_THRESHOLD = 1

#: پارامترهایِ «پیام مرزی» — حساسیتِ بالاتر فقط در طولِ سپر.
_BORDERLINE_PATTERNS = (
    # آیدی کوتاه‌تر از حدِ عادی (۲ نویسه به بالا) — مثلاً @ab
    re.compile(r"(?<![A-Za-z0-9_@])@[A-Za-z0-9_]{2,32}(?![A-Za-z0-9_])"),
    # دامنهٔ بدونِ پیشوندِ http (همان الگویِ لینک، ولی با TLDهایِ بیشتر)
    re.compile(
        r"\b(?:[a-z0-9-]+\.)+"
        r"(?:com|ir|net|org|info|me|co|io|app|xyz|shop|site|online|club|"
        r"tk|ml|ga|cf|gq|blog|live|cloud|dev|ai|buzz|vip)\b",
        re.IGNORECASE,
    ),
    # شمارهٔ موبایل با/بدونِ کدِ کشور و با فاصلهِ اختیاری
    re.compile(r"(?:\+?98|0)?9\d{2}[\s-]?\d{3}[\s-]?\d{4}\b"),
)

#: اعدادِ فارسی/عربی → لاتین (برایِ خواندنِ عددِ دستور).
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

ON_COMMAND = "سپر روشن"
OFF_COMMAND = "سپر خاموش"

# ---------------------------------------------------------------------------
# قالب‌هایِ پیام — عیناً مطابقِ خواستهٔ کارفرما (بدونِ هیچ تغییری)
# ---------------------------------------------------------------------------

TITLE_ON = "⪻ «حالت بحرانی » سپر روشن شد ✅"
TITLE_ASK = "⪻ «حالت بحرانی » سپر محافظتی"
ASK_LINE = "تایم فعال سازی سپر رو تایین کنید مثال دستوری"
ASK_EXAMPLES = ("20 دقیقه", "1 ساعت")
STATUS_LINES = (
    "کلمات ممنوعه روشن شد",
    "ضد کانال بیوگرافی روشن شد",
    "ضد تبلیغ هوش مصنوعی روشن شد",
    "تشخیص سریع هرزنامه روشن شد",
    "حالت به سکوت دائم تغییر کرد",
)
OFF_TEXT = (
    "⚙️ : سپر محافظتی خاموش شد تنظیمات مدیریتی به قبل بازگشت "
    "حالت مدیریت عادی"
)

_cache = None
_cache_mtime = None
_tasks = {}


# ---------------------------------------------------------------------------
# ابزارهایِ کوچک
# ---------------------------------------------------------------------------

def _u16(text) -> int:
    return len(str(text).encode("utf-16-le")) // 2


def _log(bot, message: str, *, error: bool = False):
    logger = getattr(bot, "logger", None)
    if logger is None:
        return
    try:
        if error:
            logger.log_error(message)
        else:
            logger.log_info(message)
    except Exception:
        pass


def _key(chat_id) -> str:
    return str(normalize_group_id(chat_id))


def normalize_text(text: str) -> str:
    """یکدست‌سازیِ دستور: اعدادِ فارسی، نیم‌فاصله، ی/کِ عربی و نشانه‌ها."""
    value = str(text or "").translate(_DIGITS)
    value = value.replace("‌", " ").replace("‏", "").replace("‎", "")
    for src, dst in (("ي", "ی"), ("ك", "ک"), ("أ", "ا"), ("إ", "ا"), ("آ", "ا")):
        value = value.replace(src, dst)
    for ch in "▪▫●○◆◇★☆✅❌.،!؛:«»":
        value = value.replace(ch, " ")
    return " ".join(value.split())


def format_duration(minutes) -> str:
    """«۱ ساعت» / «۲۰ دقیقه» / «۱ ساعت و ۳۰ دقیقه» — با ارقامِ لاتین."""
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        total = DEFAULT_MINUTES
    hours, mins = divmod(max(0, total), 60)
    if hours and mins:
        return f"{hours} ساعت و {mins} دقیقه"
    if hours:
        return f"{hours} ساعت"
    return f"{mins} دقیقه"


def _display_actor(actor) -> str:
    """نامِ تنظیم‌کننده: @یوزرنیم در اولویت، بعد نامِ نمایشی."""
    if actor is None:
        return "ناشناس"
    username = getattr(actor, "username", None)
    if username:
        return "@" + str(username).lstrip("@")
    first = (getattr(actor, "first_name", None) or "").strip()
    last = (getattr(actor, "last_name", None) or "").strip()
    full = (first + " " + last).strip()
    return full or "ناشناس"


# ---------------------------------------------------------------------------
# ساختِ پیام‌ها (متن + entityهایِ واقعیِ سروش)
# ---------------------------------------------------------------------------

def _emit(parts, entities, text, kind):
    start = _u16("".join(parts))
    parts.append(text)
    if "bold" in kind:
        entities.append(MessageEntityBold(offset=start, length=_u16(text)))
    if "quote" in kind:
        entities.append(MessageEntityBlockquote(offset=start, length=_u16(text)))


def build_on_message(*, duration: str, actor: str, chat_id) -> tuple:
    """قالبِ ۱ — پیامِ روشن‌شدنِ سپر."""
    parts = []
    entities = []
    _emit(parts, entities, TITLE_ON, "bold")
    parts.append("\n")
    for status in STATUS_LINES:
        parts.append("\n")
        _emit(parts, entities, "〶 " + status, "bold_quote")
    parts.append("\n")
    for label, value in (("مدت زمان فعال بودن", duration),
                         ("تنظیم کننده", actor), ("شناسه گروه", chat_id)):
        parts.append("\n")
        _emit(parts, entities, f"⚙️ {label} :", "bold")
        parts.append(f" {value}")
    return "".join(parts), entities


def build_ask_message() -> tuple:
    """قالبِ ۲ — وقتی دستور بدونِ تایم زده شود."""
    parts = []
    entities = []
    _emit(parts, entities, TITLE_ASK, "bold")
    parts.append("\n")
    _emit(parts, entities, ASK_LINE, "bold")
    for example in ASK_EXAMPLES:
        parts.append("\n")
        _emit(parts, entities, "⏱ " + example, "quote")
    return "".join(parts), entities


def build_off_message() -> tuple:
    """قالبِ ۳ — خاموش‌شدنِ سپر."""
    return OFF_TEXT, [MessageEntityBold(offset=0, length=_u16(OFF_TEXT))]


# ---------------------------------------------------------------------------
# ذخیره‌سازی پایدار
# ---------------------------------------------------------------------------

def _empty():
    return {"version": 1, "groups": {}}


def _load():
    global _cache, _cache_mtime
    try:
        mtime = FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    data = None
    if mtime is not None:
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if not isinstance(data, dict):
        data = _empty()
    if not isinstance(data.get("groups"), dict):
        data["groups"] = {}
    _cache = data
    _cache_mtime = mtime
    return _cache


def _save():
    global _cache_mtime
    try:
        write_json(FILE, _cache if _cache is not None else _empty())
    except Exception:
        return
    try:
        _cache_mtime = FILE.stat().st_mtime_ns
    except OSError:
        _cache_mtime = None


def _store(chat_id, record):
    data = _load()
    key = _key(chat_id)
    if record is None:
        data["groups"].pop(key, None)
    else:
        data["groups"][key] = record
    _save()


def reset_cache():
    """پاک‌کردنِ کشِ حافظه (برایِ تست و پس‌از بازیابیِ فایل)."""
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = None


def get_record(chat_id) -> Optional[dict]:
    record = _load()["groups"].get(_key(chat_id))
    return record if isinstance(record, dict) else None


def is_active(chat_id) -> bool:
    record = get_record(chat_id)
    if not isinstance(record, dict) or not record.get("active"):
        return False
    try:
        return time.time() < float(record.get("until") or 0)
    except (TypeError, ValueError):
        return False


def remaining_seconds(chat_id) -> float:
    record = get_record(chat_id)
    if not isinstance(record, dict):
        return 0.0
    try:
        return max(0.0, float(record.get("until") or 0) - time.time())
    except (TypeError, ValueError):
        return 0.0


def status(chat_id) -> dict:
    record = get_record(chat_id)
    active = is_active(chat_id)
    return {
        "active": active,
        "remaining_seconds": remaining_seconds(chat_id) if active else 0.0,
        "duration_minutes": (record or {}).get("duration_minutes"),
        "record": record,
    }


# ---------------------------------------------------------------------------
# snapshot / اعمال / بازگشت
# ---------------------------------------------------------------------------

def _snapshot(chat_id) -> dict:
    """وضعیتِ فعلیِ همهٔ تنظیماتِ قابلِ‌بازگشت در این گروه."""
    from modules import (
        ad_spam_guard, bio_link_storage, group_banned_words_control,
        punishment_mode, warning_threshold,
    )
    from modules.profile_guard import storage as profile_guard_storage

    return {
        "banned_words": bool(group_banned_words_control.is_enabled(chat_id)),
        "anti_channel": bool(profile_guard_storage.is_enabled(chat_id)),
        "ad_guard": bool(ad_spam_guard.storage.is_enabled(chat_id)),
        "bio_link": bool(bio_link_storage.is_enabled(chat_id)),
        "warning_threshold": int(warning_threshold.get_threshold(chat_id)),
        "punishment_mode": str(punishment_mode.get_mode(chat_id)),
        "media": None,
    }


def _apply_toggles(chat_id) -> None:
    """روشن‌کردنِ سه قابلیتِ پایه + بیو-لینک + حساسیت + مجازات."""
    from modules import (
        ad_spam_guard, bio_link_storage, group_banned_words_control,
        punishment_mode, warning_threshold,
    )
    from modules.profile_guard import storage as profile_guard_storage

    group_banned_words_control.enable(chat_id)
    profile_guard_storage.enable(chat_id)
    ad_spam_guard.storage.enable(chat_id)
    bio_link_storage.set_enabled(chat_id, True)
    try:
        warning_threshold.set_threshold(chat_id, SHIELD_WARNING_THRESHOLD)
    except Exception:
        pass
    punishment_mode.set_mode(chat_id, punishment_mode.MODE_MUTE)


def _restore_toggles(chat_id, snapshot: dict) -> None:
    """بازگرداندنِ دقیقِ وضعیتِ ذخیره‌شده."""
    from modules import (
        ad_spam_guard, bio_link_storage, group_banned_words_control,
        punishment_mode, warning_threshold,
    )
    from modules.profile_guard import storage as profile_guard_storage

    if snapshot.get("banned_words"):
        group_banned_words_control.enable(chat_id)
    else:
        group_banned_words_control.disable(chat_id)

    if snapshot.get("anti_channel"):
        profile_guard_storage.enable(chat_id)
    else:
        profile_guard_storage.disable(chat_id)

    if snapshot.get("ad_guard"):
        ad_spam_guard.storage.enable(chat_id)
    else:
        ad_spam_guard.storage.disable(chat_id)

    if snapshot.get("bio_link"):
        bio_link_storage.set_enabled(chat_id, True)
    else:
        bio_link_storage.set_enabled(chat_id, False)

    try:
        threshold = int(snapshot.get("warning_threshold"))
    except (TypeError, ValueError):
        threshold = None
    if threshold:
        try:
            warning_threshold.set_threshold(chat_id, threshold)
        except Exception:
            pass

    mode = snapshot.get("punishment_mode")
    if mode in (punishment_mode.MODE_BAN, punishment_mode.MODE_MUTE):
        punishment_mode.set_mode(chat_id, mode)


async def _lock_media(bot, chat_id):
    actions = getattr(bot, "group_actions", None)
    if actions is None or not hasattr(actions, "lock_media"):
        return None
    try:
        return await actions.lock_media(chat_id)
    except Exception as error:
        _log(bot, f"SHIELD MEDIA LOCK FAILED chat_id={chat_id} "
                  f"error={error!r}", error=True)
        return None


async def _unlock_media(bot, chat_id, snapshot):
    actions = getattr(bot, "group_actions", None)
    if actions is None or not hasattr(actions, "unlock_media"):
        return False
    try:
        return bool(await actions.unlock_media(chat_id, snapshot))
    except Exception as error:
        _log(bot, f"SHIELD MEDIA RESTORE FAILED chat_id={chat_id} "
                  f"error={error!r}", error=True)
        return False


async def activate(bot, chat_id, minutes: int, *, actor=None) -> dict:
    """روشن‌کردنِ سپر برایِ ``minutes`` دقیقه؛ snapshot قبل از هر تغییری."""
    minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(minutes)))
    existing = get_record(chat_id)
    if isinstance(existing, dict) and existing.get("active"):
        # تمدیدِ تایمر — snapshotِ اولیه دست‌نخورده می‌ماند.
        existing["until"] = time.time() + minutes * 60
        existing["duration_minutes"] = minutes
        _store(chat_id, existing)
        _log(bot, f"SHIELD EXTENDED chat_id={chat_id} minutes={minutes}")
        return existing

    record = {
        "active": True,
        "until": time.time() + minutes * 60,
        "duration_minutes": minutes,
        "started_at": time.time(),
        "chat_id": str(chat_id),
        "actor": _display_actor(actor),
        "snapshot": _snapshot(chat_id),
    }
    _apply_toggles(chat_id)
    media_snapshot = await _lock_media(bot, chat_id)
    if media_snapshot is not None:
        record["snapshot"]["media"] = media_snapshot
        record["media_applied"] = True
    else:
        record["media_applied"] = False
    _store(chat_id, record)
    _log(bot, f"SHIELD ON chat_id={chat_id} minutes={minutes} "
              f"media={record['media_applied']}")
    return record


async def restore(bot, chat_id, *, reason: str = "timer") -> bool:
    """بازگرداندنِ تنظیمات به وضعیتِ قبل از روشن‌شدن."""
    record = get_record(chat_id)
    if not isinstance(record, dict):
        return False
    snapshot = record.get("snapshot") or {}
    _restore_toggles(chat_id, snapshot)
    if record.get("media_applied") and isinstance(snapshot.get("media"), dict):
        await _unlock_media(bot, chat_id, snapshot["media"])
    _store(chat_id, None)
    _log(bot, f"SHIELD OFF chat_id={chat_id} reason={reason}")
    return True


# ---------------------------------------------------------------------------
# لایهٔ اختصاصیِ سپر — پیامِ مرزی و ضد تکرارِ سریع
# ---------------------------------------------------------------------------

def is_borderline(text) -> bool:
    """آیا این پیام «مرزی» است؟ (فقط در طولِ سپر به‌عنوانِ اسپم حساب می‌شود)"""
    value = str(text or "")
    if not value.strip():
        return False
    return any(pattern.search(value) for pattern in _BORDERLINE_PATTERNS)


def repeat_window(chat_id) -> float:
    """پنجرهٔ ضدتکرار در این گروه."""
    return SHIELD_REPEAT_WINDOW if is_active(chat_id) else 30.0


def repeat_limit(chat_id) -> int:
    return SHIELD_REPEAT_LIMIT if is_active(chat_id) else 3


# ---------------------------------------------------------------------------
# ناظرِ پایانِ تایمر
# ---------------------------------------------------------------------------

async def _notify_off(bot, chat_id):
    """اعلانِ خاموش‌شدن به گروه — بهترینِ تلاش، بدونِ بلاکِ ناظر."""
    text, entities = build_off_message()
    client = getattr(bot, "client", None)
    if client is None:
        return
    try:
        await client.send_message(chat_id, text, formatting_entities=entities)
    except Exception as error:
        _log(bot, f"SHIELD OFF NOTICE FAILED chat_id={chat_id} "
                  f"error={error!r}", error=True)


async def watcher(bot, logger=None) -> None:
    """هر ۳۰ ثانیه سپرهای منقضی‌شده را می‌بندد و تنظیمات را برمی‌گرداند."""
    while True:
        try:
            now = time.time()
            for key, record in list(_load()["groups"].items()):
                if not isinstance(record, dict) or not record.get("active"):
                    continue
                try:
                    until = float(record.get("until") or 0)
                except (TypeError, ValueError):
                    until = 0.0
                if now < until:
                    continue
                chat_id = record.get("chat_id") or key
                await restore(bot, chat_id, reason="timer")
                await _notify_off(bot, chat_id)
        except Exception as error:
            _log(bot, f"SHIELD WATCHER ERROR error={error!r}", error=True)
        await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# دستورها
# ---------------------------------------------------------------------------

#: پاسخِ مرحله‌ایِ «تایم» — بعد از «سپر روشن» بدونِ عدد، کاربر فقط زمان را
#: می‌فرستد («20 دقیقه»). این نگه‌داری کوتاهِ درخواست است تا همان پیامِ بعدی
#: به‌عنوانِ تایمِ همان کاربر در همان گروه شناخته شود.
_PENDING_TTL = 120
_PENDING_MAX = 2000
_pending = {}

_TIME_RE = re.compile(r"(\d{1,4})\s*(ساعت|دقیقه)?")


def parse_time_value(text: str) -> Optional[int]:
    """فقط زمان — ``20`` ، ``20 دقیقه`` ، ``1 ساعت`` ؛ ``None`` اگر زمان نبود."""
    value = normalize_text(text)
    if not value:
        return None
    match = _TIME_RE.fullmatch(value)
    if not match:
        return None
    number = int(match.group(1))
    if match.group(2) == "ساعت":
        number *= 60
    if not (MIN_MINUTES <= number <= MAX_MINUTES):
        return None
    return number


def parse_minutes(text: str) -> Optional[int]:
    """عددِ دقیقه از دستور — ``20`` ، ``20 دقیقه`` ، ``1 ساعت`` ؛ ``None`` اگر نبود."""
    value = normalize_text(text)
    if not value.startswith(ON_COMMAND):
        return None
    rest = value[len(ON_COMMAND):].strip()
    if not rest:
        return None
    return parse_time_value(rest)


def match_command(text: str) -> Optional[str]:
    """``on`` / ``off`` / ``None``."""
    value = normalize_text(text)
    if not value:
        return None
    if value == OFF_COMMAND:
        return "off"
    if value == ON_COMMAND:
        return "ask"
    if value.startswith(ON_COMMAND):
        if parse_minutes(value) is not None:
            return "on"
    return None


def is_command(text: str) -> bool:
    return match_command(text) is not None


# --- درخواستِ مرحله‌ایِ تایم -------------------------------------------------

def _pending_key(chat_id, user_id) -> tuple:
    return (_key(chat_id), str(user_id))


def set_pending(chat_id, user_id) -> None:
    """ثبتِ اینکه این کاربر در این گروه «سپر روشن» را بدونِ زمان زده است."""
    if user_id is None:
        return
    if len(_pending) >= _PENDING_MAX:
        now = time.time()
        for key in [k for k, ts in _pending.items() if now - ts > _PENDING_TTL]:
            _pending.pop(key, None)
    _pending[_pending_key(chat_id, user_id)] = time.time()


def has_pending(chat_id, user_id) -> bool:
    key = _pending_key(chat_id, user_id)
    stamp = _pending.get(key)
    if stamp is None:
        return False
    if time.time() - stamp > _PENDING_TTL:
        _pending.pop(key, None)
        return False
    return True


def take_pending(chat_id, user_id) -> bool:
    """درخواستِ تایم را می‌گیرد و یک بار مصرف می‌کند."""
    if not has_pending(chat_id, user_id):
        return False
    _pending.pop(_pending_key(chat_id, user_id), None)
    return True


def clear_pending(chat_id, user_id) -> None:
    _pending.pop(_pending_key(chat_id, user_id), None)


def is_time_reply(text: str) -> bool:
    return parse_time_value(text) is not None


def is_shield_message(chat_id, user_id, text: str) -> bool:
    """آیا این پیام به مسیرِ سپر برده شود؟ (دستور، یا زمانِ پاسخ به درخواست)."""
    if match_command(text):
        return True
    if user_id is None:
        return False
    return has_pending(chat_id, user_id) and is_time_reply(text)


async def _activate_and_reply(bot, event, chat_id, minutes: int, actor) -> None:
    await activate(bot, chat_id, minutes, actor=actor)
    record = get_record(chat_id) or {}
    body, entities = build_on_message(
        duration=format_duration(record.get("duration_minutes") or minutes),
        actor=record.get("actor") or _display_actor(actor),
        chat_id=chat_id,
    )
    await event.reply(body, formatting_entities=entities)


async def run_command(bot, event, chat_id, text: str, *, actor=None) -> bool:
    """اجرایِ دستور؛ خروجی ``True`` اگر متن دستورِ این قابلیت باشد."""
    user_id = getattr(actor, "id", None)
    action = match_command(text)
    if action == "ask":
        set_pending(chat_id, user_id)
        body, entities = build_ask_message()
        await event.reply(body, formatting_entities=entities)
        return True
    if action == "on":
        clear_pending(chat_id, user_id)
        await _activate_and_reply(
            bot, event, chat_id, parse_minutes(text) or DEFAULT_MINUTES, actor,
        )
        return True
    if action == "off":
        clear_pending(chat_id, user_id)
        await restore(bot, chat_id, reason="command")
        body, entities = build_off_message()
        await event.reply(body, formatting_entities=entities)
        return True
    if action is None and user_id is not None and is_time_reply(text):
        # پاسخِ مرحله‌ای: کاربر بعد از «سپر روشن» فقط زمان را فرستاده است.
        if take_pending(chat_id, user_id):
            await _activate_and_reply(
                bot, event, chat_id, parse_time_value(text), actor,
            )
            return True
    return False


def reset_runtime_state():
    """پاک‌کردنِ state درون‌حافظه — فقط برایِ تست."""
    _tasks.clear()
    _pending.clear()
    reset_cache()
