# -*- coding: utf-8 -*-
"""🧠 نگهبان حافظه — جلوگیری از بسته‌شدنِ خودکار ترموکس (OOM kill).

گزارش کارفرما: «اینقدر خطا و کندی می‌گیره که خود به خود ترموکس بسته میشه».

روی ترموکس/اندروید، سیستم‌عامل پروسه‌ای را که RSS بالا برود بی‌رحمانه
می‌کشد؛ هیچ tracebackای هم نمی‌ماند. این ماژول سه کار می‌کند:

1. **پایشِ RSS** از ``/proc/self/status`` (بدون وابستگی خارجی) هر
   ``interval`` ثانیه.
2. **هرسِ کش‌ها** وقتی RSS از آستانهٔ نرم گذشت؛ هر کش با ``prune_to`` خودش
   تا سقفِ مشخصی کوچک می‌شود (نه اینکه کامل پاک شود، تا رفتار کند نشود).
3. **حالتِ اضطراری**: اگر RSS از آستانهٔ سخت بگذرد، همهٔ کش‌های ثبت‌شده
   آزاد می‌شوند و ``gc.collect()`` اجرا می‌شود؛ همچنین اگر صفِ حذف/ارسال
   خیلی بزرگ باشد گزارش می‌دهد.

مقدارها و دلیلشان (قابل تغییر با env):

* ``BOT_MEMORY_SOFT_MB=220`` — از اینجا به بعد «هرسِ زودهنگام».
* ``BOT_MEMORY_HARD_MB=320`` — اندروید معمولاً در ۳۵۰–۴۰۰ مگ پروسهٔ پایتون
  را می‌کشد؛ ۳۲۰ حاشیهٔ امن است.
* ``BOT_MEMORY_GUARD_INTERVAL_S=60`` — هر دقیقه یک بار خواندنِ یک فایل کوچک
  هزینهٔ ناچیزی دارد و برای رشدِ کش‌ها کافی است.
"""
from __future__ import annotations

import gc
import os
import time
from pathlib import Path

_REGISTRY = {}
_LAST = {"rss_mb": None, "at": 0.0, "pruned": 0, "emergency": 0}
STATS = {"runs": 0, "soft_prunes": 0, "emergency_prunes": 0, "peak_rss_mb": 0.0}


def _env_float(name, default):
    try:
        raw = os.environ.get(name)
        return float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_bool(name, default=True):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "بله"}


def enabled() -> bool:
    return _env_bool("BOT_MEMORY_GUARD_ENABLED", True)


def soft_limit_mb() -> float:
    return _env_float("BOT_MEMORY_SOFT_MB", 220.0)


def hard_limit_mb() -> float:
    return _env_float("BOT_MEMORY_HARD_MB", 320.0)


def interval_seconds() -> float:
    return max(5.0, _env_float("BOT_MEMORY_GUARD_INTERVAL_S", 60.0))


def rss_mb() -> float:
    """RSS جاری پروسه به مگابایت (بدون psutil)."""
    try:
        text = Path("/proc/self/status").read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return round(int(parts[1]) / 1024.0, 1)
    except Exception:
        pass
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # لینوکس: کیلوبایت، مک: بایت
        return round(peak / 1024.0, 1)
    except Exception:
        return 0.0


def register(name, prune, size=None):
    """ثبتِ یک کش: ``prune(kind)`` که kind یکی از «soft»/«hard» است."""
    _REGISTRY[str(name)] = {"prune": prune, "size": size}
    return name


def unregister(name):
    _REGISTRY.pop(name, None)


def registered():
    return sorted(_REGISTRY)


def _safe_size(entry):
    size = entry.get("size")
    if not callable(size):
        return None
    try:
        return int(size())
    except Exception:
        return None


def prune(kind="soft", logger=None):
    """هرسِ کش‌های ثبت‌شده. خروجی: تعداد کش‌های هرس‌شده."""
    pruned = 0
    for name, entry in list(_REGISTRY.items()):
        prune_fn = entry.get("prune")
        if not callable(prune_fn):
            continue
        try:
            result = prune_fn(kind)
        except Exception as error:
            if logger is not None:
                try:
                    logger.log_error(
                        f"MEMORY PRUNE FAILED cache={name} "
                        f"error={type(error).__name__}: {error}"
                    )
                except Exception:
                    pass
            continue
        if result:
            pruned += 1
    if kind == "hard":
        try:
            gc.collect()
        except Exception:
            pass
    return pruned


def check(logger=None, bot=None):
    """یک دورِ پایش؛ خروجی دیکشنری وضعیت (برای تست/گزارش)."""
    current = rss_mb()
    STATS["runs"] += 1
    if current > STATS["peak_rss_mb"]:
        STATS["peak_rss_mb"] = current
    _LAST["rss_mb"] = current
    _LAST["at"] = time.time()

    kind = None
    if current >= hard_limit_mb():
        kind = "hard"
        STATS["emergency_prunes"] += 1
    elif current >= soft_limit_mb():
        kind = "soft"
        STATS["soft_prunes"] += 1

    pruned = prune(kind, logger) if kind else 0
    _LAST["pruned"] = pruned

    if logger is not None:
        try:
            if kind:
                logger.log_info(
                    f"MEMORY GUARD {kind.upper()} rss_mb={current} "
                    f"soft={soft_limit_mb():g} hard={hard_limit_mb():g} "
                    f"caches_pruned={pruned} caches={len(_REGISTRY)}"
                )
            else:
                logger.log_info(
                    f"MEMORY GUARD OK rss_mb={current} "
                    f"soft={soft_limit_mb():g} caches={len(_REGISTRY)}"
                )
        except Exception:
            pass
    return {"rss_mb": current, "kind": kind, "pruned": pruned,
            "caches": len(_REGISTRY)}


async def run(bot, logger=None, interval=None):
    """حلقهٔ پس‌زمینه؛ تا پایانِ عمر پروسه ادامه می‌دهد."""
    import asyncio

    delay = interval_seconds() if interval is None else max(5.0, float(interval))
    logger = logger or getattr(bot, "logger", None)
    if not enabled():
        try:
            if logger is not None:
                logger.log_info("MEMORY GUARD DISABLED by env")
        except Exception:
            pass
        return
    while True:
        await asyncio.sleep(delay)
        try:
            check(logger, bot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            try:
                if logger is not None:
                    logger.log_error(
                        "MEMORY GUARD FAILED "
                        f"error={type(error).__name__}: {error}"
                    )
            except Exception:
                pass


def describe() -> dict:
    return {
        "enabled": enabled(),
        "rss_mb": rss_mb(),
        "soft_mb": soft_limit_mb(),
        "hard_mb": hard_limit_mb(),
        "interval_s": interval_seconds(),
        "caches": list(_REGISTRY),
        "stats": dict(STATS),
    }
