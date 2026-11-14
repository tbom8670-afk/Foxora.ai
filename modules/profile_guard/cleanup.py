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


def _forget(chat_id, message_id=None, text=None) -> None:
    rows = []
    for row in _load():
        same_chat = str(row.get("chat_id")) == str(chat_id)
        same_id = message_id and str(row.get("message_id")) == str(message_id)
        same_text = text and _same_text(row.get("text"), text)
        if same_chat and (same_id or same_text):
            continue
        rows.append(row)
    _save(rows)


# ---------------------------------------------------------------------------
# کارِ حذف
# ---------------------------------------------------------------------------
def _chat_target(bot, chat_id):
    """peerِ گروهِ مقصد (اگر ربات بتواند بسازد) وگرنه شناسهٔ خام."""
    try:
        from modules.manage_features import peers
        target = peers.group_peer(bot, chat_id)
        if target is not None:
            return target
    except Exception:
        pass
    return chat_id


def _same_text(left, right) -> bool:
    if left is None or right is None:
        return False
    return str(left).strip() == str(right).strip()


async def _locate_by_text(bot, chat_id, text) -> Optional[int]:
    """پیامِ خودِ ربات را در انتهای همین گروه پیدا می‌کند (وقتی شناسه نداریم).

    بعضی نسخه‌ها/لفافه‌های ارسال، ``Message`` برنمی‌گردانند؛ در آن حالت
    شناسهٔ پیام را از تاریخچهٔ کوتاهِ گروه پیدا می‌کنیم و همان را پاک می‌کنیم.
    """
    client = getattr(bot, "client", None)
    if client is None or not text:
        return None
    try:
        history = await client.get_messages(_chat_target(bot, chat_id), limit=20)
    except Exception as error:
        _log(bot, f"history lookup failed chat_id={chat_id} "
                  f"error={type(error).__name__}", error=True)
        return None
    # ``get_messages`` در بعضی نسخه‌ها یک ``Message`` تنها برمی‌گرداند، نه لیست.
    if history is None:
        return None
    if not isinstance(history, (list, tuple)):
        history = [history]
    for message in history:
        body = getattr(message, "message", None) or getattr(message, "text", None)
        if not _same_text(body, text):
            continue
        found = getattr(message, "id", None)
        if isinstance(found, int) and found > 0:
            return found
    return None


async def _delete_now(bot, chat_id, message_id=None, text=None) -> bool:
    client = getattr(bot, "client", None)
    if client is None:
        return False
    target_id = message_id or await _locate_by_text(bot, chat_id, text)
    if not target_id:
        return False
    try:
        await client.delete_messages(int(chat_id), [int(target_id)])
        return True
    except Exception as error:
        # پیام از قبل پاک شده باشد ⇒ خطا نیست.
        name = type(error).__name__.lower()
        if "notfound" in name or "message_ids_empty" in str(error).lower():
            return True
        _log(bot, f"delete failed chat_id={chat_id} id={target_id} "
                  f"error={type(error).__name__}", error=True)
        return False


async def _delete_after(bot, chat_id, message_id=None, delay: float = 0.0,
                        text=None) -> None:
    if delay > 0:
        await asyncio.sleep(delay)
    for attempt in range(1, RETRIES + 1):
        if await _delete_now(bot, chat_id, message_id, text):
            _forget(chat_id, message_id, text)
            _log(bot, f"deleted chat_id={chat_id} id={message_id or '(by text)'} "
                      f"attempt={attempt}")
            return
        if attempt < RETRIES:
            await asyncio.sleep(RETRY_DELAY)
    _forget(chat_id, message_id, text)
    _log(bot, f"gave up chat_id={chat_id} id={message_id} text={'yes' if text else 'no'}",
         error=True)


def schedule(bot, chat_id, sent, *, ttl: Optional[float] = None,
             text: Optional[str] = None) -> Optional["asyncio.Task"]:
    """پیامِ هشدار را برای حذف پس از ``ttl`` (پیش‌فرض ۶۰ ثانیه) ثبت می‌کند.

    اگر شناسهٔ پیام از نتیجهٔ ارسال درنیاید (بعضی لفافه‌های ارسال
    ``Message`` برنمی‌گردانند) با ``text`` در تاریخچهٔ کوتاهِ گروه پیدا و
    پاک می‌شود؛ پس حذف در هر حال انجام می‌شود.

    بدون await؛ یک تسکِ سبک برمی‌گرداند (برای تست) یا ``None``.
    """
    if chat_id is None:
        return None
    message_id = message_id_of(sent)
    if not message_id and not text:
        _log(bot, f"cannot schedule: no message id and no text "
                  f"(sent_type={type(sent).__name__})", error=True)
        return None
    delay = float(ttl_seconds() if ttl is None else ttl)
    _log(bot, f"scheduled chat_id={chat_id} "
              f"id={message_id or '(by text)'} ttl={delay:g}s")
    try:
        rows = _load()
        rows.append({"chat_id": int(chat_id), "message_id": int(message_id or 0),
                     "text": (str(text)[:400] if text else None),
                     "due": time.time() + delay})
        _save(rows)
    except Exception:
        pass
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:                      # خارج از لوپ (تست/استارتاپ)
        return None
    return loop.create_task(
        _delete_after(bot, chat_id, message_id, delay, text))


def recover(bot, logger=None) -> int:
    """پس از ری‌استارت، ردیف‌های باقی‌مانده را دوباره زمان‌بندی می‌کند."""
    rows = _load()
    # 🔎 نشانگرِ زنده‌بودنِ همین مسیر در استارتاپ (برای عیب‌یابی از روی لاگ)
    _log(bot, f"ready ttl={ttl_seconds()}s pending={len(rows)}")
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
        except (TypeError, ValueError):
            continue
        try:
            message_id = int(row.get("message_id") or 0) or None
        except (TypeError, ValueError):
            message_id = None
        due = float(row.get("due") or 0)
        if due + STALE_SECONDS < now:
            continue
        loop.create_task(_delete_after(bot, chat_id, message_id,
                                       max(0.0, due - now), row.get("text")))
        started += 1
    _log(bot, f"scheduled after restart count={started} total={len(rows)}")
    return started


__all__ = ["FILE", "DEFAULT_TTL_SECONDS", "ttl_seconds", "message_id_of",
           "schedule", "recover"]
