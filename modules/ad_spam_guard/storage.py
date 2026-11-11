"""ذخیره‌سازی وضعیت روشن/خاموشِ قابلیت، به تفکیک گروه.

هم‌سبکِ ``modules.group_banned_words_control`` است: خواندن با کش mtime و
نوشتن اتمیک، تا خواندن روی مسیر داغِ پیام ارزان بماند.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file("ad_spam_guard.json")

ON_COMMAND = "تبلیغ گر روشن"
OFF_COMMAND = "تبلیغ گر خاموش"

_cache: dict[str, bool] | None = None
_cache_mtime: int | None = None


def _file_mtime() -> int | None:
    try:
        return os.stat(FILE).st_mtime_ns
    except OSError:
        return None


def load() -> dict[str, bool]:
    """نقشهٔ chat_id → فعال؟ (با کش mtime)."""
    global _cache, _cache_mtime
    mtime = _file_mtime()
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    if mtime is None:
        _cache = {}
    else:
        try:
            with open(FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        _cache = {
            str(k): bool(v)
            for k, v in raw.items()
            if isinstance(v, bool)
        }
    _cache_mtime = mtime
    return _cache


def save(data: dict[str, bool]) -> None:
    global _cache, _cache_mtime
    write_json(FILE, {str(k): bool(v) for k, v in data.items()})
    _cache = {str(k): bool(v) for k, v in data.items()}
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


def is_toggle_command(text: str) -> str | None:
    """اگر متن یکی از دو دستورِ روشن/خاموش باشد، حالت را برمی‌گرداند."""
    cleaned = (text or "").strip()
    if cleaned == ON_COMMAND:
        return "on"
    if cleaned == OFF_COMMAND:
        return "off"
    return None
