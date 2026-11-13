"""🗂 دفترچهٔ پایدارِ مجازات‌های «ضد کانال و بیوگرافی».

چرا لازم است؟

* **سکوتِ دائمی باید از ری‌استارت جان سالم ببرد.** در سروش/تلگرام
  محدودیت سمتِ سرور است، ولی هر عملیات ادمینیِ دیگری (باگِ سرویس، ری‌ستِ
  دسترسی‌ها هنگام بالا آمدن، یا unmute شدن توسط قابلیت‌های دیگر مثل
  «محدودیت پیام») می‌تواند آن را پاک کند. پس هر مجازات اینجا ثبت می‌شود تا
  ربات (الف) در استارتاپ دوباره اعمالش کند و (ب) اگر کاربرِ ساکت‌شده
  دوباره پیام داد (= سکوتش برداشته شده) فوراً دوباره ساکتش کند.
* **بیوگرافی باید همیشه چک شود.** کسی که یک‌بار تخلف کرده در فهرست
  «زیرِ نظر» می‌ماند تا پیامِ بعدی‌اش حتماً با پروفایلِ تازه بررسی شود،
  نه با کشِ قدیمی.

محرمانگی: هیچ متنِ بیوگرافی/نامِ کانال اینجا ذخیره نمی‌شود؛ فقط
«شناسه‌ها + نوعِ تخلف + زمانِ پایان».

فایل: ``~/.{INSTANCE}/config/profile_guard_penalties.json``
"""

from __future__ import annotations

import time

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file("profile_guard_penalties.json")

#: سقفِ تعداد رکوردها (قدیمی‌ترها هرس می‌شوند) — فایل هرگز باد نمی‌کند.
MAX_ENTRIES = 1000

#: بعد از پایانِ سکوتِ موقت، کاربر این‌قدر «زیرِ نظر» بماند (ثانیه).
WATCH_GRACE = 30 * 60

_cache: dict | None = None
_cache_mtime: int | None = None


def _file_mtime() -> int | None:
    try:
        return FILE.stat().st_mtime_ns
    except OSError:
        return None


def load() -> dict:
    """نقشهٔ ``"chat_id:user_id"`` → رکورد (با کشِ mtime)."""
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
        entries = (raw.get("entries") if isinstance(raw, dict) else None) or {}
        _cache = {str(k): dict(v) for k, v in entries.items()
                  if isinstance(v, dict)}
    _cache_mtime = mtime
    return _cache


def _save(entries: dict) -> None:
    global _cache, _cache_mtime
    if len(entries) > MAX_ENTRIES:
        # قدیمی‌ترین‌ها حذف می‌شوند؛ رکوردهای دائمی اولویتِ ماندن دارند.
        ordered = sorted(
            entries.items(),
            key=lambda item: (
                0 if item[1].get("until") is None else 1,
                float(item[1].get("ts") or 0),
            ),
        )
        entries = dict(ordered[-MAX_ENTRIES:])
    write_json(FILE, {"version": 1, "entries": entries}, indent=2)
    _cache = entries
    _cache_mtime = _file_mtime()


def _key(chat_id, user_id) -> str:
    return f"{chat_id}:{user_id}"


def record(chat_id, user_id, kind: str, until_ts=None) -> None:
    """ثبتِ یک مجازات. ``until_ts=None`` یعنی دائمی."""
    entries = dict(load())
    entries[_key(chat_id, user_id)] = {
        "chat_id": str(chat_id),
        "user_id": str(user_id),
        "kind": str(kind),
        "until": None if until_ts is None else float(until_ts),
        "ts": time.time(),
    }
    _save(entries)


def get(chat_id, user_id) -> dict | None:
    entry = load().get(_key(chat_id, user_id))
    return dict(entry) if entry else None


def forget(chat_id, user_id) -> None:
    entries = dict(load())
    if entries.pop(_key(chat_id, user_id), None) is not None:
        _save(entries)


def is_permanent(chat_id, user_id) -> bool:
    entry = get(chat_id, user_id)
    return bool(entry) and entry.get("until") is None


def is_watched(chat_id, user_id, *, now: float | None = None) -> bool:
    """آیا پروفایلِ این کاربر باید در پیامِ بعدی «تازه» خوانده شود؟

    * سکوتِ دائمی ⇒ همیشه بله.
    * سکوتِ موقتِ فعال یا تازه‌تمام‌شده (تا ``WATCH_GRACE``) ⇒ بله.
    """
    entry = get(chat_id, user_id)
    if not entry:
        return False
    until = entry.get("until")
    if until is None:
        return True
    now = time.time() if now is None else now
    try:
        return float(until) + WATCH_GRACE >= now
    except (TypeError, ValueError):
        return False


def permanent_entries(chat_id=None) -> list[dict]:
    """همهٔ سکوت‌های دائمی (برای اعمالِ دوباره پس از ری‌استارت)."""
    out = []
    for entry in load().values():
        if entry.get("until") is not None:
            continue
        if chat_id is not None and str(entry.get("chat_id")) != str(chat_id):
            continue
        out.append(dict(entry))
    return out


def prune(*, now: float | None = None) -> int:
    """حذفِ رکوردهای موقتِ کهنه (پایان‌یافته + گذشته از grace)."""
    now = time.time() if now is None else now
    entries = dict(load())
    removed = 0
    for key, entry in list(entries.items()):
        until = entry.get("until")
        if until is None:
            continue
        try:
            if float(until) + WATCH_GRACE < now:
                entries.pop(key, None)
                removed += 1
        except (TypeError, ValueError):
            entries.pop(key, None)
            removed += 1
    if removed:
        _save(entries)
    return removed


def clear() -> None:
    """پاک‌سازی کامل (فقط تست)."""
    global _cache, _cache_mtime
    _cache = {}
    try:
        FILE.unlink()
    except OSError:
        pass
    _cache_mtime = _file_mtime()


__all__ = [
    "FILE", "MAX_ENTRIES", "WATCH_GRACE", "record", "get", "forget",
    "is_permanent", "is_watched", "permanent_entries", "prune", "clear",
    "load",
]
