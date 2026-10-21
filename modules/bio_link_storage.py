"""🗄 ذخیره‌سازی پایدار «فیلتر لینک بیوگرافی» — به تفکیک گروه.

هر گروه سه چیز دارد:
  * تنظیمات: ``enabled`` (فعال/خاموش) و ``detect_username`` (آیا آیدی
    ``@username`` تنها هم لینک حساب شود).
  * وضعیت هر کاربر که یک بار بررسی شده است: ``link`` (بولین)، ``label``
    (نوع لینک)، ``status`` (نتیجهٔ API)، ``checked_at`` (زمان یونیکس)،
    ``bio_len`` (طول بیو، برای گزارش بدون ذخیرهٔ متن بیو).
  * اطلاعات آخرین اسکن گروه.

متن بیوگرافی ذخیره **نمی‌شود** (نه لازم است، نه امن)؛ فقط نتیجهٔ تشخیص و
طول آن نگه داشته می‌شود.

نوشتن روی دیسک اتمیک است (``modules.atomic_write``) و برای جلوگیری از
فشار I/O در زمان اسکن، نوشتن با debounce انجام می‌شود: در هر ۵۰ تغییر یا
هر ۲۰ ثانیه یک‌بار، و در پایان هر اسکن با ``flush()`` قطعی می‌شود.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file("bio_link.json")

# سقف تعداد کاربران نگه‌داری‌شده در هر گروه؛ قدیمی‌ترین‌ها حذف می‌شوند تا
# فایل روی Termux بی‌نهایت رشد نکند.
MAX_USERS_PER_GROUP = 8000

# فاصلهٔ پیش‌فرض تازه‌سازی وضعیت یک کاربر (۶ ساعت)
DEFAULT_TTL_SECONDS = 6 * 60 * 60

_AUTOSAVE_EVERY = 50
_AUTOSAVE_SECONDS = 20.0

_cache = None
_cache_mtime = None
_dirty = 0
_last_save = 0.0


# ---------------------------------------------------------------------------
# چرخهٔ فایل
# ---------------------------------------------------------------------------

def _empty():
    return {"version": 1, "groups": {}}


def _load():
    global _cache, _cache_mtime
    try:
        mtime = FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    data = None
    if mtime is not None:
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            data = None
    if not isinstance(data, dict):
        data = _empty()
    if not isinstance(data.get("groups"), dict):
        data["groups"] = {}
    _cache = data
    _cache_mtime = mtime
    return _cache


def _save():
    global _cache_mtime, _dirty, _last_save
    data = _cache if _cache is not None else _empty()
    try:
        write_json(FILE, data, indent=None)
    except Exception:
        # ذخیره‌سازی نباید مسیر پیام را بشکند؛ تغییر بعدی دوباره تلاش می‌کند.
        return
    try:
        _cache_mtime = FILE.stat().st_mtime_ns
    except OSError:
        _cache_mtime = None
    _dirty = 0
    _last_save = time.time()


def flush():
    """نوشتن قطعی تغییرات معلق روی دیسک."""
    if _dirty:
        _save()


def _touch():
    """ثبت یک تغییر و نوشتن در صورت رسیدن به آستانه."""
    global _dirty
    _dirty += 1
    if _dirty >= _AUTOSAVE_EVERY or (time.time() - _last_save) >= _AUTOSAVE_SECONDS:
        _save()


def reset_cache():
    """پاک کردن کش حافظه (برای تست و پس از بازیابی فایل)."""
    global _cache, _cache_mtime, _dirty, _last_save
    _cache = None
    _cache_mtime = None
    _dirty = 0
    _last_save = 0.0


# ---------------------------------------------------------------------------
# تنظیمات گروه
# ---------------------------------------------------------------------------

def _group(chat_id, create=False):
    data = _load()
    key = str(normalize_group_id(chat_id))
    record = data["groups"].get(key)
    if not isinstance(record, dict):
        if not create:
            return None
        record = {"enabled": None, "detect_username": None, "users": {}, "scan": {}}
        data["groups"][key] = record
    if not isinstance(record.get("users"), dict):
        record["users"] = {}
    if not isinstance(record.get("scan"), dict):
        record["scan"] = {}
    return record


def _default_enabled() -> bool:
    raw = os.getenv("BIO_LINK_DEFAULT_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _default_detect_username() -> bool:
    raw = os.getenv("BIO_LINK_DETECT_USERNAME", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def is_enabled(chat_id) -> bool:
    """آیا فیلتر لینک بیو در این گروه فعال است.

    پیش‌فرضِ گروه‌های بدون تنظیم با ``BIO_LINK_DEFAULT_ENABLED`` کنترل
    می‌شود (پیش‌فرض: روشن) تا قابلیت واقعاً برای گروه‌های فعال کار کند.
    """
    record = _group(chat_id)
    value = None if record is None else record.get("enabled")
    if value is None:
        return _default_enabled()
    return bool(value)


def set_enabled(chat_id, enabled: bool) -> bool:
    """فعال/خاموش کردن؛ خروجی ``True`` اگر مقدار واقعاً تغییر کرده باشد."""
    record = _group(chat_id, create=True)
    new_value = bool(enabled)
    if record.get("enabled") == new_value:
        return False
    record["enabled"] = new_value
    _touch()
    flush()
    return True


def detect_username(chat_id) -> bool:
    record = _group(chat_id)
    value = None if record is None else record.get("detect_username")
    if value is None:
        return _default_detect_username()
    return bool(value)


def set_detect_username(chat_id, enabled: bool) -> bool:
    record = _group(chat_id, create=True)
    new_value = bool(enabled)
    if record.get("detect_username") == new_value:
        return False
    record["detect_username"] = new_value
    _touch()
    flush()
    return True


def ttl_seconds() -> int:
    try:
        value = int(os.getenv("BIO_LINK_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return max(60, value)


# ---------------------------------------------------------------------------
# وضعیت کاربران
# ---------------------------------------------------------------------------

def get_user(chat_id, user_id) -> Optional[dict]:
    """رکورد بررسی‌شدهٔ یک کاربر در این گروه، یا ``None``."""
    record = _group(chat_id)
    if record is None:
        return None
    entry = record["users"].get(str(user_id))
    return entry if isinstance(entry, dict) else None


def set_user(chat_id, user_id, *, link: bool, label=None, status: str = "ok",
             bio_len: int = 0, checked_at: Optional[float] = None,
             name: Optional[str] = None,
             exempt: Optional[str] = None) -> dict:
    """ثبت/به‌روزرسانی وضعیت یک کاربر.

    ``name`` فقط برای نمایش در «لیست لینک بیو» است (نام عمومی گروه) و متن
    بیوگرافی هرگز ذخیره نمی‌شود.
    """
    record = _group(chat_id, create=True)
    users = record["users"]
    entry = {
        "link": bool(link),
        "label": label or None,
        "status": status,
        "bio_len": int(bio_len or 0),
        "checked_at": float(checked_at if checked_at is not None else time.time()),
    }
    if name:
        entry["name"] = str(name)[:80]
    if exempt:
        # دلیل معافیت (ادمینِ گروه/مالک/ادمین ثبت‌شده/VIP) — برای این کاربر
        # هیچ اخطار یا حذفی اعمال نمی‌شود.
        entry["exempt"] = str(exempt)[:32]
    users[str(user_id)] = entry
    _prune(users)
    _touch()
    return entry


def _prune(users: dict):
    overflow = len(users) - MAX_USERS_PER_GROUP
    if overflow <= 0:
        return
    # قدیمی‌ترین بررسی‌ها اول حذف می‌شوند.
    ordered = sorted(
        users.items(),
        key=lambda item: float((item[1] or {}).get("checked_at") or 0),
    )
    for key, _value in ordered[:overflow]:
        users.pop(key, None)


def remove_user(chat_id, user_id) -> bool:
    record = _group(chat_id)
    if record is None:
        return False
    if record["users"].pop(str(user_id), None) is None:
        return False
    _touch()
    return True


def clear_group(chat_id) -> int:
    """پاک کردن کامل وضعیت کاربران یک گروه؛ خروجی تعداد رکوردهای پاک‌شده."""
    record = _group(chat_id)
    if record is None:
        return 0
    count = len(record["users"])
    record["users"] = {}
    record["scan"] = {}
    _touch()
    flush()
    return count


def flagged_users(chat_id):
    """کاربران دارای لینک در بیو، تازه‌ترین بررسی اول."""
    record = _group(chat_id)
    if record is None:
        return []
    rows = [
        (key, value)
        for key, value in record["users"].items()
        if isinstance(value, dict) and value.get("link") and not value.get("exempt")
    ]
    rows.sort(key=lambda item: float((item[1] or {}).get("checked_at") or 0), reverse=True)
    return rows


def stats(chat_id) -> dict:
    """آمار کوتاه برای دستور وضعیت."""
    record = _group(chat_id)
    users = record["users"] if record else {}
    total = len(users)
    with_link = sum(
        1 for value in users.values()
        if isinstance(value, dict) and value.get("link") and not value.get("exempt")
    )
    exempt = sum(
        1 for value in users.values()
        if isinstance(value, dict) and value.get("exempt")
    )
    errors = sum(
        1 for value in users.values()
        if isinstance(value, dict) and value.get("status") not in {"ok", "empty"}
    )
    if record and record.get("enabled") is None:
        enabled_source = "default"
    else:
        enabled_source = "group"
    return {
        "enabled": is_enabled(chat_id),
        "detect_username": detect_username(chat_id),
        "enabled_source": enabled_source,
        "checked": total,
        "with_link": with_link,
        "exempt": exempt,
        "errors": errors,
        "scan": dict(record.get("scan") or {}) if record else {},
        "ttl_seconds": ttl_seconds(),
    }


# ---------------------------------------------------------------------------
# آخرین اسکن گروه
# ---------------------------------------------------------------------------

def mark_scan(chat_id, **info):
    record = _group(chat_id, create=True)
    payload = dict(record.get("scan") or {})
    payload.update(info)
    payload["at"] = float(info.get("at") or time.time())
    record["scan"] = payload
    _touch()
    flush()
    return payload


def last_scan(chat_id) -> dict:
    record = _group(chat_id)
    return dict(record.get("scan") or {}) if record else {}
