import json

from modules.runtime_paths import runtime_config_file
from modules.atomic_write import write_json
import os

from modules.group_id import normalize_group_id

# Per-group switch for GROUP_CUSTOM_WORD_FILTER only.
# It must never gate GLOBAL_FORBIDDEN_WORDS / SpamDetector.check_banned_words.
FILE = runtime_config_file("group_banned_words.json")

_cache = None
_cache_mtime = None

def _file_mtime():
    try:
        return os.stat(FILE).st_mtime_ns
    except OSError:
        return None


def load():
    global _cache, _cache_mtime
    mtime = _file_mtime()
    if _cache is not None and mtime == _cache_mtime:
        return _cache

    if mtime is None:
        _cache = {}
    else:
        try:
            with open(FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}

    _cache_mtime = mtime
    return _cache


def save(data):
    global _cache, _cache_mtime
    write_json(FILE, data, indent=2)
    _cache = data
    _cache_mtime = _file_mtime()


def _leave_manual_change(chat_id):
    """🎒 «مرخصی ادمین»: تغییرِ دستیِ این تنظیم در مدتِ مرخصی ثبت می‌شود تا
    بازیابیِ خودکارِ پایانِ مرخصی آن را از بین نبرد. بدون مرخصیِ فعال: هیچ."""
    try:
        from modules.manage_features import admin_leave
        admin_leave.note_manual_change('banned_words', chat_id)
    except Exception:
        pass


def enable(chat_id):
    data = load()
    data[normalize_group_id(chat_id)] = True
    save(data)
    _leave_manual_change(chat_id)


def disable(chat_id):
    data = load()
    data[normalize_group_id(chat_id)] = False
    save(data)
    _leave_manual_change(chat_id)


def is_enabled(chat_id):
    data = load()
    return data.get(normalize_group_id(chat_id), True)
