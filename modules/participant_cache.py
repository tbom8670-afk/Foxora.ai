# -*- coding: utf-8 -*-
"""🧊 کشِ مشترکِ «فهرست اعضا» — جلوگیری از طوفانِ GetParticipantsRequest.

مشکلِ مشاهده‌شده در لاگِ کلاینت (22:53:58 تا 22:54:09): برای **یک** chat_id
``GetParticipantsRequest`` حدود ۱۳ بار پشتِ‌سرِهم با فاصلهٔ ~۱۰۰ms بین پاسخ و
درخواستِ بعدی زده شد (همه با ``bucket=heavy``). هر تماس یک RPC کامل روی
Governor است، پس هم باکت heavy را اشباع می‌کند و هم ``delete``/``notice``/
``send`` پشتش در صف می‌مانند (``GOVERNOR HOLD SLOW bucket=notice`,
``bucket=delete`` هر دو ~۵۰۳ms).

این ماژول سه کار می‌کند و هیچ RPCای از خودش نمی‌زند:

1. **کشِ TTL:** نتیجهٔ هر پرس‌وجو برای ``ttl`` ثانیه (پیش‌فرض ۸) نگه داشته
   می‌شود؛ تماسِ بعدی در این بازه صفر RPC است.
2. **Single-flight (coalescing):** اگر چند مسیر هم‌زمان همان کلید را بخواهند،
   فقط **یک** RPC زده می‌شود و بقیه منتظر همان Future می‌مانند.
3. **حداقلِ فاصلهٔ RPC:** بین دو تلاشِ واقعی برای همان کلید دست‌کم
   ``MIN_INTERVAL`` ثانیه (پیش‌فرض ۱٫۵) فاصله می‌افتد؛ یعنی حتی در حالت
   کش‌نشده/خطا هم polling به یک RPC در ۱٫۵ ثانیه محدود است.

مقدارها و دلیلشان (طبقِ درخواستِ راند ۱۲):

* ``TTL = 8s`` در بازهٔ پیشنهادی ۵ تا ۱۰ ثانیه: تغییرِ فهرستِ اعضا/نقش‌ها
  نادر است و سروش خودش update می‌فرستد؛ TTL کوتاه‌تر (مثلاً ۲s) فشار را
  کم نمی‌کند و بلندتر از ۱۰s ممکن است دادهٔ کهنه بدهد. ۸ ثانیه وسطِ این
  بازه است تا هم طوفانِ ۱۳ تایی به ۱ RPC تبدیل شود و هم داده کهنه نشود.
* ``MIN_INTERVAL = 1.5s`` در بازهٔ پیشنهادی ۱ تا ۲ ثانیه: کفِ فاصلهٔ دو RPC
  واقعی برای همان **chat_id** (نه فقط همان کلید)؛ ۱ ثانیه سست است و ۲ ثانیه
  پاسخِ دستور را دیر می‌کند. تنها استثنا پیمایشِ صفحه‌به‌صفحهٔ force-join است
  که ``min_interval=0`` می‌فرستد: آن یک «اسکنِ واحد» با تأخیرِ داخلی و
  بودجهٔ زمانی است و اعمالِ ۱٫۵ ثانیه بین صفحه‌ها آن را از بودجهٔ ۲۰ ثانیه
  بیرون می‌انداخت (یعنی خراب‌کردنِ یک مسیرِ سالم).
* سقفِ تعداد کلید = ۲۰۰۰ با حذفِ قدیمی‌ترین (LRU ساده) تا حافظهٔ ترموکس
  هیچ‌وقت با رشدِ بی‌نهایت بالا نرود.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

DEFAULT_TTL = 8.0
MIN_INTERVAL = 1.5
MAX_ENTRIES = 2000

# فاصلهٔ حداقلیِ لاگِ «cache hit» برای هر کلید (ضد طوفانِ لاگ).
_HIT_LOG_INTERVAL = 1.0
_LOG_TS = {}

# لاگرِ پیش‌فرض (core یک بار ثبت می‌کند تا مسیرهایی که logger ندارند هم لاگ بدهند)
_DEFAULT_LOGGER = [None]


def set_default_logger(logger):
    """ثبتِ لاگرِ پروسه؛ مسیرهایی مثل force_join_api لاگر ندارند."""
    _DEFAULT_LOGGER[0] = logger
    return logger


def _logger(logger=None):
    return logger if logger is not None else _DEFAULT_LOGGER[0]


def _log(logger, message, *, error=False):
    logger = _logger(logger)
    if logger is None:
        return
    try:
        if error:
            logger.log_error(message)
        else:
            logger.log_info(message)
    except Exception:
        pass

# key → (saved_at, value)
_CACHE = OrderedDict()
# key → asyncio.Future (درخواستِ در جریان)
_INFLIGHT = {}
# chat_key → monotonic timestamp آخرین RPC واقعی (cooldownِ هر chat)
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


def _chat_key(chat_id):
    """کلیدِ «گروه» برای cooldown — مستقل از kind/offset."""
    try:
        from modules.group_id import normalize_group_id
        return str(normalize_group_id(chat_id))
    except Exception:
        return str(chat_id)


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
    _LOG_TS.clear()
    for name in STATS:
        STATS[name] = 0


def _log_hit(chat_id, kind, value, logger, extra="", label="GetParticipants"):
    """لاگِ «cache hit» با محدودیتِ ۱ بار در ثانیه برای هر کلید (ضد طوفان)."""
    key = _key(chat_id, kind, extra)
    now = time.monotonic()
    if now - _LOG_TS.get(key, 0.0) < _HIT_LOG_INTERVAL:
        return
    _LOG_TS[key] = now
    try:
        size = len(value) if isinstance(value, (list, tuple, dict, set)) else 1
    except Exception:
        size = 1
    _log(
        logger,
        f"{label} cache hit chat={chat_id} kind={kind} key={extra or '-'} "
        f"size={size} hits={STATS['hits']} rpc_saved=1",
    )


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
                _log_hit(chat_id, kind, value, logger, extra)
                return value
            _CACHE.pop(key, None)

    inflight = _INFLIGHT.get(key)
    if inflight is not None and not inflight.done():
        STATS["coalesced"] += 1
        _log(
            logger,
            f"GetParticipants coalesced chat={chat_id} kind={kind} "
            f"key={extra or '-'} action=await_inflight",
        )
        try:
            return await asyncio.shield(inflight)
        except Exception:
            return None

    chat_key = _chat_key(chat_id)
    last_call = _LAST_CALL.get(chat_key) if min_interval > 0 else None
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
    if min_interval > 0:
        _LAST_CALL[chat_key] = time.monotonic()
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
