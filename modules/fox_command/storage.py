# -*- coding: utf-8 -*-
"""🦊 ذخیره‌سازی پایدار بازی «مرکز فرماندهی روباه».

همه‌چیز داخل خودِ پوشهٔ پروژه و با نوشتن اتمیک ذخیره می‌شود تا با ری‌استارت
ربات هیچ مسابقه‌ای گم نشود::

    <project>/data/fox_command/
        matches/<match_id>.json   وضعیت کامل هر مسابقه (شناسهٔ یکتا)
        archive/<match_id>.json   آرشیو مسابقه‌های تمام‌شده (برای گزارش/تاریخچه)
        index.json                نگاشت chat_id → match_id مسابقه‌های باز
        config/settings.json      (اختیاری) تنظیمات اختصاصی

قواعد:

* هر مسابقه یک فایل ⇒ دو گروه هرگز روی یک فایل نمی‌نویسند.
* نوشتن اتمیک (``modules.atomic_write``) ⇒ فایل نیمه‌کاره/خراب نمی‌ماند.
* قفل درون‌فرایندی ``LIVE_LOCK`` برای عملیات‌های خرید/شلیک/دفاع که باید
  اتمیک باشند.
* شناسهٔ مسابقه زمان + تصادف امن است و وجود فایل را هم چک می‌کند.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from modules.atomic_write import write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = PROJECT_ROOT / "data" / "fox_command"

_OVERRIDE_ENV = "FOX_COMMAND_DATA_DIR"
_override_base = None

LIVE_LOCK = threading.RLock()


def base_dir() -> Path:
    if _override_base is not None:
        return Path(_override_base)
    env = os.getenv(_OVERRIDE_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_BASE


def set_base_dir(path=None):
    """override صریح مسیر (تست/استقرار). ``None`` یعنی پیش‌فرض پروژه."""
    global _override_base
    _override_base = Path(path).expanduser().resolve() if path else None
    return base_dir()


def matches_dir() -> Path:
    return base_dir() / "matches"


def archive_dir() -> Path:
    return base_dir() / "archive"


def index_path() -> Path:
    return base_dir() / "index.json"


def ensure_dirs():
    matches_dir().mkdir(parents=True, exist_ok=True)
    archive_dir().mkdir(parents=True, exist_ok=True)
    return base_dir()


# ---------------------------------------------------------------------------
# شناسه و مسیر
# ---------------------------------------------------------------------------
def new_match_id(chat_id, now=None) -> str:
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(now or time.time()))
    safe_chat = str(chat_id).replace("-", "m")
    for _ in range(20):
        candidate = f"{stamp}-{safe_chat}-{secrets.token_hex(3)}"
        if not match_path(candidate).exists():
            return candidate
    return f"{stamp}-{safe_chat}-{secrets.token_hex(6)}"  # pragma: no cover


def match_path(match_id) -> Path:
    return matches_dir() / f"{match_id}.json"


def archive_path(match_id) -> Path:
    return archive_dir() / f"{match_id}.json"


# ---------------------------------------------------------------------------
# نوشتن/خواندن
# ---------------------------------------------------------------------------
def save_match(match) -> Path:
    ensure_dirs()
    path = match_path(match["match_id"])
    match["updated_at"] = time.time()
    write_json(path, match)
    _sync_index(match)
    return path


def load_match(match_id):
    path = match_path(match_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def archive_match(match):
    """کپی نهایی مسابقه در آرشیو + حذف از فهرست باز (فایل اصلی می‌ماند)."""
    ensure_dirs()
    write_json(archive_path(match["match_id"]), match)
    _remove_from_index(match.get("chat_id"), match["match_id"])
    return archive_path(match["match_id"])


def delete_match(match_id):
    path = match_path(match_id)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    _remove_from_index(None, match_id)


# ---------------------------------------------------------------------------
# index: chat_id → [match_id, …]
# ---------------------------------------------------------------------------
def _read_index() -> dict:
    path = index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(data):
    ensure_dirs()
    write_json(index_path(), data)


def _sync_index(match):
    chat_id = str(match.get("chat_id"))
    data = _read_index()
    ids = [i for i in data.get(chat_id, []) if (matches_dir() / f"{i}.json").exists()]
    if match["match_id"] not in ids:
        ids.append(match["match_id"])
    data[chat_id] = ids[-5:]        # فقط چند شناسهٔ آخر هر گروه
    _write_index(data)


def _remove_from_index(chat_id, match_id):
    data = _read_index()
    changed = False
    for key, ids in list(data.items()):
        if chat_id is not None and key != str(chat_id):
            continue
        if match_id in ids:
            data[key] = [i for i in ids if i != match_id]
            changed = True
    if changed:
        _write_index(data)


def chat_matches(chat_id):
    """شناسهٔ مسابقه‌های همین گروه (تازه‌ترین آخر)."""
    data = _read_index()
    return list(data.get(str(chat_id), []))


def open_matches(chat_id=None):
    """مسابقه‌هایی که ``phase != finished`` هستند."""
    result = []
    ids = []
    if chat_id is not None:
        ids = chat_matches(chat_id)
    else:
        try:
            ids = [p.stem for p in matches_dir().glob("*.json")]
        except OSError:
            ids = []
    for match_id in ids:
        match = load_match(match_id)
        if not match:
            continue
        if match.get("phase") != "finished":
            result.append(match)
    return result


def all_open_matches():
    result = []
    try:
        paths = sorted(matches_dir().glob("*.json"))
    except OSError:
        return result
    for path in paths:
        try:
            match = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if match.get("phase") != "finished":
            result.append(match)
    return result


def stats() -> dict:
    try:
        open_count = len(all_open_matches())
    except Exception:
        open_count = 0
    try:
        archived = len(list(archive_dir().glob("*.json")))
    except OSError:
        archived = 0
    return {"base_dir": str(base_dir()), "open": open_count, "archived": archived}
