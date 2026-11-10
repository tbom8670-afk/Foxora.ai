# -*- coding: utf-8 -*-
"""🗄️ ذخیره‌سازی پایدارِ قابلیت‌های جدید مدیریتی.

چهار فایل مستقل در ``config/`` (برای اینکه خرابیِ یکی به بقیه سرایت نکند):

* ``exclusive_access.json``  — فهرست کاربران «اختصاصی» هر گروه
* ``timed_message.json``     — زمان‌سنج‌ها و پیام‌های ثبت‌شدهٔ هر اجرا
* ``user_reports.json``      — پرونده‌های «گزارش» و محدودیت ۷ ثانیه
* ``violators.json``         — متخلفانِ هر گروه
                                (``{chat_id: {user_id: {since, set_by,
                                set_by_username}}}``)

قواعد مشترک (الزامات ماموریت):

* نوشتن **اتمیک** (temp + ``os.replace`` + ``fsync``) ⇒ kill وسط کار فایل را
  خراب نمی‌کند.
* نسخهٔ پشتیبان (``.bak``) و بازیابی خودکار اگر فایل اصلی خراب شود.
* **کش mtime**: خواندنِ مکرر بدون I/O اضافه؛ تغییر از پروسهٔ دیگر (یا
  ری‌استارت) دیده می‌شود.
* همهٔ زمان‌ها با ``time.time()`` (UTC epoch) ذخیره می‌شوند و نمایش با
  ``ZoneInfo("Asia/Tehran")`` انجام می‌شود. زمان‌سنجیِ داخلی (تایم‌اوت/بک‌آف)
  با ``time.monotonic()`` است تا تغییر ساعت دستگاه خطا نسازد.
* شناسه‌ها همیشه **عددی/رشته‌ای پایدار** ذخیره می‌شوند؛ یوزرنیم فقط برای نمایش.
"""
from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file

try:  # منطقهٔ زمانی رسمی پروژه
    from zoneinfo import ZoneInfo

    TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover - فقط اگر tzdata نصب نباشد
    TEHRAN = timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")


# ---------------------------------------------------------------------------
#  زمان
# ---------------------------------------------------------------------------
def now() -> float:
    """زمان دیوار به‌صورت UTC epoch (برای ذخیره و مقایسه)."""
    return time.time()


def monotonic() -> float:
    """ساعت یکنواخت برای تایم‌اوت/بک‌آف (تغییر ساعت دستگاه اثر ندارد)."""
    return time.monotonic()


def to_tehran(timestamp=None) -> datetime:
    """تبدیل به زمان تهران برای نمایش."""
    value = now() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(value, tz=TEHRAN)


def iso_tehran(timestamp=None) -> str:
    return to_tehran(timestamp).isoformat(timespec="seconds")


def parse_iso(text):
    """خواندن زمانِ ISO ذخیره‌شده؛ ``None`` اگر نامعتبر بود."""
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TEHRAN)
    return parsed


def fa_duration(seconds) -> str:
    """مدت‌زمان خوانا برای متن‌های فارسی (بدون وابستگی به قالب‌ها)."""
    try:
        total = int(max(0, round(float(seconds))))
    except (TypeError, ValueError):
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    if secs or not parts:
        parts.append(f"{secs} ثانیه")
    return " و ".join(parts)


# ---------------------------------------------------------------------------
#  موتور ذخیرهٔ اتمیک + کش mtime
# ---------------------------------------------------------------------------
class Storage:
    """یک فایل JSON اتمیک با کش mtime، نسخهٔ پشتیبان و قفل thread-safe."""

    def __init__(self, filename, default=None):
        self.path = Path(runtime_config_file(filename))
        self.backup_path = self.path.with_name(self.path.name + ".bak")
        self._default = default if default is not None else {}
        self._lock = threading.RLock()
        self._cache = None
        self._mtime = None

    # ---- پایه ----
    def _file_mtime(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _read_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def load(self):
        """داده (کپی) برمی‌گرداند؛ هرگز ``None``."""
        with self._lock:
            return deepcopy(self._data())

    def _data(self):
        mtime = self._file_mtime()
        if self._cache is not None and mtime == self._mtime:
            return self._cache
        data = self._read_file(self.path)
        if data is None:
            # فایل اصلی نیست/خراب است ⇒ آخرین نسخهٔ سالم
            data = self._read_file(self.backup_path) or deepcopy(self._default)
        self._cache = data
        self._mtime = mtime
        return self._cache

    def _invalidate(self):
        """کش را بی‌اعتبار می‌کند تا خواندن بعدی از دیسک باشد.

        لازم است چون ``_data()`` خودِ شیءِ کش را برمی‌گرداند و تغییرِ کاربر روی
        آن، mtime را عوض نمی‌کند؛ بدون این، نوشتنِ ما می‌توانست روی نسخهٔ
        کهنه انجام شود (و رکورد تازه از دست برود).
        """
        self._cache = None
        self._mtime = None

    def save(self, data):
        """نوشتن اتمیک + نسخهٔ پشتیبان؛ دادهٔ نوشته‌شده جای کش می‌نشیند."""
        with self._lock:
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(temp, self.path)
            try:
                backup_temp = self.backup_path.with_name(
                    self.backup_path.name + ".tmp")
                with open(backup_temp, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(backup_temp, self.backup_path)
            except OSError:
                pass
            self._cache = deepcopy(data)
            self._mtime = self._file_mtime()
            return True

    # ---- کمکی ----
    def update(self, mutate):
        """خواندن، تغییر و نوشتن در یک قفل؛ خروجیِ ``mutate`` برمی‌گردد."""
        with self._lock:
            # همیشه از دیسک بخوان: تغییرِ قبلیِ ما mtime را عوض نکرده است.
            self._invalidate()
            data = self._data()
            result = mutate(data)
            self.save(data)
            return result

    def reset(self):
        with self._lock:
            self.save(deepcopy(self._default))


def group_key(chat_id) -> str:
    """کلید پایدارِ گروه (همان شکلِ بقیهٔ storageهای پروژه)."""
    return str(normalize_group_id(chat_id))


def user_key(user_id) -> str:
    return str(int(user_id)) if str(user_id).lstrip("-").isdigit() else str(user_id)


#: سه فایل مستقل — هیچ قابلیتی داده‌ی دیگری را نمی‌خواند/نمی‌نویسد.
EXCLUSIVE_STORAGE = Storage("exclusive_access.json", {"groups": {}})
TIMED_STORAGE = Storage("timed_message.json", {"groups": {}})
REPORT_STORAGE = Storage("user_reports.json", {"groups": {}, "limits": {}})
#: «متخلف» — ساختارِ دقیقاً مطابق خواستهٔ کارفرما:
#: ``{chat_id: {user_id: {since, set_by, set_by_username}}}``
VIOLATORS_STORAGE = Storage("violators.json", {})


def reset_all():
    """پاک‌سازی کامل (فقط برای تست/ری‌ست دستی)."""
    EXCLUSIVE_STORAGE.reset()
    TIMED_STORAGE.reset()
    REPORT_STORAGE.reset()
    VIOLATORS_STORAGE.reset()
