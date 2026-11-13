"""🛰 لایهٔ API قابلیت «ضد کانال و بیوگرافی» — تنها جایی که RPC می‌زند.

مسیر واقعی (تأییدشده روی Schema نصب‌شدهٔ SPlusthon 1.1.4):

    ``users.getFullUser#b60f5918`` → ``users.UserFull``
        ├─ ``full_user: UserFull``
        │     ├─ ``about``                 → متن بیوگرافی
        │     ├─ ``personal_channel_id``   → شناسهٔ کانال متصل به پروفایل
        │     └─ ``personal_channel_message`` → شناسهٔ پیام پیش‌نمایش
        └─ ``chats: Vector<Chat>``         → معمولاً همان کانال با
                                             ``access_hash``/``title``/``participants_count``

اگر کانال در وکتور ``chats`` نبود، به‌ترتیب:

    1. کش entity همان نشست (``client.get_input_entity(PeerChannel(id))``)
    2. ``channels.getChannels`` با ``InputChannel(id, access_hash)``
    3. (فقط وقتی لازم شد و برای نمایش تعداد اعضا) ``channels.getFullChannel``

سیاستِ «کم‌هزینه بودن»:

* هر کاربر **یک اسنپ‌شات** دارد با TTL (پیش‌فرض ۱۰ دقیقه)؛ پیام‌های
  بعدیِ همان کاربر هیچ RPC‌ای نمی‌زنند.
* همهٔ RPCها از یک سمافور + فاصلهٔ حداقلی + احترام به FloodWait رد می‌شوند.
* خطا ⇒ ``retry_after`` برای همان کاربر؛ نه حلقهٔ خطا، نه فشار روی سرور.

هیچ متن بیوگرافی، توکن، شماره یا اطلاعات نشست لاگ نمی‌شود.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

# ---------------------------------------------------------------------------
# وضعیت‌ها
# ---------------------------------------------------------------------------
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_NO_ACCESS = "no_access"
STATUS_ERROR = "error"
STATUS_FLOOD = "flood_wait"


def _env_int(name: str, default: int, minimum: int = 0,
             maximum: int = 10_000_000) -> int:
    try:
        return max(minimum, min(maximum, int(str(os.getenv(name, default)).strip())))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0,
               maximum: float = 3600.0) -> float:
    try:
        return max(minimum, min(maximum, float(str(os.getenv(name, default)).strip())))
    except (TypeError, ValueError):
        return default


def ttl_seconds() -> int:
    """عمر اسنپ‌شاتِ پروفایل (پیش‌فرض ۱۰ دقیقه)."""
    return _env_int("PROFILE_GUARD_TTL", 600, 30, 7 * 24 * 3600)


def fetch_timeout() -> float:
    return _env_float("PROFILE_GUARD_FETCH_TIMEOUT", 12.0, 2.0, 60.0)


def rpc_min_interval() -> float:
    return _env_float("PROFILE_GUARD_RPC_MIN_INTERVAL", 0.35, 0.0, 10.0)


def error_retry_seconds() -> int:
    return _env_int("PROFILE_GUARD_ERROR_RETRY", 1800, 60, 86400)


# ---------------------------------------------------------------------------
# کش و محدودکننده‌ها
# ---------------------------------------------------------------------------
_cache: dict = {}
_locks: dict = {}
_retry_after: dict = {}
_semaphore: Optional[asyncio.Semaphore] = None
_last_rpc_ts = 0.0
_flood_until = 0.0


def _loop_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


def _lock(user_id) -> asyncio.Lock:
    key = str(user_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def _throttle() -> None:
    """فاصلهٔ حداقلی بین RPCها + احترام به پنجرهٔ FloodWait."""
    global _last_rpc_ts
    wait = rpc_min_interval() - (time.monotonic() - _last_rpc_ts)
    if wait > 0:
        await asyncio.sleep(wait)
    now = time.monotonic()
    if _flood_until > now:
        await asyncio.sleep(min(_flood_until - now, 30.0))
    _last_rpc_ts = time.monotonic()


def _note_flood(error) -> int:
    global _flood_until
    seconds = getattr(error, "seconds", None) or 5
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 5
    _flood_until = time.monotonic() + min(seconds, 120)
    return seconds


def _log(bot, message: str, *, error: bool = False) -> None:
    logger = getattr(bot, "logger", None)
    try:
        (logger.log_error if error else logger.log_info)(
            f"PROFILE GUARD {message}"
        )
    except Exception:
        pass


def _classify_error(error: Exception) -> str:
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "flood" in name or "floodwait" in text or "too many requests" in text:
        return STATUS_FLOOD
    if "privacy" in name or "no_access" in text or "not enough rights" in text:
        return STATUS_NO_ACCESS
    if "user_id_invalid" in text or "deactivated" in text:
        return STATUS_NO_ACCESS
    return STATUS_ERROR


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# ساخت اسنپ‌شات
# ---------------------------------------------------------------------------
def _empty_snapshot() -> dict:
    return {
        "status": STATUS_EMPTY,
        "about": None,
        "channel_id": None,
        "channel_title": None,
        "channel_username": None,
        "participants": None,
        "access_hash": None,
        "ts": 0.0,
    }


def _peer_channel_id(channel_id: int) -> int:
    """شناسهٔ کانال → شناسهٔ peer سروش/تلگرام (``-100…``)."""
    try:
        return int(f"-100{int(channel_id)}")
    except (TypeError, ValueError):
        return 0


def _channel_from_chats(chats, channel_id):
    """جست‌وجوی کانال در وکتور ``chats`` پاسخ ``getFullUser``."""
    if not channel_id:
        return None
    wanted = _safe_int(channel_id)
    for chat in chats or []:
        if _safe_int(getattr(chat, "id", None)) != wanted:
            continue
        if getattr(chat, "title", None) is None:
            continue
        return chat
    return None


def _apply_channel(snapshot: dict, channel) -> None:
    snapshot["channel_title"] = getattr(channel, "title", None)
    snapshot["channel_username"] = getattr(channel, "username", None)
    snapshot["participants"] = _safe_int(
        getattr(channel, "participants_count", None)
    )
    snapshot["access_hash"] = _safe_int(getattr(channel, "access_hash", None))


async def _resolve_channel(bot, channel_id, access_hash=None):
    """تلاش برای گرفتن آبجکت کانال از شناسه (فقط وقتی در ``chats`` نبود)."""
    client = getattr(bot, "client", None)
    if client is None or not channel_id:
        return None
    try:
        from splusthon.tl.functions.channels import GetChannelsRequest
        from splusthon.tl.types import InputChannel
    except Exception:  # pragma: no cover — کتابخانه نصب نیست
        return None
    try:
        result = await asyncio.wait_for(
            client(GetChannelsRequest([
                InputChannel(channel_id=int(channel_id),
                             access_hash=int(access_hash or 0))
            ])),
            timeout=fetch_timeout(),
        )
    except asyncio.TimeoutError:
        return None
    except Exception as error:
        if _classify_error(error) == STATUS_FLOOD:
            _note_flood(error)
        return None
    for chat in getattr(result, "chats", None) or []:
        if _safe_int(getattr(chat, "id", None)) == _safe_int(channel_id):
            if getattr(chat, "title", None) is not None:
                return chat
    return None


async def _fetch(bot, user) -> Optional[dict]:
    """یک‌بار خواندنِ کامل پروفایل و کانالِ متصل به آن."""
    client = getattr(bot, "client", None)
    if client is None:
        return None
    try:
        from splusthon.tl.functions.users import GetFullUserRequest
    except Exception:  # pragma: no cover
        return None

    request_input = None
    try:
        from splusthon import utils as splus_utils
        request_input = splus_utils.get_input_user(user)
    except Exception:
        request_input = None
    if request_input is None:
        try:
            request_input = await asyncio.wait_for(
                client.get_input_entity(user), timeout=fetch_timeout()
            )
        except asyncio.TimeoutError:
            return None
        except Exception as error:
            _log(bot, f"input_entity failed error={type(error).__name__}",
                 error=True)
            return None

    await _throttle()
    try:
        full = await asyncio.wait_for(
            client(GetFullUserRequest(request_input)), timeout=fetch_timeout()
        )
    except asyncio.TimeoutError:
        _log(bot, "getFullUser timeout", error=True)
        return None
    except Exception as error:
        status = _classify_error(error)
        if status == STATUS_FLOOD:
            _note_flood(error)
        _log(bot, f"getFullUser failed status={status} "
                  f"error={type(error).__name__}", error=True)
        return None

    snapshot = _empty_snapshot()
    full_user = getattr(full, "full_user", None)
    if full_user is None:
        snapshot["status"] = STATUS_NO_ACCESS
        snapshot["ts"] = time.time()
        return snapshot

    about = getattr(full_user, "about", None)
    snapshot["about"] = str(about) if about else None
    channel_id = _safe_int(getattr(full_user, "personal_channel_id", None))
    snapshot["channel_id"] = channel_id
    snapshot["status"] = STATUS_OK

    if channel_id:
        channel = _channel_from_chats(getattr(full, "chats", None), channel_id)
        if channel is not None:
            _apply_channel(snapshot, channel)
        else:
            # کانال در پاسخ نبود: از کش entity یا getChannels
            try:
                from splusthon.tl.types import PeerChannel
                entity = await asyncio.wait_for(
                    client.get_input_entity(PeerChannel(channel_id)),
                    timeout=fetch_timeout(),
                )
                access_hash = _safe_int(getattr(entity, "access_hash", None))
            except Exception:
                access_hash = None
            channel = await _resolve_channel(bot, channel_id, access_hash)
            if channel is not None:
                _apply_channel(snapshot, channel)

    snapshot["ts"] = time.time()
    _log(bot, f"snapshot user={getattr(user, 'id', None)} "
              f"channel={'yes' if channel_id else 'no'} "
              f"title={'yes' if snapshot['channel_title'] else 'no'}")
    return snapshot


async def snapshot(bot, user, *, force: bool = False,
                   max_age: Optional[int] = None) -> Optional[dict]:
    """اسنپ‌شات پروفایل کاربر؛ در صورت تازگی از کش می‌آید.

    ``None`` یعنی همین حالا داده‌ای نداریم (خطا/در صف) و تصمیم‌گیری
    مجاز **نیست**.
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return None
    key = str(user_id)
    now = time.time()
    age_limit = ttl_seconds() if max_age is None else int(max_age)

    entry = _cache.get(key)
    if entry and not force and (now - entry.get("ts", 0)) < age_limit:
        return entry
    if _retry_after.get(key, 0) > now and not force:
        return entry

    async with _lock(user_id):
        entry = _cache.get(key)
        if entry and not force and (time.time() - entry.get("ts", 0)) < age_limit:
            return entry
        async with _loop_semaphore():
            fresh = await _fetch(bot, user)
        if fresh is None:
            _retry_after[key] = time.time() + error_retry_seconds()
            return entry
        _retry_after.pop(key, None)
        _cache[key] = fresh
        return fresh


async def channel_details(bot, channel_id, access_hash=None) -> dict:
    """توضیحات/تعداد اعضای کانال (فقط وقتی لازم شد صدا زده می‌شود).

    خروجی: ``{"about": str|None, "participants": int|None}``
    """
    out = {"about": None, "participants": None}
    client = getattr(bot, "client", None)
    if client is None or not channel_id:
        return out
    try:
        from splusthon.tl.functions.channels import GetFullChannelRequest
        from splusthon.tl.types import InputChannel
    except Exception:  # pragma: no cover
        return out
    await _throttle()
    try:
        result = await asyncio.wait_for(
            client(GetFullChannelRequest(
                InputChannel(channel_id=int(channel_id),
                             access_hash=int(access_hash or 0))
            )),
            timeout=fetch_timeout(),
        )
    except asyncio.TimeoutError:
        return out
    except Exception as error:
        if _classify_error(error) == STATUS_FLOOD:
            _note_flood(error)
        return out
    full_chat = getattr(result, "full_chat", None)
    if full_chat is None:
        return out
    about = getattr(full_chat, "about", None)
    out["about"] = str(about) if about else None
    out["participants"] = _safe_int(
        getattr(full_chat, "participants_count", None)
    )
    return out


def invalidate(user_id) -> None:
    """پاک‌کردن کشِ یک کاربر (برای بررسی اجباری در پیام بعدی)."""
    _cache.pop(str(user_id), None)


def clear() -> None:
    """پاک‌کردن کل کش (تست/دستور ادمین)."""
    _cache.clear()
    _retry_after.clear()
