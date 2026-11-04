# -*- coding: utf-8 -*-
"""⚖️ اجرای مجازات دادگاه روباه: حذف پیام‌های متهم به مدت ۶۰ ثانیه.

این ماژول روی **زیرساخت موجود** ربات سوار می‌شود و سیستم موازی نمی‌سازد:

* حذف پیام‌ها از همان صف حذف پروژه انجام می‌شود
  (``bot.message_delete_queue`` — ماژول ``modules/message_delete_queue``)
  با اولویت ۱ (همان اولویتی که پاک‌سازی خودکار اسپم استفاده می‌کند).
* تشخیص پیام‌های متهم در همان نقطهٔ ورودی هندلر گروه انجام می‌شود
  (``handlers/message_handler.handle_new_message``) که از قبل
  شناسهٔ پیام و فرستنده را دارد؛ هیچ اسکن تاریخچه یا RPC اضافه لازم نیست.

قواعد:

* **فقط** پیام‌های همان کاربرِ محکوم در همان گروه حذف می‌شوند.
* پنجره دقیقاً ``punishment_seconds`` (پیش‌فرض ۶۰) ثانیه است و بعد از
  پایان آن هیچ حذفی انجام نمی‌شود.
* اگر صف حذف گزارش دهد که پیامی پاک نشده (نبود دسترسی ربات، خطای
  entity، …) شمارش «ناموفق» بالا می‌رود و در گزارش نهایی صریحاً اعلام
  می‌شود؛ وضعیت موفق به‌اشتباه اعلام نمی‌شود.
* پنجره‌ها با کلید ``(chat_key, user_id)`` جدا هستند، پس مجازات یک گروه
  هرگز روی گروه یا بازی دیگر اثر ندارد.
"""
from __future__ import annotations

import time

from modules.court_game import storage, texts

PUNISH_SECONDS = 60

# (chat_key, user_id) → پنجره
_WINDOWS = {}
_SAFE_LIMIT = 500


def _chat_key(chat_id) -> str:
    """کلید پایدار گروه؛ ``-100123`` و ``123`` یک کلید می‌شوند.

    از همان قاعدهٔ ``normalize_group_id`` پروژه استفاده می‌کند تا کلید
    پنجرهٔ مجازات با کلیدهای بقیهٔ بخش‌های ربات یکسان باشد.
    """
    try:
        from modules.group_id import normalize_group_id
        value = getattr(chat_id, "channel_id", None)
        if value is None:
            value = getattr(chat_id, "chat_id", None)
        if value is None:
            value = chat_id
        return str(normalize_group_id(int(value)))
    except Exception:
        try:
            return str(int(chat_id))
        except Exception:
            return str(chat_id)


def _key(chat_id, user_id):
    return (_chat_key(chat_id), str(user_id))


def start(chat_id, user_id, case_id=None, seconds=None, group_event=None):
    """باز کردن پنجرهٔ مجازات. خروجی: dict پنجره."""
    seconds = PUNISH_SECONDS if seconds is None else int(seconds)
    now = time.monotonic()
    window = {
        "chat_id": chat_id,
        "chat_key": _chat_key(chat_id),
        "user_id": str(user_id),
        "case_id": case_id,
        "seconds": seconds,
        "started_at": now,
        "ends_at": now + seconds,
        "deleted": 0,
        "failed": 0,
        "observed": 0,
        "closed": False,
        "started_iso": storage.now_iso(),
    }
    _WINDOWS[_key(chat_id, user_id)] = window
    _prune(now)
    try:
        storage.write_session(
            "punish_%s_%s" % (storage.chat_key_for(chat_id), user_id),
            {
                "kind": "punishment",
                "chat_key": storage.chat_key_for(chat_id),
                "chat_id": chat_id,
                "user_id": str(user_id),
                "case_id": case_id,
                "seconds": seconds,
                "saved_at": storage.now_iso(),
            },
        )
    except Exception:
        pass
    return dict(window)


def _prune(now=None):
    now = time.monotonic() if now is None else now
    for key, window in list(_WINDOWS.items()):
        if window.get("closed") or window["ends_at"] <= now - 5:
            _WINDOWS.pop(key, None)
    while len(_WINDOWS) > _SAFE_LIMIT:
        _WINDOWS.pop(next(iter(_WINDOWS)), None)


def active(chat_id, user_id, now=None) -> bool:
    now = time.monotonic() if now is None else now
    window = _WINDOWS.get(_key(chat_id, user_id))
    return bool(window and not window.get("closed") and window["ends_at"] > now)


def window_of(chat_id, user_id):
    window = _WINDOWS.get(_key(chat_id, user_id))
    return dict(window) if window else None


def remaining(chat_id, user_id, now=None) -> float:
    now = time.monotonic() if now is None else now
    window = _WINDOWS.get(_key(chat_id, user_id))
    if not window or window.get("closed"):
        return 0.0
    return max(0.0, window["ends_at"] - now)


def finish(chat_id, user_id):
    """بستن پنجره و برگرداندن آمار؛ بعد از آن حذفی انجام نمی‌شود."""
    key = _key(chat_id, user_id)
    window = _WINDOWS.get(key)
    if not window:
        return None
    window["closed"] = True
    window["ended_at"] = time.monotonic()
    stats = dict(window)
    _WINDOWS.pop(key, None)
    try:
        storage.delete_session(
            "punish_%s_%s" % (storage.chat_key_for(chat_id), user_id))
    except Exception:
        pass
    return stats


def _log(bot, message, error=False):
    logger = getattr(bot, "logger", None)
    if logger is None:
        return
    try:
        (logger.log_error if error else logger.log_info)(message)
    except Exception:
        pass


def observe_message(bot, event, chat_id, user_id, message_id, now=None):
    """اگر پنجرهٔ مجازات این کاربر باز است، پیامش را به صف حذف می‌دهد.

    این تابع **sync** است و هیچ RPC‌ای در مسیر پیام اجرا نمی‌کند؛ فقط یک
    job روی صف موجود می‌گذارد. خروجی: True اگر پیام برای حذف ثبت شد.
    """
    if chat_id is None or user_id is None or message_id is None:
        return False
    now = time.monotonic() if now is None else now
    key = _key(chat_id, user_id)
    window = _WINDOWS.get(key)
    if not window or window.get("closed") or window["ends_at"] <= now:
        return False
    if str(user_id) != window["user_id"]:
        return False
    if not isinstance(message_id, int) or message_id <= 0:
        return False

    window["observed"] += 1
    queue = getattr(bot, "message_delete_queue", None)
    if queue is None:
        window["failed"] += 1
        _log(bot, f"COURT PUNISH NO DELETE QUEUE chat_id={chat_id} "
                  f"message_id={message_id}", error=True)
        return True
    try:
        future = queue.enqueue(chat_id, [message_id], priority=1)
    except Exception as error:
        window["failed"] += 1
        _log(bot, f"COURT PUNISH ENQUEUE FAILED chat_id={chat_id} "
                  f"message_id={message_id} error={error!r}", error=True)
        return True

    def _done(result):
        try:
            deleted, remaining_ids = result.result()
        except Exception as error:
            window["failed"] += 1
            _log(bot, f"COURT PUNISH DELETE FAILED chat_id={chat_id} "
                      f"message_id={message_id} error={error!r}", error=True)
            return
        deleted = int(deleted or 0)
        failed_ids = list(remaining_ids or [])
        if deleted > 0 and not failed_ids:
            window["deleted"] += deleted
        else:
            window["deleted"] += max(0, deleted)
            window["failed"] += max(1, len(failed_ids))
        _log(bot, f"COURT PUNISH DELETE chat_id={chat_id} "
                  f"message_id={message_id} deleted={deleted} "
                  f"failed={len(failed_ids)}")

    try:
        future.add_done_callback(_done)
    except Exception:
        pass
    return True


def report_text(stats) -> str:
    stats = stats or {}
    deleted = int(stats.get("deleted", 0))
    failed = int(stats.get("failed", 0))
    observed = int(stats.get("observed", 0))
    if observed == 0:
        return texts.punishment_report_no_messages()
    return texts.punishment_report(deleted, failed)


def summary(stats) -> dict:
    stats = stats or {}
    return {
        "status": "ok" if int(stats.get("failed", 0)) == 0 else "partial",
        "deleted": int(stats.get("deleted", 0)),
        "failed": int(stats.get("failed", 0)),
        "observed": int(stats.get("observed", 0)),
        "seconds": int(stats.get("seconds", PUNISH_SECONDS)),
        "case_id": stats.get("case_id"),
        "user_id": stats.get("user_id"),
    }


def reset_all():
    """فقط برای تست."""
    _WINDOWS.clear()
