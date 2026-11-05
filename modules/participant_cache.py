# -*- coding: utf-8 -*-
"""🧊 کشِ مشترکِ «فهرست اعضا» — جلوگیری از طوفانِ GetParticipantsRequest.

مشکلِ مشاهده‌شده در لاگ: برای یک گروه، ``GetParticipantsRequest`` حدود ۱۰ بار
در ۱۴ ثانیه صدا زده می‌شد. هر تماس یک RPC کامل روی Governor است، پس هم
bucket=delete را پشتِ صف می‌اندازد و هم روی ترموکس/موبایل فشار می‌آورد.

این ماژول سه کار می‌کند و هیچ RPCای از خودش نمی‌زند:

1. **کشِ TTL:** نتیجهٔ هر پرس‌وجو برای ``ttl`` ثانیه (پیش‌فرض ۶۰) نگه داشته
   می‌شود؛ تماسِ بعدی در این بازه صفر RPC است.
2. **Single-flight:** اگر دو مسیر هم‌زمان همان کلید را بخواهند، فقط **یک**
   RPC زده می‌شود و بقیه منتظر همان Future می‌مانند (نه ۱۰ درخواست موازی).
3. **حداقلِ فاصلهٔ RPC بین دو شکستِ کش:** اگر نتیجه «خالی/خطا» بود و کسی
   دوباره پرسید، حداقل ``MIN_INTERVAL`` ثانیه (پیش‌فرض ۲ ثانیه) صبر می‌شود؛
   یعنی حتی در حالت خرابی هم polling به حداکثر یک RPC در ۲ ثانیه محدود است.

مقدارها و دلیلشان:

* ``TTL = 60s``: نقش ادمین/مالک در گروه‌های سروش به‌ندرت (دقیقه‌ای) عوض
  می‌شود؛ ۶۰ ثانیه تأخیر در دیده‌شدنِ تغییرِ نقش بی‌خطر است و در عوض حجم
  RPC را برای هر گروه از «هر پیام/هر دستور» به «حداکثر یک بار در دقیقه»
  می‌رساند.
* ``MIN_INTERVAL = 2s``: کرانِ بالا برای حالتِ خطا/کش‌نشده؛ کمتر از آن سودی
  ندارد و بیشتر از ۲ ثانیه باعث می‌شود پاسخِ دستورِ کاربر دیر شود.
* سقفِ تعداد کلید = ۲۰۰۰ با حذفِ قدیمی‌ترین (LRU ساده) تا حافظهٔ ترموکس
  هیچ‌وقت با رشدِ بی‌نهایت بالا نرود.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

DEFAULT_TTL = 60.0
MIN_INTERVAL = 2.0
MAX_ENTRIES = 2000

# key → (saved_at, value)
_CACHE = OrderedDict()
# key → asyncio.Future (درخواستِ در جریان)
_INFLIGHT = {}
# key → monotonic timestamp آخرین تلاش
_LAST_CALL = {}
# key → آخرین خطا (برای گزارش/تست)
_LAST_ERROR = {}

STATS = {"calls": 0, "hits": 0, "coalesced": 0, "throttled": 0, "errors": 0}


def _key(chat_id, kind, extra=""):
    try:
        from modules.group_id import normalize_group_id
        chat_key = str(normalize_group_id(chat_id))
    except Exception:
        chat_key = str(chat_id)
    return f"{chat_key}:{kind}:{extra}"


def _prune():
    while len(_CACHE) > MAX_ENTRIES:
        _CACHE.popitem(last=False)
    if len(_LAST_CALL) > MAX_ENTRIES:
        for stale in list(_LAST_CALL)[:len(_LAST_CALL) - MAX_ENTRIES]:
            _LAST_CALL.pop(stale, None)
    if len(_LAST_ERROR) > MAX_ENTRIES:
        for stale in list(_LAST_ERROR)[:len(_LAST_ERROR) - MAX_ENTRIES]:
            _LAST_ERROR.pop(stale, None)


def get_cached(chat_id, kind, extra=""):
    """فقط خواندنِ کش؛ ``None`` یعنی موجود نیست/منقضی شده."""
    key = _key(chat_id, kind, extra)
    entry = _CACHE.get(key)
    if not entry:
        return None
    saved_at, value = entry
    if time.monotonic() - saved_at > DEFAULT_TTL:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return value


def put(chat_id, kind, value, extra="", ttl=None):
    key = _key(chat_id, kind, extra)
    _CACHE[key] = (time.monotonic(), value)
    _CACHE.move_to_end(key)
    _prune()
    return value


def invalidate(chat_id, kind=None, extra=None):
    """پاک‌کردن کش یک گروه (مثلاً بعد از تغییر ادمین)."""
    if kind is None:
        prefix_ids = {_key(chat_id, "owner"), _key(chat_id, "admins")}
        for key in list(_CACHE):
            if key in prefix_ids:
                _CACHE.pop(key, None)
        return
    if extra is None:
        for key in list(_CACHE):
            if key.startswith(_key(chat_id, kind).rsplit(":", 1)[0]):
                _CACHE.pop(key, None)
        return
    _CACHE.pop(_key(chat_id, kind, extra), None)


def prune_to(limit):
    """کوچک‌کردن کش تا ``limit`` کلید (برای نگهبان حافظه)."""
    removed = 0
    while len(_CACHE) > max(0, int(limit)):
        _CACHE.popitem(last=False)
        removed += 1
    cutoff = time.monotonic() - 300.0
    for key, stamp in list(_LAST_CALL.items()):
        if stamp < cutoff:
            _LAST_CALL.pop(key, None)
    return removed


def clear():
    _CACHE.clear()
    _INFLIGHT.clear()
    _LAST_CALL.clear()
    _LAST_ERROR.clear()
    for name in STATS:
        STATS[name] = 0


async def get_or_fetch(chat_id, kind, fetch, *, extra="", ttl=None,
                       min_interval=None, logger=None, refresh=False):
    """نتیجه را از کش می‌دهد، وگرنه **یک بار** ``fetch()`` را اجرا می‌کند.

    ``fetch`` باید یک coroutine function بدون آرگومان باشد. اگر نتیجهٔ
    فراخوانی ``None`` یا استثنا بود، کش نمی‌شود (جز خطا که فقط برای
    ``MIN_INTERVAL`` نگه داشته می‌شود) تا خطای موقت به کشِ دائمی تبدیل نشود.
    """
    ttl = DEFAULT_TTL if ttl is None else float(ttl)
    min_interval = MIN_INTERVAL if min_interval is None else float(min_interval)
    key = _key(chat_id, kind, extra)

    if not refresh:
        entry = _CACHE.get(key)
        if entry:
            saved_at, value = entry
            if time.monotonic() - saved_at <= ttl:
                _CACHE.move_to_end(key)
                STATS["hits"] += 1
                return value
            _CACHE.pop(key, None)

    inflight = _INFLIGHT.get(key)
    if inflight is not None and not inflight.done():
        STATS["coalesced"] += 1
        try:
            return await asyncio.shield(inflight)
        except Exception:
            return None

    last_call = _LAST_CALL.get(key)
    if last_call is not None:
        wait_for = min_interval - (time.monotonic() - last_call)
        if wait_for > 0:
            cached = get_cached(chat_id, kind, extra)
            if cached is not None:
                STATS["hits"] += 1
                return cached
            STATS["throttled"] += 1
            if logger is not None:
                try:
                    logger.log_info(
                        f"PARTICIPANT CACHE THROTTLED chat_id={chat_id} "
                        f"kind={kind} wait_ms={int(wait_for * 1000)}"
                    )
                except Exception:
                    pass
            await asyncio.sleep(wait_for)

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _INFLIGHT[key] = future
    _LAST_CALL[key] = time.monotonic()
    STATS["calls"] += 1
    try:
        value = await fetch()
    except asyncio.CancelledError:
        _INFLIGHT.pop(key, None)
        if not future.done():
            future.cancel()
        raise
    except Exception as error:
        STATS["errors"] += 1
        _LAST_ERROR[key] = (time.monotonic(), f"{type(error).__name__}: {error}")
        _INFLIGHT.pop(key, None)
        if not future.done():
            future.set_result(None)
        if logger is not None:
            try:
                logger.log_error(
                    f"PARTICIPANT FETCH FAILED chat_id={chat_id} "
                    f"kind={kind} error={type(error).__name__}: {error}"
                )
            except Exception:
                pass
        return None
    else:
        _LAST_ERROR.pop(key, None)
        if value is not None:
            _CACHE[key] = (time.monotonic(), value)
            _CACHE.move_to_end(key)
            _prune()
        _INFLIGHT.pop(key, None)
        if not future.done():
            future.set_result(value)
        return value


def describe(chat_id=None) -> dict:
    """گزارش کوتاه وضعیت کش (برای پینگ/تشخیص)."""
    info = dict(STATS)
    info["entries"] = len(_CACHE)
    info["inflight"] = len(_INFLIGHT)
    if chat_id is not None:
        info["owner"] = get_cached(chat_id, "owner")
    return info
