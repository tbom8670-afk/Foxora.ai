"""🗄 روشن/خاموشِ قابلیت «ضد کانال و بیوگرافی» — به تفکیک گروه.

هم‌سبکِ ``modules/ad_spam_guard/storage.py``: خواندن با کش mtime و نوشتن
اتمیک، تا خواندن روی مسیر داغِ پیام ارزان بماند.

دستورها (هر دو شکلِ «ضد کانال» و «ضد کانال روشن» روشن‌کننده‌اند):

    ضد کانال            → روشن
    ضد کانال روشن        → روشن
    ضد کانال خاموش       → خاموش
"""

from __future__ import annotations

import os

from modules.atomic_write import write_json
from modules.profile_guard.matcher import normalize as _fold
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file("profile_guard.json")

ON_COMMANDS = ("ضد کانال", "ضد کانال روشن")
OFF_COMMANDS = ("ضد کانال خاموش",)

_cache: dict | None = None
_cache_mtime: int | None = None


def _file_mtime() -> int | None:
    try:
        return os.stat(FILE).st_mtime_ns
    except OSError:
        return None


def load() -> dict:
    """نقشهٔ chat_id → فعال؟ (با کش mtime)."""
    global _cache, _cache_mtime
    mtime = _file_mtime()
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    if mtime is None:
        _cache = {}
    else:
        try:
            import json
            with open(FILE, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        _cache = {str(k): bool(v) for k, v in raw.items()
                  if isinstance(v, bool)}
    _cache_mtime = mtime
    return _cache


def save(data: dict) -> None:
    global _cache, _cache_mtime
    cleaned = {str(k): bool(v) for k, v in data.items()}
    write_json(FILE, cleaned, indent=2)
    _cache = cleaned
    _cache_mtime = _file_mtime()


def enable(chat_id) -> None:
    data = dict(load())
    data[str(chat_id)] = True
    save(data)


def disable(chat_id) -> None:
    data = dict(load())
    data[str(chat_id)] = False
    save(data)


def is_enabled(chat_id) -> bool:
    return bool(load().get(str(chat_id), False))


def _squash(text) -> str:
    """دستور را برای مقایسه یکدست می‌کند (بدون تغییرِ حروف)."""
    folded = _fold(text)
    return "".join(ch for ch in folded if ch.isalnum())


def toggle_mode(text: str) -> str | None:
    """اگر متن یکی از دستورهای روشن/خاموش باشد، ``on``/``off`` می‌دهد."""
    value = _squash(text)
    if not value:
        return None
    for candidate in OFF_COMMANDS:
        if value == _squash(candidate):
            return "off"
    for candidate in ON_COMMANDS:
        if value == _squash(candidate):
            return "on"
    return None
