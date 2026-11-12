# -*- coding: utf-8 -*-
"""♻️ dedupِ سبکِ «همین پیام قبلاً پردازش شده؟».

مشکلِ مشاهده‌شده: پس از reconnect یکی از بازی‌ها (JADVALESTAN) برای **یک**
پیام دوبار اجرا شد و دو ``SendMessageRequest`` رفت. اگر همان
``(chat_id, message_id)`` دوباره به هندلر برسد، باید بدونِ هیچ اثرِ جانبی
نادیده گرفته شود.

قاعده‌های سخت:

* کلید = ``(chat_id, message_id)``. پیامِ تازه شناسهٔ تازه دارد، پس **هیچ
  پیامِ واقعیِ تازه‌ای گم نمی‌شود**؛ فقط تکرارِ همان پیام حذف می‌شود.
* سقفِ حافظه: ۱۰۰۰ کلید (LRU) و TTL ۱۰ دقیقه — هر دو از محیط قابلِ تغییر.
* فلگِ ``HANDLER_MESSAGE_DEDUP_ENABLED`` (پیش‌فرض روشن): با ``0`` کردنش
  رفتار دقیقاً مثل قبل می‌شود.
* مسیرِ «دستورِ تأییدشدهٔ دو مرحله‌ای» عمداً از این dedup رد **نمی‌شود**
  (آن مسیر همان متنِ دستور را با شناسهٔ پیامِ اصلی دوباره اجرا می‌کند)؛
  این استثنا در خودِ هندلر با ``two_factor_confirmed_active()`` اعمال می‌شود.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

DEFAULT_TTL = 600.0
DEFAULT_MAX = 1000

_lock = threading.RLock()
#: key → (زمانِ پردازش، اثرانگشتِ متن)
_seen: "OrderedDict[tuple, tuple]" = OrderedDict()

#: شمارنده‌ها (فقط برای تست/گزارش؛ رفتار را عوض نمی‌کنند)
COUNTERS = {"checked": 0, "duplicates": 0, "skipped_flag": 0}


def _env_float(name, default, minimum=1.0, maximum=86400.0):
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_int(name, default, minimum=1, maximum=1_000_000):
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def enabled() -> bool:
    raw = os.getenv("HANDLER_MESSAGE_DEDUP_ENABLED")
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


def ttl_seconds() -> float:
    return _env_float("HANDLER_MESSAGE_DEDUP_TTL", DEFAULT_TTL, 1.0, 86400.0)


def max_entries() -> int:
    return _env_int("HANDLER_MESSAGE_DEDUP_MAX", DEFAULT_MAX, 1, 100_000)


def _key(chat_id, message_id):
    try:
        chat = int(chat_id)
    except (TypeError, ValueError):
        chat = str(chat_id)
    try:
        message = int(message_id)
    except (TypeError, ValueError):
        message = str(message_id)
    return (chat, message)


def _fingerprint(text) -> str:
    """اثرانگشتِ ارزانِ متن (برای تشخیصِ «همان پیام» از «پیامِ دیگر با شناسهٔ تکراری»)."""
    import hashlib

    return hashlib.blake2b(
        str(text or "").encode("utf-8", errors="ignore"), digest_size=8
    ).hexdigest()


def is_duplicate(chat_id, message_id, text=None) -> bool:
    """آیا این پیام قبلاً دیده شده؟ (اولین‌بار ``False``، تکرار ``True``)

    بدونِ ``chat_id`` یا ``message_id`` هیچ‌وقت «تکراری» اعلام نمی‌شود؛
    نبودِ شناسه یعنی نمی‌توان با اطمینان گفت همان پیام است. اگر همان شناسه
    با **متنِ متفاوت** برسد، پیامِ دیگری است و dedup نمی‌شود.
    """
    if chat_id is None or message_id is None:
        return False
    COUNTERS["checked"] += 1
    if not enabled():
        COUNTERS["skipped_flag"] += 1
        return False
    key = _key(chat_id, message_id)
    now = time.time()
    ttl = ttl_seconds()
    limit = max_entries()
    print_ = _fingerprint(text)
    with _lock:
        seen = _seen.get(key)
        if seen is not None and now - seen[0] <= ttl and seen[1] == print_:
            _seen.move_to_end(key)
            COUNTERS["duplicates"] += 1
            return True
        _seen[key] = (now, print_)
        _seen.move_to_end(key)
        while len(_seen) > limit:
            _seen.popitem(last=False)
        # پاک‌سازیِ تنبلِ کلیدهای کهنه تا حافظه بی‌دلیل بزرگ نماند.
        if len(_seen) > 16:
            stale = [item for item, at in _seen.items() if now - at[0] > ttl]
            for item in stale:
                _seen.pop(item, None)
    return False


def reset() -> None:
    """پاک‌کردنِ کاملِ حالت (فقط تست)."""
    with _lock:
        _seen.clear()
        for key in COUNTERS:
            COUNTERS[key] = 0


def size() -> int:
    with _lock:
        return len(_seen)
