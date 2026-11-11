"""وضعیتِ موقتِ تأییدیه + شمارندهٔ اخطار.

دو چیز اینجا نگه داشته می‌شود:

* ``_PENDING`` — چه کسی در چه گروهی درخواستِ روشن‌کردن داده و منتظرِ
  «تایید/لغو» است. در حافظه است (نه دیسک) چون یک تصمیمِ چندثانیه‌ای است و
  با ری‌استارتِ ربات منقضی می‌شود — که رفتارِ درستی است.
* شمارندهٔ اخطار per-(گروه، کاربر) که در قالبِ پیامِ هشدار نشان داده می‌شود.
"""
from __future__ import annotations

import time
from collections import OrderedDict

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

# ---------------------------------------------------------------- تأییدیه
_PENDING: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_PENDING_TTL = 300.0  # ۵ دقیقه
_PENDING_MAX = 512


def request_confirm(chat_id, user_id) -> None:
    """درخواستِ روشن‌کردن را ثبت می‌کند (منتظرِ تایید)."""
    key = str(chat_id)
    _PENDING[key] = (str(user_id), time.monotonic())
    _PENDING.move_to_end(key)
    while len(_PENDING) > _PENDING_MAX:
        _PENDING.popitem(last=False)


def pending_user(chat_id):
    """کاربری که منتظرِ تایید است (اگر منقضی نشده باشد)."""
    entry = _PENDING.get(str(chat_id))
    if entry is None:
        return None
    user_id, stamp = entry
    if time.monotonic() - stamp > _PENDING_TTL:
        _PENDING.pop(str(chat_id), None)
        return None
    return user_id


def clear_confirm(chat_id) -> None:
    _PENDING.pop(str(chat_id), None)


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
        import json

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
