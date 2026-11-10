# -*- coding: utf-8 -*-
"""🌐 پوششِ امنِ فراخوانی‌های SPlusthon برای سه قابلیت جدید.

* تایم‌اوتِ هر RPC (ساعت یکنواخت) تا یک درخواستِ گیرکرده صف را قفل نکند.
* FloodWait: انتظارِ محدود (``FLOOD_MAX_WAIT``) و تلاش مجدد.
* خطای گذرا (timeout/connection/server): تلاش مجدد با backoff.
* خطای دائمی (Forbidden/ChatAdminRequired/…) فوراً بالا می‌رود تا لایهٔ
  بالاتر «مجوز لغو نشد» را صادقانه گزارش کند و در صفِ بازیابی بنشاند.

⚠️ نکتهٔ مهم (رفع باگ ``RuntimeError: cannot reuse already awaited coroutine``):

``call()`` قبلاً اگر یک **coroutine آماده** می‌گرفت (مثل
``call(bot.client(req))``) در تلاشِ دوم همان شیء مصرف‌شده را دوباره
``await`` می‌کرد و با ``RuntimeError`` می‌شکست. حالا:

* اگر coroutine آماده داده شود ⇒ **فقط یک تلاش** (بدون retry و بدون
  backoff)، چون coroutine قابل ساختِ دوباره نیست. این رفتار برای همهٔ
  مصرف‌کننده‌های قبلی (پیام زماندار، گزارش) بدون تغییر است.
* برای retry واقعی، ``call`` باید یک **factory** بگیرد (``lambda`` یا تابعِ
  ``async``) تا هر تلاش یک coroutine تازه بسازد. ``exclusive.py`` همین کار
  را می‌کند (``call(lambda: client(req))``).

هیچ پارامترِ کتابخانه‌ای حدس زده نشده: نسخهٔ نصب‌شده splusthon 1.1.4 است و
همین APIها در ``docs/MANAGE_FEATURES.md`` مستند شده‌اند.
"""
from __future__ import annotations

import asyncio
import inspect

RPC_TIMEOUT = 10.0
MAX_RETRY = 3
FLOOD_MAX_WAIT = 60
BACKOFF_BASE = 0.25

_TRANSIENT_MARKERS = (
    "timeout", "timedout", "connection", "network", "server", "internal",
    "serviceunavailable", "oserror",
)
_PERMANENT_MARKERS = (
    "forbidden", "channelsadminrequired", "channelsadmin", "chatadminrequired",
    "channelschatadminrequired", "userprivacy", "usernotparticipant",
    "peeridinvalid", "badrequest",
)


def flood_seconds(error):
    """ثانیهٔ FloodWait (اگر خطا از این نوع است) — وگرنه ``None``."""
    if error is None:
        return None
    name = type(error).__name__.lower()
    seconds = getattr(error, "seconds", None)
    if "flood" in name:
        try:
            return float(seconds if seconds is not None else 5)
        except (TypeError, ValueError):
            return 5.0
    return None


def is_transient(error) -> bool:
    """آیا خطا موقتی است (شبکه/سرور/تایم‌اوت) و ارزش تلاش دوباره دارد؟"""
    name = type(error).__name__.lower()
    text = str(error).lower()
    if any(marker in name or marker in text for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in name or marker in text for marker in _TRANSIENT_MARKERS)


def is_permanent(error) -> bool:
    """آیا خطا قطعی است (مجوز/دسترسی/درخواستِ نامعتبر)؟"""
    if error is None:
        return False
    name = type(error).__name__.lower()
    text = str(error).lower()
    return any(marker in name or marker in text for marker in _PERMANENT_MARKERS)


async def _sleep(seconds):
    await asyncio.sleep(seconds)


def _resolve_target(func, args, kwargs):
    """``(target, reusable)`` — ``reusable=False`` یعنی coroutine آماده است.

    coroutine آماده قابل ساختِ دوباره نیست؛ بنابراین ``call`` برایش فقط یک
    تلاش انجام می‌دهد (و خطا را بالا می‌دهد) تا هیچ‌وقت ``RuntimeError``
    «cannot reuse already awaited coroutine» رخ ندهد.
    """
    if inspect.iscoroutine(func):
        return func, False
    if inspect.iscoroutinefunction(func):
        return func(*args, **kwargs), True
    result = func(*args, **kwargs)
    if not inspect.isawaitable(result):
        return None, True
    return result, True


async def call(func, *args, logger=None, timeout=RPC_TIMEOUT,
               max_retry=MAX_RETRY, what="RPC", **kwargs):
    """فراخوانی یک coroutine/factory با تایم‌اوت و تلاش مجدد.

    ``func`` می‌تواند یکی از این‌ها باشد:

    * تابعِ ``async`` یا هر callableای که awaitable برمی‌گرداند ⇒ در هر تلاش
      دوباره صدا زده می‌شود (retry واقعی).
    * یک **coroutine آماده** (``call(client(req))``) ⇒ فقط یک تلاش؛ اگر خطای
      گذرا داد دیگر تلاش نمی‌شود (چون همان شیء دوباره قابل await نیست) و خطا
      بالا می‌رود تا لایهٔ بالاتر تصمیم بگیرد.

    برای retry واقعی روی یک درخواستِ SPlusthon بنویس:
    ``await call(lambda: bot.client(req), ...)``.
    """
    target, reusable = _resolve_target(func, args, kwargs)
    if target is None:
        # خروجیِ غیرقابل‌await (مقدار آماده) — همان را برگردان.
        return func(*args, **kwargs) if callable(func) else func

    effective_retry = max_retry if reusable else 1
    attempt = 0
    last_error = None
    while attempt < effective_retry:
        attempt += 1
        try:
            return await asyncio.wait_for(target, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as error:                    # noqa: BLE001
            last_error = error
            seconds = flood_seconds(error)
            if not reusable:
                _log(logger, f"{what} FAILED (single attempt, ready coroutine) "
                             f"error={error!r}")
                raise
            if seconds is not None and seconds <= FLOOD_MAX_WAIT and \
                    attempt < effective_retry:
                _log(logger, f"{what} FLOOD_WAIT wait={seconds}s "
                             f"attempt={attempt}")
                await _sleep(seconds)
                target, _reusable = _resolve_target(func, args, kwargs)
                continue
            if is_transient(error) and attempt < effective_retry:
                delay = BACKOFF_BASE * (2 ** (attempt - 1))
                _log(logger, f"{what} RETRY attempt={attempt} "
                             f"delay={delay}s error={error!r}")
                await _sleep(delay)
                # ⚠️ coroutine تازه برای تلاش بعدی (نه همان شیء مصرف‌شده).
                target, _reusable = _resolve_target(func, args, kwargs)
                continue
            _log(logger, f"{what} FAILED attempt={attempt} error={error!r}")
            raise
    raise last_error


def _log(logger, message):
    if logger is None:
        return
    try:
        logger.log_error(message)
    except Exception:                                # noqa: BLE001
        pass


def log_info(logger, message):
    if logger is None:
        return
    try:
        logger.log_info(message)
    except Exception:                                # noqa: BLE001
        pass
