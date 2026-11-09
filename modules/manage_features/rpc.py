# -*- coding: utf-8 -*-
"""🌐 پوششِ امنِ فراخوانی‌های SPlusthon برای سه قابلیت جدید.

* تایم‌اوتِ هر RPC (ساعت یکنواخت) تا یک درخواستِ گیرکرده صف را قفل نکند.
* FloodWait: انتظارِ محدود (``FLOOD_MAX_WAIT``) و تلاش مجدد.
* خطای گذرا (timeout/connection/server): تلاش مجدد با backoff.
* خطای دائمی (Forbidden/ChatAdminRequired/…) فوراً بالا می‌رود تا لایهٔ
  بالاتر «مجوز لغو نشد» را صادقانه گزارش کند و در صفِ بازیابی بنشاند.

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
    name = type(error).__name__.lower()
    text = str(error).lower()
    if any(marker in name or marker in text for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in name or marker in text for marker in _TRANSIENT_MARKERS)


async def _sleep(seconds):
    await asyncio.sleep(seconds)


async def call(func, *args, logger=None, timeout=RPC_TIMEOUT,
               max_retry=MAX_RETRY, what="RPC", **kwargs):
    """فراخوانی یک coroutine یا callable با تایم‌اوت و تلاش مجدد."""
    attempt = 0
    last_error = None
    while attempt < max_retry:
        attempt += 1
        try:
            if inspect.iscoroutinefunction(func):
                target = func(*args, **kwargs)
            elif inspect.iscoroutine(func):
                target = func
            else:
                result = func(*args, **kwargs)
                if not inspect.isawaitable(result):
                    return result
                target = result
            return await asyncio.wait_for(target, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as error:                    # noqa: BLE001
            last_error = error
            seconds = flood_seconds(error)
            if seconds is not None and seconds <= FLOOD_MAX_WAIT and \
                    attempt < max_retry:
                _log(logger, f"{what} FLOOD_WAIT wait={seconds}s "
                             f"attempt={attempt}")
                await _sleep(seconds)
                continue
            if is_transient(error) and attempt < max_retry:
                delay = BACKOFF_BASE * (2 ** (attempt - 1))
                _log(logger, f"{what} RETRY attempt={attempt} "
                             f"delay={delay}s error={error!r}")
                await _sleep(delay)
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
