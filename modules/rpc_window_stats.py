# -*- coding: utf-8 -*-
"""📈 پنجرهٔ ۶۰ ثانیه‌ایِ RPC: میانگینِ ``rpc_await_ms`` و تعدادِ GetDifference.

چرا این ماژول: در لاگ، هر RPC یک خطِ ``RESPONSE … rpc_await_ms=…`` دارد و
``MetricsCollector`` فقط تجمعِ از-زمانِ-بوت را نگه می‌دارد؛ هیچ‌جا «میانگینِ
یک دقیقه» نبود. این ماژول همان یک عدد را می‌دهد **بدونِ کارِ سنگین**:

* در مسیرِ داغ فقط دو جمع و یک مقایسهٔ زمانی انجام می‌شود (O(1)، بدونِ
  allocation، بدونِ قفل، بدونِ I/O و بدونِ تسکِ پس‌زمینه).
* خطِ لاگ فقط هر ۶۰ ثانیه و **تنبل (lazy)** — یعنی اولین RPCِ بعد از تمام‌شدنِ
  پنجره آن را می‌نویسد؛ هیچ تایمر/تسکِ تازه‌ای ساخته نمی‌شود.
* با ``RPC_WINDOW_STATS_ENABLED=0`` کلِ ماژول خاموش است (رفتارِ قبلی، صفر هزینه).

هیچ محدودیت یا معافیتی در governor/flood جابه‌جا نمی‌شود؛ این فقط شمارش است.
"""
from __future__ import annotations

import os
import time

DEFAULT_INTERVAL_S = 60.0

#: وضعیتِ پنجرهٔ جاری (اعداد خیلی ساده؛ GIL برای این نوع افزایش کافی است)
_STATE = {
    "start": time.monotonic(),
    "requests": 0,
    "sum_ms": 0.0,
    "max_ms": 0.0,
    "get_difference": 0,
    "get_channel_difference": 0,
}

#: آخرین پنجرهٔ بسته‌شده (برای تست/گزارش؛ رفتار را عوض نمی‌کند)
LAST = {}


def _env_flag(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


def enabled() -> bool:
    """آیا شمارشِ پنجره‌ای روشن است؟ (پیش‌فرض: روشن)"""
    return _env_flag("RPC_WINDOW_STATS_ENABLED", True)


def interval_seconds() -> float:
    """طولِ پنجره (پیش‌فرض ۶۰ ثانیه؛ تست‌ها کوتاهش می‌کنند)."""
    try:
        value = float(str(os.getenv("RPC_WINDOW_STATS_SECONDS", DEFAULT_INTERVAL_S)).strip())
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    return max(1.0, min(3600.0, value))


def record(request_name, await_ms, logger=None) -> None:
    """ثبتِ یک RPC تمام‌شده؛ اگر پنجره تمام شده باشد یک خطِ خلاصه می‌نویسد.

    نامِ پارامترها عمداً کوتاه و کار سبک است: مسیرِ داغ همین‌جا صدا می‌زند.
    """
    if not enabled():
        return
    state = _STATE
    try:
        await_ms = float(await_ms)
    except (TypeError, ValueError):
        await_ms = 0.0
    state["requests"] += 1
    state["sum_ms"] += await_ms
    if await_ms > state["max_ms"]:
        state["max_ms"] = await_ms
    name = request_name or ""
    if name == "GetDifferenceRequest":
        state["get_difference"] += 1
    elif name == "GetChannelDifferenceRequest":
        state["get_channel_difference"] += 1

    elapsed = time.monotonic() - state["start"]
    if elapsed < interval_seconds():
        return
    _close_window(logger, elapsed)


def _close_window(logger, elapsed) -> None:
    """بستنِ پنجره: نوشتنِ یک خطِ کوتاه و صفرکردنِ شمارنده‌ها."""
    state = _STATE
    requests = state["requests"]
    snapshot = {
        "window_s": round(float(elapsed), 1),
        "requests": requests,
        "avg_rpc_await_ms": (round(state["sum_ms"] / requests, 1)
                             if requests else 0.0),
        "max_rpc_await_ms": round(state["max_ms"], 1),
        "get_difference": state["get_difference"],
        "get_channel_difference": state["get_channel_difference"],
    }
    LAST.clear()
    LAST.update(snapshot)

    # صفرکردن پیش از لاگ تا اگر logger خطا داد، پنجره دوباره شمرده نشود.
    state["start"] = time.monotonic()
    state["requests"] = 0
    state["sum_ms"] = 0.0
    state["max_ms"] = 0.0
    state["get_difference"] = 0
    state["get_channel_difference"] = 0

    if logger is None:
        return
    try:
        logger.log_info(
            "RPC WINDOW "
            f"window_s={snapshot['window_s']} "
            f"requests={snapshot['requests']} "
            f"avg_rpc_await_ms={snapshot['avg_rpc_await_ms']} "
            f"max_rpc_await_ms={snapshot['max_rpc_await_ms']} "
            f"get_difference={snapshot['get_difference']} "
            f"get_channel_difference={snapshot['get_channel_difference']}"
        )
    except Exception:                                    # noqa: BLE001
        pass


def snapshot() -> dict:
    """وضعیتِ پنجرهٔ جاری (فقط خواندن؛ برای تست/گزارش)."""
    state = dict(_STATE)
    state["avg_rpc_await_ms"] = (
        round(state["sum_ms"] / state["requests"], 1) if state["requests"] else 0.0
    )
    state["window_s"] = round(time.monotonic() - state["start"], 1)
    return state


def reset() -> None:
    """صفرکردنِ کامل (فقط تست)."""
    _STATE.update({
        "start": time.monotonic(),
        "requests": 0,
        "sum_ms": 0.0,
        "max_ms": 0.0,
        "get_difference": 0,
        "get_channel_difference": 0,
    })
    LAST.clear()
