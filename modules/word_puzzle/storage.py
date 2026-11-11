"""💾 پیشرفتِ دائمیِ «پازل کلمه» — هر کاربر، هر گروه، برای همیشه.

فایل: ``config/word_puzzle_progress.json`` (مستقل از بقیهٔ فایل‌های ربات):

    {
      "version": 1,
      "progress": {
        "<chat_key>": {
          "<user_id>": {"stage": 3, "updated": 1730000000.0}
        }
      }
    }

``stage`` = آخرین مرحله‌ای که کامل شده؛ مرحلهٔ بعدی = ``stage + 1``.
هیچ‌وقت ریست نمی‌شود (نه با ری‌استارت، نه با گذشتِ زمان).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

FILE_NAME = "word_puzzle_progress.json"

_lock = threading.RLock()
_cache = None
_cache_path = None
_FILE_OVERRIDE = None


def use_file(path) -> None:
    """مسیرِ فایل را عوض می‌کند (فقط تست)."""
    global _FILE_OVERRIDE, _cache, _cache_path
    with _lock:
        _FILE_OVERRIDE = Path(path) if path is not None else None
        _cache = None
        _cache_path = None


def reset_cache() -> None:
    global _cache, _cache_path
    with _lock:
        _cache = None
        _cache_path = None


def reset_all() -> None:
    """پاک‌کردنِ کاملِ پیشرفت — فقط برای تست."""
    global _cache, _cache_path
    with _lock:
        _cache = {"version": 1, "progress": {}}
        _cache_path = None
        try:
            path = path_of_file()
            if path.exists():
                path.unlink()
        except OSError:
            pass


def path_of_file() -> Path:
    if _FILE_OVERRIDE is not None:
        return Path(_FILE_OVERRIDE)
    override = os.environ.get("WORD_PUZZLE_PROGRESS_FILE")
    if override:
        return Path(override)
    return runtime_config_file(FILE_NAME)


def chat_key(chat_id) -> str:
    """کلیدِ یکسانِ گروه (شکلِ کوتاه و کاملِ شناسه یک رکورد نمی‌سازند)."""
    try:
        from modules.group_id import normalize_group_id

        return str(normalize_group_id(int(chat_id)))
    except Exception:  # noqa: BLE001
        return str(chat_id)


def _load():
    global _cache, _cache_path
    with _lock:
        path = path_of_file()
        if _cache is not None and _cache_path == str(path):
            return _cache
        data = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("progress"), dict):
                data = raw
        except (OSError, ValueError):
            data = {}
        if not data:
            data = {"version": 1, "progress": {}}
        _cache = data
        _cache_path = str(path)
        return _cache


def _save(data) -> None:
    global _cache, _cache_path
    path = path_of_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, data)
    except Exception:  # noqa: BLE001 — نبودِ دیسک نباید بازی را بشکند
        pass
    _cache = data
    _cache_path = str(path)


def completed_stage(chat_id, user_id) -> int:
    """آخرین مرحلهٔ کامل‌شدهٔ این کاربر در این گروه (۰ = هنوز شروع نکرده)."""
    data = _load()
    entry = (
        data.get("progress", {})
        .get(chat_key(chat_id), {})
        .get(str(user_id), {})
    )
    try:
        return max(0, int(entry.get("stage") or 0))
    except (TypeError, ValueError):
        return 0


def complete_stage(chat_id, user_id, stage) -> int:
    """ثبتِ کامل‌شدنِ یک مرحله؛ شمارنده فقط جلو می‌رود."""
    with _lock:
        data = _load()
        progress = data.setdefault("progress", {})
        chat = progress.setdefault(chat_key(chat_id), {})
        entry = chat.setdefault(str(user_id), {})
        current = completed_stage(chat_id, user_id)
        try:
            target = int(stage)
        except (TypeError, ValueError):
            return current
        if target <= current:
            return current
        entry["stage"] = target
        entry["updated"] = time.time()
        _save(data)
        return target
