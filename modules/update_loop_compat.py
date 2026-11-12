# -*- coding: utf-8 -*-
"""🌉 سازگاری با حلقهٔ update کتابخانه برای GetDifference/GetChannelDifference.

**مشکلِ واقعی (لاگ بعد از دیپلوی):**

``splusthon/client/updates.py::_update_loop`` برای دو درخواستِ
``GetDifferenceRequest`` و ``GetChannelDifferenceRequest`` فقط این خطاها را
تحمل می‌کند::

    ServerError, TimedOutError, FloodWaitError, ValueError,
    UnauthorizedError, AuthKeyError, TypeNotFoundError, OperationalError,
    RPCError, OSError

هر چیزِ دیگری به ``except Exception`` انتهایی می‌رسد →
``Fatal error handling updates`` و بعد ``await self.disconnect()``
(یعنی کلِ کلاینت قطع می‌شود).

لایهٔ محافظِ خودمان (``modules/connection_guard.py``) وقتی RPC از مهلت
می‌گذرد ``RpcTimeout`` می‌اندازد که یک ``Exception`` ساده است — نه
``RPCError``، نه ``OSError``، نه ``TimedOutError`` — پس دقیقاً همان
«Fatal error» را می‌سازد. همچنین اگر watchdog داخلی، تسکِ درونیِ RPC را
کنسل کند، ``asyncio.CancelledError`` بالا می‌آید که حلقهٔ کتابخانه آن را در
``except asyncio.CancelledError: pass`` می‌بلعد و **حلقهٔ update بی‌صدا
می‌میرد** در حالی که کلاینت «وصل» است.

این ماژول آن‌ها را به خطای **تحمل‌پذیر** تبدیل می‌کند:

* ``RpcTimeout`` / ``TimeoutError`` / ``asyncio.TimeoutError`` → ``TimedOutError``
  (کد ۵۰۳) که حلقه با آن «SoroushPlus مشکل دارد» را لاگ می‌کند، تفاوت را
  پایان می‌دهد و **دوباره تلاش می‌کند** — بدونِ disconnect.
* کنسلی که از watchdog خودمان آمده (نه خاموشیِ کلی) هم همان ``TimedOutError``.

قاعده‌ها:

* فقط برای همان دو درخواست؛ بقیهٔ درخواست‌ها **دست‌نخورده**.
* ``asyncio.CancelledError`` واقعی (خاموشی/قطعِ عمدی) هرگز بلعیده نمی‌شود.
* با ``UPDATE_LOOP_COMPAT_ENABLED=0`` کلِ این تبدیل خاموش است.
"""
from __future__ import annotations

import asyncio
import os

UPDATE_LOOP_REQUESTS = frozenset({
    "GetDifferenceRequest",
    "GetChannelDifferenceRequest",
})


def _env_flag(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


def enabled() -> bool:
    """آیا تبدیلِ خطا برای حلقهٔ update فعال است؟ (پیش‌فرض: روشن)"""
    return _env_flag("UPDATE_LOOP_COMPAT_ENABLED", True)


def applies_to(request_name) -> bool:
    return bool(request_name) and request_name in UPDATE_LOOP_REQUESTS


def _timed_out_error(request, original):
    """همان ``errors.TimedOutError`` کتابخانه (۵۰۳) با متنِ گویا."""
    try:
        from splusthon import errors as tg_errors
    except Exception:                                    # noqa: BLE001
        tg_errors = None
    if tg_errors is not None:
        try:
            return tg_errors.TimedOutError(
                request, f"Timeout (translated from {type(original).__name__})"
            )
        except Exception:                                # noqa: BLE001
            pass
    return TimeoutError(
        f"translated from {type(original).__name__}: {original}"
    )


def translate(request_name, error, request=None):
    """خطای ناشی از RPC را برای حلقهٔ update «تحمل‌پذیر» می‌کند.

    اگر خطا از قبل تحمل‌پذیر است، **همان** برگردانده می‌شود (بدونِ تغییر).
    """
    if not enabled() or not applies_to(request_name) or error is None:
        return error
    if isinstance(error, asyncio.CancelledError):
        return error                       # کنسلِ واقعی: هرگز بلعیده نشود
    name = type(error).__name__.lower()
    # ⚠️ ترتیب مهم است: در پایتون ۳٫۱۱+ خودِ ``TimeoutError`` زیرکلاسِ
    # ``OSError`` است، پس اول «تایم‌اوت» را تشخیص می‌دهیم و بعد OSError را
    # دست‌نخورده رد می‌کنیم (ConnectionError یعنی شبکه خوابیده، نه سرور).
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or \
            "timeout" in name or "timedout" in name:
        return _timed_out_error(request, error)
    try:
        from splusthon import errors as tg_errors
    except Exception:                                    # noqa: BLE001
        tg_errors = None
    if tg_errors is not None and isinstance(error, tg_errors.RPCError):
        return error                       # کتابخانه خودش می‌داند چه کند
    if isinstance(error, (ValueError, OSError)):
        return error                       # شبکهٔ خوابیده/ورودیِ بد: مثل قبل
    return error                           # خطاهای دیگر دست‌نخورده


def note_and_translate(request_name, error, request=None, logger=None):
    """``translate`` + یک لاگِ تازه (فرمتِ لاگ‌های موجود تغییر نمی‌کند)."""
    translated = translate(request_name, error, request)
    if translated is not error and logger is not None:
        try:
            logger.log_error(
                "UPDATE LOOP TRANSIENT "
                f"request={request_name} "
                f"original={type(error).__name__} "
                f"translated={type(translated).__name__}"
            )
        except Exception:                                # noqa: BLE001
            pass
    return translated


def current_task_is_cancelling() -> bool:
    """آیا تسکِ جاری خودش در حالِ کنسل‌شدن است؟ (خاموشی/قطعِ عمدی)

    در آن حالت نباید کنسل را به تایم‌اوت تبدیل کنیم؛ ابزارِ تشخیصیِ پایتون
    ۳٫۱۱+ است و در نسخه‌های قدیمی‌تر ``False`` برمی‌گردد.
    """
    try:
        task = asyncio.current_task()
        if task is None:
            return False
        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling):
            return bool(cancelling())
    except Exception:                                    # noqa: BLE001
        pass
    return False
