"""🧹 حذفِ خودکارِ پیام‌های هشدارِ «ضد کانال و بیوگرافی» پس از ۶۰ ثانیه.

این مسیر **مستقل** از سرویسِ عمومیِ ``modules/notice_cleanup`` است: بعضی
گروه‌ها/نسخه‌ها پیامِ ثبت‌شده در آن سرویس را پاک نمی‌کنند، پس این قابلیت
تایمر و کارگرِ خودش را دارد و پیام را با همان روشی پاک می‌کند که حذفِ
پیامِ متخلف در همین بسته انجام می‌شود:

    ``bot.client.delete_messages(chat_id, [message_id])``

ویژگی‌ها:

* زمان‌بندی در حافظه + ردیفِ پایدار روی دیسک (ری‌استارت ⇒ حذف از دست نمی‌رود).
* تلاشِ دوباره (۳ بار، ۳ ثانیه فاصله) و بعد رها کردنِ ردیف.
* هیچ await در مسیر پیام نیست؛ فقط یک ``asyncio.Task`` سبک ساخته می‌شود.
* سقفِ ۵۰۰ ردیف و حذفِ ردیف‌های خیلی کهنه (۶ ساعت) تا فایل باد نکند.

فایل: ``~/.{INSTANCE}/config/profile_guard_cleanup.json``
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file("profile_guard_cleanup.json")

DEFAULT_TTL_SECONDS = 60
MAX_ROWS = 500
RETRIES = 3
RETRY_DELAY = 3.0
#: ردیفی که بیشتر از این مدت گذشته، دیگر تلاش نمی‌شود (شناسهٔ کهنه).
STALE_SECONDS = 6 * 3600


def ttl_seconds() -> int:
    """مهلتِ پاک‌سازی (پیش‌فرض ۶۰ ثانیه)."""
    try:
        return max(5, min(86400, int(str(
            os.getenv("PROFILE_GUARD_NOTICE_TTL", DEFAULT_TTL_SECONDS)
        ).strip())))
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS


def _log(bot, message: str, *, error: bool = False) -> None:
    logger = getattr(bot, "logger", None)
    try:
        (logger.log_error if error else logger.log_info)(
            f"PROFILE GUARD CLEANUP {message}"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# شناسهٔ پیام از نتیجهٔ ارسال
# ---------------------------------------------------------------------------
def message_id_of(sent) -> Optional[int]:
    """شناسهٔ پیام از خروجیِ ``send_message``/``reply`` (یا ``None``)."""
    try:
        from modules.notice_cleanup import extract_sent_id
        found = extract_sent_id(sent)
        if found:
            return int(found)
    except Exception:
        pass
    for attr in ("id", "message_id"):
        value = getattr(sent, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    return None


# ---------------------------------------------------------------------------
# ردیف‌های پایدار
# ---------------------------------------------------------------------------
def _load() -> list[dict]:
    try:
        import json
        with open(FILE, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
        rows = raw.get("rows") if isinstance(raw, dict) else raw
        return [dict(row) for row in (rows or []) if isinstance(row, dict)]
    except Exception:
        return []


def _save(rows: list[dict]) -> None:
    now = time.time()
    clean = [row for row in rows
             if float(row.get("due") or 0) + STALE_SECONDS >= now]
    clean = clean[-MAX_ROWS:]
    try:
        write_json(FILE, {"version": 1, "rows": clean}, indent=2)
    except Exception:
        pass


def _forget(chat_id, message_id) -> None:
    rows = [row for row in _load()
            if not (str(row.get("chat_id")) == str(chat_id)
                    and str(row.get("message_id")) == str(message_id))]
    _save(rows)


# ---------------------------------------------------------------------------
# کارِ حذف
# ---------------------------------------------------------------------------
async def _delete_now(bot, chat_id, message_id) -> bool:
    client = getattr(bot, "client", None)
    if client is None:
        return False
    try:
        await client.delete_messages(int(chat_id), [int(message_id)])
        return True
    except Exception as error:
        # پیام از قبل پاک شده باشد ⇒ خطا نیست.
        name = type(error).__name__.lower()
        if "notfound" in name or "message_ids_empty" in str(error).lower():
            return True
        _log(bot, f"delete failed chat_id={chat_id} id={message_id} "
                  f"error={type(error).__name__}", error=True)
        return False


async def _delete_after(bot, chat_id, message_id, delay: float) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    for attempt in range(1, RETRIES + 1):
        if await _delete_now(bot, chat_id, message_id):
            _forget(chat_id, message_id)
            _log(bot, f"deleted chat_id={chat_id} id={message_id} "
                      f"after={ttl_seconds()}s")
            return
        if attempt < RETRIES:
            await asyncio.sleep(RETRY_DELAY)
    _forget(chat_id, message_id)
    _log(bot, f"gave up chat_id={chat_id} id={message_id}", error=True)


def schedule(bot, chat_id, sent, *, ttl: Optional[float] = None
             ) -> Optional["asyncio.Task"]:
    """پیامِ هشدار را برای حذف پس از ``ttl`` (پیش‌فرض ۶۰ ثانیه) ثبت می‌کند.

    بدون await؛ یک تسکِ سبک برمی‌گرداند (برای تست) یا ``None``.
    """
    message_id = message_id_of(sent)
    if chat_id is None or not message_id:
        _log(bot, f"cannot schedule: no message id "
                  f"(sent_type={type(sent).__name__})", error=True)
        return None
    delay = float(ttl_seconds() if ttl is None else ttl)
    try:
        rows = _load()
        rows.append({"chat_id": int(chat_id), "message_id": int(message_id),
                     "due": time.time() + delay})
        _save(rows)
    except Exception:
        pass
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:                      # خارج از لوپ (تست/استارتاپ)
        return None
    return loop.create_task(_delete_after(bot, chat_id, message_id, delay))


def recover(bot, logger=None) -> int:
    """پس از ری‌استارت، ردیف‌های باقی‌مانده را دوباره زمان‌بندی می‌کند."""
    rows = _load()
    if not rows:
        return 0
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    now = time.time()
    started = 0
    for row in rows:
        try:
            chat_id = int(row.get("chat_id"))
            message_id = int(row.get("message_id"))
        except (TypeError, ValueError):
            continue
        due = float(row.get("due") or 0)
        if due + STALE_SECONDS < now:
            continue
        loop.create_task(_delete_after(bot, chat_id, message_id,
                                       max(0.0, due - now)))
        started += 1
    if started:
        _log(bot, f"recovered pending deletions count={started}")
    return started


__all__ = ["FILE", "DEFAULT_TTL_SECONDS", "ttl_seconds", "message_id_of",
           "schedule", "recover"]
