"""وضعیتِ موقتِ تأییدیه + شمارندهٔ اخطار.

دو چیز اینجا نگه داشته می‌شود:

* ``pending`` — چه کسی در چه گروهی درخواستِ روشن‌کردن داده و منتظرِ
  «تایید/لغو» است، به‌همراه شناسهٔ همان کارتِ تأییدیه.
* شمارندهٔ اخطار per-(گروه، کاربر) که در قالبِ پیامِ هشدار نشان داده می‌شود.

⚠️ چرا روی دیسک هم نوشته می‌شود؟

قبلاً فقط در حافظه بود؛ بعد از هر ری‌استارت (یا اخراج/بروزرسانیِ ربات) کارت
روی صفحه می‌ماند ولی «تایید» کاربر بی‌اثر می‌شد و همان پیام به مسیرِ
«گزارش» می‌افتاد («گزارش‌دهنده نمی‌تواند تصمیم بگیرد»). حالا وضعیت در یک
فایلِ کوچکِ اتمیک ذخیره می‌شود تا «تایید» بعد از ری‌استارت هم کار کند.
TTL کوتاه است (۱۵ دقیقه) چون این یک تصمیمِ چندثانیه‌ای است.
"""
from __future__ import annotations

import json
import time

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

# ---------------------------------------------------------------- تأییدیه
_PENDING_TTL = 900.0  # ۱۵ دقیقه
_PENDING_MAX = 512

_PENDING_FILE = runtime_config_file("ad_spam_guard_pending.json")

#: chat_id → {"user": str, "ts": float, "card": int | None}
_PENDING: "dict[str, dict]" | None = None
_PENDING_MTIME: int | None = None


def _file_mtime():
    import os

    try:
        return os.stat(_PENDING_FILE).st_mtime_ns
    except OSError:
        return None


def _load() -> dict:
    """نقشهٔ chat_id → رکورد، با کشِ mtime (نوشتنِ بیرونی هم دیده می‌شود)."""
    global _PENDING, _PENDING_MTIME
    mtime = _file_mtime()
    if _PENDING is not None and mtime == _PENDING_MTIME:
        return _PENDING
    data: dict[str, dict] = {}
    if mtime is not None:
        try:
            with open(_PENDING_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        if isinstance(raw, dict):
            for key, record in raw.items():
                if not isinstance(record, dict):
                    continue
                try:
                    stamp = float(record.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                data[str(key)] = {
                    "user": str(record.get("user") or ""),
                    "ts": stamp,
                    "card": record.get("card"),
                }
    _PENDING = data
    _PENDING_MTIME = mtime
    return _PENDING


def _save(data: dict) -> None:
    global _PENDING, _PENDING_MTIME
    _PENDING = data
    try:
        write_json(_PENDING_FILE, data)
    except Exception:
        # نوشتنِ ناموفق نباید قابلیت را از کار بیندازد؛ حافظه معتبر است.
        pass
    _PENDING_MTIME = _file_mtime()


def request_confirm(chat_id, user_id, card_message_id=None) -> None:
    """درخواستِ روشن‌کردن را ثبت می‌کند (منتظرِ تایید)."""
    data = dict(_load())
    data[str(chat_id)] = {
        "user": str(user_id),
        "ts": time.time(),
        "card": card_message_id,
    }
    # پاک‌سازیِ رکوردهای منقضی/اضافه
    now = time.time()
    data = {
        key: record for key, record in data.items()
        if now - float(record.get("ts") or 0) <= _PENDING_TTL
    }
    while len(data) > _PENDING_MAX:
        oldest = min(data, key=lambda k: float(data[k].get("ts") or 0))
        data.pop(oldest, None)
    _save(data)


def set_confirm_card(chat_id, card_message_id) -> None:
    """شناسهٔ پیامِ کارت را ثبت می‌کند (برای «تایید با ریپلای»)."""
    data = dict(_load())
    record = data.get(str(chat_id))
    if record is None:
        return
    record = dict(record)
    record["card"] = card_message_id
    data[str(chat_id)] = record
    _save(data)


def _record(chat_id):
    record = _load().get(str(chat_id))
    if record is None:
        return None
    try:
        stamp = float(record.get("ts") or 0)
    except (TypeError, ValueError):
        stamp = 0.0
    if time.time() - stamp > _PENDING_TTL:
        clear_confirm(chat_id)
        return None
    return record


def has_pending(chat_id) -> bool:
    """آیا در این گروه کارتی منتظرِ «تایید/لغو» است؟ (ارزان، بدونِ RPC)"""
    return _record(chat_id) is not None


def pending_user(chat_id):
    """کاربری که درخواستِ روشن‌کردن داده (اگر منقضی نشده باشد)."""
    record = _record(chat_id)
    return None if record is None else record.get("user")


def pending_card(chat_id):
    """شناسهٔ پیامِ کارتِ منتظرِ تصمیم (یا ``None``)."""
    record = _record(chat_id)
    return None if record is None else record.get("card")


def is_pending_card(chat_id, message_id) -> bool:
    """آیا این شناسهٔ پیام، همان کارتِ تأییدیهٔ در جریان است؟"""
    card = pending_card(chat_id)
    if card is None or message_id is None:
        return False
    return str(card) == str(message_id)


def clear_confirm(chat_id) -> None:
    data = dict(_load())
    if str(chat_id) in data:
        data.pop(str(chat_id), None)
        _save(data)


# ---------------------------------------------------------------- اخطار
_WARN_FILE = runtime_config_file("ad_spam_guard_warns.json")
_warn_cache: dict[str, int] | None = None
_warn_mtime: int | None = None


def _mtime():
    import os

    try:
        return os.stat(_WARN_FILE).st_mtime_ns
    except OSError:
        return None


def _load_warns() -> dict[str, int]:
    global _warn_cache, _warn_mtime
    mtime = _mtime()
    if _warn_cache is not None and mtime == _warn_mtime:
        return _warn_cache
    if mtime is None:
        _warn_cache = {}
    else:
        try:
            with open(_WARN_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        _warn_cache = {
            str(k): int(v) for k, v in raw.items()
            if isinstance(v, (int, float)) and int(v) > 0
        }
    _warn_mtime = mtime
    return _warn_cache


def warn_count(chat_id, user_id) -> int:
    return int(_load_warns().get(_key(chat_id, user_id), 0))


def add_warn(chat_id, user_id) -> int:
    """یک اخطار اضافه می‌کند و تعدادِ جدید را برمی‌گرداند."""
    data = dict(_load_warns())
    key = _key(chat_id, user_id)
    data[key] = int(data.get(key, 0)) + 1
    write_json(_WARN_FILE, data)
    global _warn_cache, _warn_mtime
    _warn_cache = data
    _warn_mtime = _mtime()
    return int(data[key])


def reset_warns(chat_id, user_id) -> None:
    data = dict(_load_warns())
    if _key(chat_id, user_id) in data:
        data.pop(_key(chat_id, user_id), None)
        write_json(_WARN_FILE, data)
        global _warn_cache, _warn_mtime
        _warn_cache = data
        _warn_mtime = _mtime()


def _key(chat_id, user_id) -> str:
    return f"{chat_id}:{user_id}"
