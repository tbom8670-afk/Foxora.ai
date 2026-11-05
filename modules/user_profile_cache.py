# -*- coding: utf-8 -*-
"""👤 کشِ پروفایل (GetFullUserRequest) — جلوگیری از خواندنِ تکراریِ بیو.

خطِ لاگِ مرتبط::

    22:54:01.834 RPC GOVERNOR WAIT GetFullUserRequest wait_ms=412.7
    22:54:02.148 BIO LINK CHECK user_id=70874302 status=ok
    22:54:02.170 ERROR PERF WARNING chat_id=24100530 stage=RECEIVE elapsed_ms=1218.02

چرا تکرار می‌شد: بررسیِ «لینک در بیو» برای هر **(گروه، کاربر)** ذخیره می‌شود،
ولی پروفایل یک چیزِ **کاربری** است. یک کاربر در ۵ گروه ⇒ تا ۵ بار
``GetFullUserRequest`` در چند ثانیه؛ به‌علاوهٔ ``refresh_before_punish`` و
بررسیِ پس از ری‌استارت. هر فراخوانی یک RPC روی Governor است و مسیرِ پیام
(``stage=RECEIVE``) را ۱٫۲ ثانیه‌ای می‌کند.

این ماژول همان الگوی ``participant_cache`` را برای پروفایل پیاده می‌کند:

* **TTL = 45s** (بازهٔ معقول ۳۰ تا ۶۰): بیوگرافی کند تغییر می‌کند؛ ۴۵ ثانیه
  هم تکرارِ چندگروهی را حذف می‌کند و هم داده را کهنه نمی‌کند.
* **MIN_INTERVAL = 2s** بین دو RPC واقعی برای همان کاربر.
* **Single-flight:** درخواست‌های هم‌زمانِ همان کاربر به یک Future وصل می‌شوند.
* فقط نتیجهٔ سالم (``ok``/``empty``) کش می‌شود؛ خطا/بدون‌دسترسی نه، تا یک
  شکستِ موقت به «کاربر سالم» یا «پروفایل قفل‌شده» تبدیل نشود.
* سقفِ ۴۰۰۰ کاربر با LRU تا حافظهٔ ترموکس بالا نرود.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

TTL = 45.0
MIN_INTERVAL = 2.0
MAX_ENTRIES = 4000

CACHEABLE_STATUSES = ("ok", "empty")
# FLOOD باید همیشه بالا برود (کول‌داونِ سرویس)، حتی اگر TTL داشته باشیم.
_NEVER_CACHE = ("error", "no_access", "flood_wait")

_CACHE = OrderedDict()   # user_id → (saved_at, (status, about))
_INFLIGHT = {}
_LAST_CALL = {}
STATS = {"calls": 0, "hits": 0, "coalesced": 0, "throttled": 0, "skipped": 0,
         "errors": 0}

_DEFAULT_LOGGER = [None]


def set_default_logger(logger):
    _DEFAULT_LOGGER[0] = logger
    return logger


def _log(logger, message, *, error=False):
    logger = logger if logger is not None else _DEFAULT_LOGGER[0]
    if logger is None:
        return
    try:
        if error:
            logger.log_error(message)
        else:
            logger.log_info(message)
    except Exception:
        pass


def _key(user_id):
    try:
        return str(int(user_id))
    except (TypeError, ValueError):
        return str(user_id)


def get_cached(user_id, ttl=None):
    ttl = TTL if ttl is None else float(ttl)
    key = _key(user_id)
    entry = _CACHE.get(key)
    if not entry:
        return None
    saved_at, value = entry
    if time.monotonic() - saved_at > ttl:
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)
    return value


def _prune():
    while len(_CACHE) > MAX_ENTRIES:
        _CACHE.popitem(last=False)
    if len(_LAST_CALL) > MAX_ENTRIES:
        for stale in list(_LAST_CALL)[:len(_LAST_CALL) - MAX_ENTRIES]:
            _LAST_CALL.pop(stale, None)


def prune_to(limit):
    removed = 0
    while len(_CACHE) > max(0, int(limit)):
        _CACHE.popitem(last=False)
        removed += 1
    return removed


def clear():
    _CACHE.clear()
    _INFLIGHT.clear()
    _LAST_CALL.clear()
    for name in STATS:
        STATS[name] = 0


def describe():
    info = dict(STATS)
    info["entries"] = len(_CACHE)
    info["inflight"] = len(_INFLIGHT)
    return info


async def get_about(user_id, fetch, *, logger=None, ttl=None,
                    min_interval=None):
    """``(status, about)`` را از کش می‌دهد، وگرنه **یک بار** ``fetch()``.

    ``fetch`` باید coroutine function بدون آرگومان باشد و ``(status, text)``
    برگرداند. خروجی همیشه یک تاپل دو‌عضوی است (در بدترین حالت ``error``).
    """
    ttl = TTL if ttl is None else float(ttl)
    min_interval = MIN_INTERVAL if min_interval is None else float(min_interval)
    key = _key(user_id)

    cached = get_cached(user_id, ttl)
    if cached is not None:
        STATS["hits"] += 1
        _log(
            logger,
            f"GETFULLUSER cache hit user={key} status={cached[0]} "
            f"hits={STATS['hits']} rpc_saved=1",
        )
        return cached

    inflight = _INFLIGHT.get(key)
    if inflight is not None and not inflight.done():
        STATS["coalesced"] += 1
        _log(logger, f"GETFULLUSER coalesced user={key} action=await_inflight")
        try:
            return await asyncio.shield(inflight)
        except Exception:
            return "error", None

    last_call = _LAST_CALL.get(key)
    if last_call is not None:
        wait_for = min_interval - (time.monotonic() - last_call)
        if wait_for > 0:
            cached = get_cached(user_id)
            if cached is not None:
                STATS["hits"] += 1
                return cached
            STATS["throttled"] += 1
            _log(
                logger,
                f"GETFULLUSER throttled user={key} wait_ms={int(wait_for * 1000)}",
            )
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
        _INFLIGHT.pop(key, None)
        if not future.done():
            future.set_result(("error", None))
        _log(
            logger,
            f"GETFULLUSER fetch failed user={key} "
            f"error={type(error).__name__}: {error}",
            error=True,
        )
        return "error", None

    status = value[0] if isinstance(value, (tuple, list)) and value else "error"
    if status in CACHEABLE_STATUSES:
        _CACHE[key] = (time.monotonic(), value)
        _CACHE.move_to_end(key)
        _prune()
    else:
        # خطا/بدون‌دسترسی/فلوود کش نمی‌شود (فقط کول‌داونِ کوتاه بالا اعمال است).
        STATS["skipped"] += 1
        _log(logger, f"GETFULLUSER not cached user={key} status={status}")
    _INFLIGHT.pop(key, None)
    if not future.done():
        future.set_result(value)
    return value
