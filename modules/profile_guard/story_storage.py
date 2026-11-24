"""Independent per-group anti-story switch (off by default)."""
from __future__ import annotations

import json
import os

from modules.atomic_write import write_json
from modules.profile_guard.matcher import normalize
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file('story_guard.json')
_cache = None
_cache_mtime = None


def _mtime():
    try:
        return os.stat(FILE).st_mtime_ns
    except OSError:
        return None


def load():
    global _cache, _cache_mtime
    stamp = _mtime()
    if _cache is not None and stamp == _cache_mtime:
        return _cache
    try:
        with open(FILE, encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _cache = {str(key): value for key, value in data.items() if isinstance(value, bool)}
    _cache_mtime = stamp
    return _cache


def _set(chat_id, value):
    global _cache, _cache_mtime
    data = dict(load())
    data[str(chat_id)] = bool(value)
    write_json(FILE, data, indent=2)
    _cache, _cache_mtime = data, _mtime()


def enable(chat_id):
    _set(chat_id, True)


def disable(chat_id):
    _set(chat_id, False)


def is_enabled(chat_id):
    return bool(load().get(str(chat_id), False))


def toggle_mode(text):
    value = ''.join(ch for ch in normalize(text) if ch.isalnum())
    if value == 'ضداستوریروشن':
        return 'on'
    if value == 'ضداستوریخاموش':
        return 'off'
    return None
