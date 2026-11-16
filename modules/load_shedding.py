"""حالتِ کاهش‌بارِ خودکار (automatic load-shedding).

وقتی اتصالِ سروش کند شود یا صف‌ها سنگین شوند، این ماژول به قابلیت‌های
کم‌اولویت (اسکن‌های دوره‌ای، گزارش‌های غیرضروری، کارهای پس‌زمینه) می‌گوید
موقتاً عقب بمانند تا مدیریتِ حیاتی و پاسخ‌های ضروریِ کاربر — که از همان
اتصال و همان Governor/صف‌ها تغذیه می‌شوند — قربانی نشوند.

پایهٔ تصمیم‌گیری همان زیرساختِ موجود است؛ چیزی از صفر ساخته نشده:

* ``modules.rpc_governor.RpcGovernor`` → تعدادِ RPC‌های در انتظارِ پذیرش
  و سنِ قدیمی‌ترینِ آن‌ها + EMAِ زمانِ نگه‌داشتِ پرمیت (کندیِ واقعیِ اتصال).
* ``modules.runtime_snapshot.collect_sync`` → شمارنده‌های ارزانِ صف‌ها
  (backlogِ سطحِ پایینِ sender، صفِ عادیِ dispatch و ...).

حلقه‌های کم‌اولویت فقط یک سؤال می‌پرسند::

    from modules import load_shedding
    if load_shedding.should_defer(bot, "bio_link.member_scan"):
        ...  # این پاس انجام نمی‌شود؛ تیکِ بعدی دوباره تلاش می‌کند

تضمین‌ها:

* هرگز RPC نمی‌زند و هرگز مسدود نمی‌کند — فقط شمارنده‌های درون‌حافظه‌ای.
* در حالتِ عادی هزینه‌اش تقریباً صفر است (نمونه‌گیری هر چند ثانیه یک‌بار
  و کش می‌شود).
* خروج/ورود به حالتِ کاهش‌بار با hysteresis و چند نمونهٔ متوالی انجام
  می‌شود تا بین دو حالت سوسو نزند.
* با ``BOT_LOAD_SHED=0`` کاملاً خاموش می‌شود (رفتار قبلیِ ربات حفظ است).
* اگر هر سیگنالی در دسترس نبود (تست، نمونهٔ ناتمام) بی‌صدا نادیده گرفته
  می‌شود؛ بدترین حالت «عدم کاهش‌بار» است نه خطا.

کلیدهای env (همگی اختیاری):

* ``BOT_LOAD_SHED``                 «1» پیش‌فرض؛ «0» یعنی خاموش.
* ``BOT_LOAD_SHED_SAMPLE_SECONDS``  حداقل فاصلهٔ نمونه‌گیری (پیش‌فرض 2.0).
* ``BOT_LOAD_SHED_ENTER_SCORE``     آستانهٔ ورود به کاهش‌بار (پیش‌فرض 3.0).
* ``BOT_LOAD_SHED_EXIT_SCORE``      آستانهٔ خروج (پیش‌فرض 1.5؛ کمتر از ورود).
* ``BOT_LOAD_SHED_ENTER_SAMPLES``   نمونه‌های متوالیِ بالای آستانه برای ورود (3).
* ``BOT_LOAD_SHED_EXIT_SAMPLES``    نمونه‌های متوالیِ زیر آستانه برای خروج (6).
* ``BOT_LOAD_SHED_MIN_STATE_SECONDS`` حداقل ماندگاری در هر حالت (20.0).
* ``BOT_LOAD_SHED_STRETCH``         ضریبِ کشیدنِ بازهٔ خواب حین کاهش‌بار (3.0).
* ``BOT_LOAD_SHED_GOVERNOR_WAIT_MS``    سنِ قدیمی‌ترین منتظرِ Governor (800ms).
* ``BOT_LOAD_SHED_GOVERNOR_WAITING``    تعدادِ منتظرانِ Governor (6).
* ``BOT_LOAD_SHED_RPC_PENDING``         backlogِ سطحِ پایینِ RPC (8).
* ``BOT_LOAD_SHED_SENDER_OLDEST_MS``    سنِ قدیمی‌ترین ارسالِ معلق (5000ms).
* ``BOT_LOAD_SHED_DISPATCH_PENDING``    backlogِ صفِ عادیِ dispatch (10).
"""

from __future__ import annotations

import os
import time

__all__ = [
    "LoadShedder",
    "get_shedder",
    "should_defer",
    "stretch",
    "reset_for_tests",
]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


class LoadShedder:
    """تصمیم‌گیرندهٔ «الان بار زیاد است؟» با hysteresis و کشِ نمونه."""

    def __init__(self, bot=None, logger=None) -> None:
        self._bot = bot
        self._logger = logger if logger is not None else getattr(bot, "logger", None)

        self.enabled = _env_bool("BOT_LOAD_SHED", True)
        self.sample_seconds = _env_float("BOT_LOAD_SHED_SAMPLE_SECONDS", 2.0, minimum=0.2)
        self.enter_score = _env_float("BOT_LOAD_SHED_ENTER_SCORE", 3.0, minimum=0.1)
        self.exit_score = _env_float("BOT_LOAD_SHED_EXIT_SCORE", 1.5, minimum=0.0)
        if self.exit_score >= self.enter_score:
            # بدونِ شکافِ ورود/خروج، حالت سوسو می‌زند؛ حداقل شکاف را حفظ کن.
            self.exit_score = self.enter_score * 0.5
        self.enter_samples = _env_int("BOT_LOAD_SHED_ENTER_SAMPLES", 3, minimum=1)
        self.exit_samples = _env_int("BOT_LOAD_SHED_EXIT_SAMPLES", 6, minimum=1)
        self.min_state_seconds = _env_float("BOT_LOAD_SHED_MIN_STATE_SECONDS", 20.0)
        self.stretch_factor = max(
            1.0, _env_float("BOT_LOAD_SHED_STRETCH", 3.0, minimum=1.0)
        )

        # آستانه‌های سیگنال
        self.gov_wait_ms = _env_float("BOT_LOAD_SHED_GOVERNOR_WAIT_MS", 800.0, minimum=1.0)
        self.gov_waiting = _env_int("BOT_LOAD_SHED_GOVERNOR_WAITING", 6, minimum=1)
        self.rpc_pending = _env_int("BOT_LOAD_SHED_RPC_PENDING", 8, minimum=1)
        self.sender_oldest_ms = _env_float("BOT_LOAD_SHED_SENDER_OLDEST_MS", 5000.0, minimum=1.0)
        self.dispatch_pending = _env_int("BOT_LOAD_SHED_DISPATCH_PENDING", 10, minimum=1)

        self._shedding = False
        self._state_since = time.monotonic()
        self._over_streak = 0
        self._under_streak = 0
        self._last_sample_at = 0.0
        self._last_score = 0.0
        self._last_reasons: tuple = ()
        self._stats = {
            "samples": 0,
            "enter": 0,
            "exit": 0,
            "deferred": {},
            "passed": {},
        }

    # ------------------------------------------------------------------
    # نمونه‌گیری
    # ------------------------------------------------------------------
    def _signals(self) -> dict:
        """شمارنده‌های ارزان؛ هر failure به صفر تقلیل می‌یابد."""
        bot = self._bot
        snap = {}
        if bot is not None:
            try:
                from modules import runtime_snapshot

                snap = runtime_snapshot.collect_sync(bot) or {}
            except Exception:  # noqa: BLE001
                snap = {}

        def _num(key, default=0.0):
            try:
                return float(snap.get(key, default) or default)
            except (TypeError, ValueError):
                return float(default)

        # EMAِ زمانِ نگه‌داشتِ پرمیتِ Governor — کندیِ واقعیِ رفت‌وبرگشتِ RPC.
        latency_ema = 0.0
        latency_healthy_ms = 0.0
        try:
            governor = None
            if bot is not None:
                governor = getattr(bot, "rpc_governor", None)
            if governor is None:
                from modules import rpc_governor as _gov_mod

                governor = _gov_mod.active_governor()
            if governor is not None:
                samples = int(getattr(governor, "_latency_samples", 0) or 0)
                if samples >= 8:
                    ema = getattr(governor, "_latency_ema", None)
                    if ema is not None:
                        latency_ema = float(ema)
                        latency_healthy_ms = float(
                            getattr(governor, "adaptive_healthy_ms", 500.0) or 500.0
                        )
        except Exception:  # noqa: BLE001
            latency_ema = 0.0
            latency_healthy_ms = 0.0

        return {
            "governor_wait_ms": _num("rpc_governor_wait_ms"),
            "governor_waiting": _num("governor_waiting"),
            "rpc_pending": _num("rpc_pending"),
            "sender_oldest_age_ms": _num("sender_pending_oldest_age_ms"),
            "dispatch_pending": _num("normal_queue_pending"),
            "rpc_latency_ema_ms": latency_ema,
            "rpc_latency_healthy_ms": latency_healthy_ms,
        }

    def _score(self, signals: dict) -> tuple:
        """امتیازِ بار + دلایل، برای لاگِ گذار شفاف."""
        score = 0.0
        reasons = []

        wait_ms = signals["governor_wait_ms"]
        if wait_ms >= self.gov_wait_ms:
            score += 2.0
            reasons.append(f"governor_wait_ms={wait_ms:.0f}")

        waiting = signals["governor_waiting"]
        if waiting >= self.gov_waiting:
            add = min(1.5, waiting / float(self.gov_waiting))
            score += add
            reasons.append(f"governor_waiting={int(waiting)}")

        rpc_pending = signals["rpc_pending"]
        if rpc_pending >= self.rpc_pending:
            add = min(1.5, rpc_pending / float(self.rpc_pending))
            score += add
            reasons.append(f"rpc_pending={int(rpc_pending)}")

        oldest_ms = signals["sender_oldest_age_ms"]
        if oldest_ms >= self.sender_oldest_ms:
            score += 1.5
            reasons.append(f"sender_oldest_ms={oldest_ms:.0f}")

        dispatch = signals["dispatch_pending"]
        if dispatch >= self.dispatch_pending:
            score += 1.0
            reasons.append(f"dispatch_pending={int(dispatch)}")

        ema = signals["rpc_latency_ema_ms"]
        healthy = signals["rpc_latency_healthy_ms"]
        if ema > 0.0 and healthy > 0.0 and ema >= 2.0 * healthy:
            score += 1.5
            reasons.append(f"rpc_latency_ema_ms={ema:.0f}")

        return score, tuple(reasons)

    def sample(self, *, force: bool = False) -> dict:
        """نمونهٔ تازه (با کش) و به‌روزرسانیِ حالتِ کاهش‌بار."""
        now = time.monotonic()
        if (
            not force
            and self._last_sample_at
            and now - self._last_sample_at < self.sample_seconds
        ):
            return self.status()

        self._last_sample_at = now
        self._stats["samples"] += 1
        signals = self._signals()
        score, reasons = self._score(signals)
        self._last_score = score
        self._last_reasons = reasons
        if not self.enabled:
            # خاموش: همیشه حالتِ عادی؛ ولی نمونه‌ها برای تشخیص می‌مانند.
            self._over_streak = 0
            self._under_streak = 0
            return self.status()

        if score >= self.enter_score:
            self._over_streak += 1
            self._under_streak = 0
        elif score <= self.exit_score:
            self._under_streak += 1
            self._over_streak = 0
        else:
            # ناحیهٔ خاکستری بین دو آستانه: هیچ streakای جمع نمی‌شود.
            self._over_streak = 0
            self._under_streak = 0

        state_age = now - self._state_since
        if not self._shedding:
            if self._over_streak >= self.enter_samples and state_age >= self.min_state_seconds:
                self._shedding = True
                self._state_since = now
                self._over_streak = 0
                self._stats["enter"] += 1
                self._log_state("ENTER")
        else:
            if self._under_streak >= self.exit_samples and state_age >= self.min_state_seconds:
                self._shedding = False
                self._state_since = now
                self._under_streak = 0
                self._stats["exit"] += 1
                self._log_state("EXIT")
        return self.status()

    def _log_state(self, direction: str) -> None:
        reasons = ",".join(self._last_reasons) or "-"
        message = (
            f"LOAD SHED {direction} score={self._last_score:.1f} "
            f"enter>={self.enter_score:g} exit<={self.exit_score:g} reasons={reasons}"
        )
        logger = self._logger
        try:
            if logger is not None:
                log_info = getattr(logger, "log_info", None)
                if callable(log_info):
                    log_info(message)
        except Exception:  # noqa: BLE001
            pass
        # PRINT وقتی کاهش‌بار فعال/غیرفعال شد همیشه دیده شود (هم‌سبکِ لاگ‌های ربات).
        try:
            print(message)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # API عمومی
    # ------------------------------------------------------------------
    @property
    def shedding(self) -> bool:
        self.sample()
        return bool(self.enabled and self._shedding)

    def should_defer(self, feature: str, *, min_interval: float = 0.0) -> bool:
        """آیا «feature» همین حالا باید عقب بیفتد؟

        ``False`` یعنی اجرای عادی؛ در این صورت زمانِ اجرا برای feature ثبت
        می‌شود تا ``min_interval`` (اگر داده شده باشد) بتواند اجراهای پیاپی
        را هم پخش کند. فراخوانی با مقدارِ deferred در آمار می‌نشیند.
        """
        key = str(feature or "unknown")
        shedding = self.shedding
        if shedding:
            self._stats["deferred"][key] = self._stats["deferred"].get(key, 0) + 1
            return True
        if min_interval > 0.0:
            last = self._stats["passed"].get(key)
            # مقدارِ passed به‌صورت (count, monotonic) نگه داشته می‌شود.
            if isinstance(last, tuple) and len(last) == 2:
                if time.monotonic() - float(last[1]) < float(min_interval):
                    self._stats["deferred"][key] = self._stats["deferred"].get(key, 0) + 1
                    return True
        count = 0
        previous = self._stats["passed"].get(key)
        if isinstance(previous, tuple) and len(previous) == 2:
            count = int(previous[0])
        self._stats["passed"][key] = (count + 1, time.monotonic())
        return False

    def stretch(self, feature: str, base_seconds: float) -> float:
        """بازهٔ خوابِ پیشنهادی: حین کاهش‌بار، چند برابرِ بازهٔ عادی."""
        try:
            base = float(base_seconds)
        except (TypeError, ValueError):
            base = 0.0
        base = max(0.0, base)
        if self.shedding:
            return base * self.stretch_factor
        return base

    def status(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "shedding": bool(self.enabled and self._shedding),
            "score": round(float(self._last_score), 2),
            "reasons": list(self._last_reasons),
            "over_streak": int(self._over_streak),
            "under_streak": int(self._under_streak),
            "state_age_seconds": round(time.monotonic() - self._state_since, 1),
            "stats": {
                "samples": int(self._stats["samples"]),
                "enter": int(self._stats["enter"]),
                "exit": int(self._stats["exit"]),
                "deferred": dict(self._stats["deferred"]),
                "passed": {
                    key: (value[0] if isinstance(value, tuple) else value)
                    for key, value in self._stats["passed"].items()
                },
            },
        }


# ----------------------------------------------------------------------
# نمونهٔ مشترکِ ربات (lazy؛ بدونِ دست‌کاریِ main.py)
# ----------------------------------------------------------------------
_FALLBACK: LoadShedder | None = None


def get_shedder(bot=None, logger=None) -> LoadShedder:
    """سازندهٔ تنبل: یک shedder به ازای هر bot (روی خودِ bot کش می‌شود).

    اگر bot نداشته باشیم (ابزارهای مستقل) یک نمونهٔ global می‌سازد.
    """
    global _FALLBACK
    if bot is not None:
        shedder = getattr(bot, "_load_shedder", None)
        if shedder is None:
            shedder = LoadShedder(bot, logger=logger)
            try:
                setattr(bot, "_load_shedder", shedder)
            except Exception:  # noqa: BLE001
                pass
        return shedder
    if _FALLBACK is None:
        _FALLBACK = LoadShedder(None, logger=logger)
    return _FALLBACK


def should_defer(bot, feature: str, *, min_interval: float = 0.0) -> bool:
    """میان‌برِ ``get_shedder(bot).should_defer(...)`` برای حلقه‌ها."""
    try:
        return get_shedder(bot).should_defer(feature, min_interval=min_interval)
    except Exception:  # noqa: BLE001
        # کاهش‌بار هرگز نباید حلقهٔ صاحبش را بشکند؛ بدترین حالت = اجرای عادی.
        return False


def stretch(bot, feature: str, base_seconds: float) -> float:
    """میان‌برِ ``get_shedder(bot).stretch(...)`` برای بازهٔ خوابِ حلقه‌ها."""
    try:
        return get_shedder(bot).stretch(feature, base_seconds)
    except Exception:  # noqa: BLE001
        try:
            return max(0.0, float(base_seconds))
        except (TypeError, ValueError):
            return 0.0


def reset_for_tests() -> None:
    """پاک‌سازیِ نمونهٔ global — فقط برای تست."""
    global _FALLBACK
    _FALLBACK = None
