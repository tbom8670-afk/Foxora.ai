# -*- coding: utf-8 -*-
"""🌖 «خاموشی گروه» — زمان‌بند مرکزیِ پایدار برای قفل خودکار گروه در زمان مشخص.

ویژگی‌های معماری:

1. **دسترسی منحصراً برای مالک اصلی ثبت‌شدهٔ ربات** (``owner_check.is_global_owner``)
   بدون هرگونه شناسه یا نام کاربری هاردکدشده. مالک گروه، ادمین‌های گروه و
   ادمین‌های ثبت‌شدهٔ ربات مجاز نیستند مگر آنکه همان مالک اصلی ربات باشند.
2. **زمان‌بند مرکزیِ پایدار (Single Central Scheduler)** مستقل از هندلر پیام‌ها:
   * بدون ساخت تسک ``sleep`` جداگانه به ازای هر گروه و بدون تسک‌های رهاشده.
   * محاسبهٔ زمان بر اساس ساعت واقعی (Unix ``timestamp``) و مقایسه با
     ``shutdown_ts`` ذخیره‌شده در دیتابیس SQLite.
   * اجرای عملیات هر گروه در صف مستقلِ خودش (``PerGroupQueue("group_shutdown")``)
     تا گروه‌های هم‌زمان روی یکدیگر اثر نگذارند.
3. **اتصال مستقیم به سیستم قفل موجود ربات (``GroupActions``)**:
   * ابتدا وضعیت واقعی ``default_banned_rights`` از سرور خوانده می‌شود؛ اگر گروه
     از قبل قفل باشد (``text_is_banned == True``)، هیچ عملیات تکراریِ قفل انجام
     نمی‌شود و وضعیت ``already_locked`` ثبت می‌گردد.
   * اگر گروه باز باشد، مستقیماً ``GroupActions.lock_group`` فراخوانی می‌شود که
     تمام دسترسی‌های غیرمتنیِ قبلی گروه را حفظ و اسنپ‌شات بازگشتی را ذخیره می‌کند
     و اعمال واقعی قفل را از سرور راستی‌آزمایی می‌نماید (هرگز با حذف پیام جایگزین
     نمی‌شود).
4. **پایداری و بازیابی کامل پس از ری‌استارت (``recover``)**:
   * تمامی زمان‌بندی‌ها در جدول SQLite ``group_shutdown_schedules`` ذخیره می‌شوند.
   * پس از راه‌اندازی مجدد ربات، زمان‌بندی‌های فعال بازیابی شده و اگر زمان خاموشی
     در حین خاموش بودن ربات گذشته باشد، بلافاصله قفل اجرا می‌شود.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from modules import admin_tools, owner_check, runtime_db
from modules.group_actions import GroupActions, text_is_banned
from modules.group_id import CHANNEL_ID_OFFSET, normalize_group_id
from modules.manage_features import peers, texts
from modules.manage_features.queue import PerGroupQueue, QueueFull, reset_queue
from modules.manage_features.rpc import flood_seconds, is_transient

# ---------------------------------------------------------------------------
#  ثابت‌ها و وضعیت‌ها
# ---------------------------------------------------------------------------
STATUS_SCHEDULED = "scheduled"
STATUS_EXECUTING = "executing"
STATUS_RETRY_PENDING = "retry_pending"
STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = frozenset({
    STATUS_SCHEDULED,
    STATUS_EXECUTING,
    STATUS_RETRY_PENDING,
})

LOCK_PENDING = "pending"
LOCK_LOCKED = "locked"
LOCK_ALREADY_LOCKED = "already_locked"
LOCK_FAILED = "failed"

#: کمینه و بیشینهٔ منطقی مدت زمان خاموشی (۱ ثانیه تا ۳۰ روز).
MIN_DURATION_SECONDS = 1
MAX_DURATION_SECONDS = 30 * 24 * 3600  # 2,592,000 ثانیه (۳۰ شبانه‌روز)

#: بیشینهٔ بازهٔ بیدار شدن حلقهٔ زمان‌بند مرکزی (ثانیه) برای دقت زیرثانیه و مقاومت در برابر تغییر ساعت.
MAX_SCHEDULER_TICK = 0.5
#: سقف تلاش مجدد برای خطاهای گذرای شبکه/FloodWait.
MAX_EXEC_RETRIES = 3
#: سقف زمانی هر فراخوانی RPC قفل گروه.
LOCK_RPC_TIMEOUT = 25.0

#: صف اختصاصی اجرای خاموشی به تفکیک هر گروه.
QUEUE = PerGroupQueue(
    "group_shutdown",
    max_pending_per_key=8,
    max_pending_total=256,
    max_keys=256,
    task_timeout=60.0,
)


# ---------------------------------------------------------------------------
#  استثناهای دامنه
# ---------------------------------------------------------------------------
class GroupShutdownError(Exception):
    """پایهٔ خطاهای قابلیت خاموشی گروه."""


class UnauthorizedOwnerError(GroupShutdownError):
    """فقط مالک ثبت‌شدهٔ اصلی ربات مجاز به اجرای این دستور است."""


class InvalidDurationError(GroupShutdownError):
    """ورودی مدت زمان نامعتبر، صفر یا منفی است."""

    def __init__(self, reason: str = "invalid_duration"):
        super().__init__(reason)
        self.reason = reason


class DurationTooLongError(InvalidDurationError):
    """مدت زمان واردشده بیش از سقف مجاز منطقی است."""

    def __init__(self, seconds: int, maximum: int = MAX_DURATION_SECONDS):
        super().__init__("duration_too_long")
        self.seconds = int(seconds)
        self.maximum = int(maximum)


# ---------------------------------------------------------------------------
#  نرمال‌سازی متن و تجزیهٔ دقیق انواع فرمت‌های زمان
# ---------------------------------------------------------------------------
_DIGIT_MAP = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩０１２３４５６７８９",
    "012345678901234567890123456789",
)


def normalize_text(text: str) -> str:
    """نرمال‌سازی متن دستور/زمان (ارقام فارسی/عربی، نیم‌فاصله، ی/ک و کاراکترهای جهت‌دهی)."""
    value = str(text or "")
    for ch in ("\u200c", "\u200f", "\u200e", "\ufeff", "\u202a", "\u202b", "\u202c"):
        value = value.replace(ch, " ")
    value = (
        value.replace("ي", "ی")
        .replace("ك", "ک")
        .replace("：", ":")
        .translate(_DIGIT_MAP)
    )
    return " ".join(value.split())


_COLON_HMS_RE = re.compile(r"^([+-]?\d+)\s*:\s*([+-]?\d+)\s*:\s*([+-]?\d+)$")
_COLON_HM_RE = re.compile(r"^([+-]?\d+)\s*:\s*([+-]?\d+)$")
_UNIT_TOKEN_RE = re.compile(
    r"([+-]?\d+(?:[./]\d+)?)\s*(ساعته|ساعت|دقیقه|دیقه|دقیقہ|مین|ثانیه|ثانیہ)"
)


def _unit_kind(unit_word: str) -> str:
    if unit_word in ("ساعت", "ساعته"):
        return "h"
    if unit_word in ("دقیقه", "دیقه", "دقیقہ", "مین"):
        return "m"
    return "s"


def parse_duration_seconds(
    raw_text: str,
    *,
    max_seconds: int = MAX_DURATION_SECONDS,
) -> int:
    """تبدیل ورودی زمان مالک به ثانیهٔ دقیق و معتبر.

    فرمت‌های پشتیبانی‌شده:
    * ``3:32:20`` / ``03:30:00`` (ساعت:دقیقه:ثانیه)
    * ``02:30`` (ساعت:دقیقه)
    * ``2 ساعت`` / ``30 دقیقه`` / ``45 ثانیه`` (با ارقام فارسی، عربی یا لاتین)
    * ترکیبی: ``3 ساعت و 32 دقیقه و 20 ثانیه``، ``2 ساعت 15 دقیقه``، ``10 دقیقه، 30 ثانیه``
    """
    norm = normalize_text(raw_text)
    if not norm:
        raise InvalidDurationError("empty")

    # رد صریح اعداد خام بدون واحد یا مقادیر منفیِ ابتدای رشته
    if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", norm):
        val = float(norm.replace(",", "."))
        if val <= 0:
            raise InvalidDurationError("non_positive")
        raise InvalidDurationError("missing_unit")

    # ۱) فرمت H:M:S
    m_hms = _COLON_HMS_RE.match(norm)
    if m_hms:
        hours = int(m_hms.group(1))
        minutes = int(m_hms.group(2))
        seconds = int(m_hms.group(3))
        if hours < 0 or minutes < 0 or seconds < 0:
            raise InvalidDurationError("negative")
        if minutes >= 60 or seconds >= 60:
            raise InvalidDurationError("invalid_colon_range")
        total = hours * 3600 + minutes * 60 + seconds
        if total < MIN_DURATION_SECONDS:
            raise InvalidDurationError("non_positive")
        if total > int(max_seconds):
            raise DurationTooLongError(total, int(max_seconds))
        return total

    # ۲) فرمت H:M
    m_hm = _COLON_HM_RE.match(norm)
    if m_hm:
        hours = int(m_hm.group(1))
        minutes = int(m_hm.group(2))
        if hours < 0 or minutes < 0:
            raise InvalidDurationError("negative")
        if minutes >= 60:
            raise InvalidDurationError("invalid_colon_range")
        total = hours * 3600 + minutes * 60
        if total < MIN_DURATION_SECONDS:
            raise InvalidDurationError("non_positive")
        if total > int(max_seconds):
            raise DurationTooLongError(total, int(max_seconds))
        return total

    if ":" in norm:
        raise InvalidDurationError("invalid_colon_format")

    # ۳) فرمت واحددار (تکی یا ترکیبی از ساعت / دقیقه / ثانیه)
    matches = list(_UNIT_TOKEN_RE.finditer(norm))
    if not matches:
        raise InvalidDurationError("invalid_format")

    # اطمینان از اینکه بیرون از توکن‌های مجاز فقط جداکننده‌های «و» / «،» / «,» / فاصله وجود دارد
    cursor = 0
    seen_units = set()
    total = 0
    multipliers = {"h": 3600, "m": 60, "s": 1}

    for match in matches:
        gap = norm[cursor:match.start()].strip()
        if gap and not re.fullmatch(r"(?:و|[،,])(?:\s*(?:و|[،,]))*", gap):
            raise InvalidDurationError("unexpected_tokens")
        num_str, unit_word = match.group(1), match.group(2)
        if "." in num_str or "/" in num_str:
            raise InvalidDurationError("non_integer")
        amount = int(num_str)
        if amount <= 0:
            raise InvalidDurationError("non_positive")
        ukind = _unit_kind(unit_word)
        if ukind in seen_units:
            raise InvalidDurationError("duplicate_unit")
        seen_units.add(ukind)
        total += amount * multipliers[ukind]
        cursor = match.end()

    trailing = norm[cursor:].strip()
    if trailing:
        raise InvalidDurationError("unexpected_trailing_tokens")

    if total < MIN_DURATION_SECONDS:
        raise InvalidDurationError("non_positive")
    if total > int(max_seconds):
        raise DurationTooLongError(total, int(max_seconds))
    return total


# ---------------------------------------------------------------------------
#  بررسی احراز هویت مالک اصلی ثبت‌شدهٔ ربات
# ---------------------------------------------------------------------------
def is_authorized_owner(user) -> bool:
    """فقط مالک ثبت‌شدهٔ اصلی ربات مجاز است (از منبع واحد ``owner_check``)."""
    return bool(owner_check.is_global_owner(user))


# ---------------------------------------------------------------------------
#  لایهٔ پایدار SQLite + کش حافظه‌ای زمان‌بندی‌های فعال
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_shutdown_schedules (
    group_id TEXT PRIMARY KEY,
    raw_chat_id INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK(duration_seconds > 0),
    duration_raw TEXT NOT NULL,
    shutdown_ts REAL NOT NULL,
    schedule_id INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'scheduled',
    lock_status TEXT NOT NULL DEFAULT 'pending',
    executed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_group_shutdown_status_ts
    ON group_shutdown_schedules(status, shutdown_ts);
"""

_STATE_LOCK = threading.RLock()
_READY_CONN_ID: Optional[int] = None
#: group_key -> dict رکورد زمان‌بندی فعال (scheduled / executing / retry_pending)
_ACTIVE_SCHEDULES: Dict[str, dict] = {}

#: وضعیت زمان‌بند مرکزی
_SCHEDULER_TASK: Optional[asyncio.Task] = None
_SCHEDULER_LOOP: Optional[asyncio.AbstractEventLoop] = None
_WAKE_EVENT: Optional[asyncio.Event] = None
_BOUND_BOT = None
_BOUND_LOGGER = None
_CLOCK_FN: Callable[[], float] = time.time


def _gkey(chat_id) -> str:
    return normalize_group_id(chat_id)


def _canonical_chat_id(chat_id) -> int:
    """شناسهٔ عددیِ منفیِ معتبر SPlusthon برای گروه/سوپرگروه."""
    raw_val = int(chat_id)
    if raw_val < 0:
        return raw_val
    short_id = int(normalize_group_id(chat_id))
    return -(CHANNEL_ID_OFFSET + abs(short_id))


def _row_to_dict(row) -> dict:
    return {
        "group_id": str(row["group_id"]),
        "raw_chat_id": int(row["raw_chat_id"]),
        "owner_id": str(row["owner_id"]),
        "created_at": float(row["created_at"]),
        "duration_seconds": int(row["duration_seconds"]),
        "duration_raw": str(row["duration_raw"]),
        "shutdown_ts": float(row["shutdown_ts"]),
        "schedule_id": int(row["schedule_id"]),
        "status": str(row["status"]),
        "lock_status": str(row["lock_status"]),
        "executed_at": float(row["executed_at"]) if row["executed_at"] is not None else None,
        "attempts": int(row["attempts"] or 0),
        "next_attempt_ts": (
            float(row["next_attempt_ts"]) if row["next_attempt_ts"] is not None else None
        ),
        "last_error": row["last_error"],
    }


def _ensure_loaded(*, force: bool = False, reset_interrupted: bool = False) -> None:
    """ایجاد جدول در صورت نیاز و همگام‌سازی ایندکس زمان‌بندی‌های فعال در حافظه."""
    global _READY_CONN_ID
    conn = runtime_db.connection()
    with _STATE_LOCK:
        if not force and not reset_interrupted and _READY_CONN_ID == id(conn):
            return
        with runtime_db._LOCK:
            conn.executescript(_SCHEMA)
            if reset_interrupted:
                # اگر ربات دقیقاً وسط اجرای RPC ری‌استارت شده باشد، وضعیت به scheduled
                # برمی‌گردد تا بلافاصله پس از بالا آمدن تکمیل گردد.
                conn.execute(
                    "UPDATE group_shutdown_schedules SET status = ? WHERE status = ?",
                    (STATUS_SCHEDULED, STATUS_EXECUTING),
                )
            conn.commit()
            rows = conn.execute(
                """
                SELECT * FROM group_shutdown_schedules
                WHERE status IN (?, ?, ?)
                """,
                (STATUS_SCHEDULED, STATUS_EXECUTING, STATUS_RETRY_PENDING),
            ).fetchall()
        _ACTIVE_SCHEDULES.clear()
        for row in rows:
            rec = _row_to_dict(row)
            _ACTIVE_SCHEDULES[rec["group_id"]] = rec
        _READY_CONN_ID = id(conn)


def _wake_scheduler() -> None:
    """بیدار کردن فوری حلقهٔ زمان‌بند مرکزی به‌صورت thread-safe / loop-safe."""
    ev = _WAKE_EVENT
    loop = _SCHEDULER_LOOP
    if ev is None or loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        ev.set()
    else:
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            pass


def set_clock_for_tests(clock_fn: Optional[Callable[[], float]]) -> None:
    """تنظیم تابع ساعت برای تست‌های قطعی (در حالت عادی ``time.time``)."""
    global _CLOCK_FN
    _CLOCK_FN = clock_fn if callable(clock_fn) else time.time
    _wake_scheduler()


def now_timestamp() -> float:
    return float(_CLOCK_FN())


# ---------------------------------------------------------------------------
#  خواندن، ثبت و لغو زمان‌بندی در دیتابیس
# ---------------------------------------------------------------------------
def get_schedule(chat_id) -> Optional[dict]:
    """دریافت آخرین رکورد ذخیره‌شدهٔ گروه از دیتابیس (فعال یا پایان‌یافته)."""
    _ensure_loaded()
    gkey = _gkey(chat_id)
    with _STATE_LOCK:
        cached = _ACTIVE_SCHEDULES.get(gkey)
        if cached is not None:
            return dict(cached)
        conn = runtime_db.connection()
        with runtime_db._LOCK:
            row = conn.execute(
                "SELECT * FROM group_shutdown_schedules WHERE group_id = ?",
                (gkey,),
            ).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_active_schedule(chat_id) -> Optional[dict]:
    """دریافت زمان‌بندی فعال گروه در حافظه (O(1))."""
    _ensure_loaded()
    gkey = _gkey(chat_id)
    with _STATE_LOCK:
        rec = _ACTIVE_SCHEDULES.get(gkey)
        return dict(rec) if rec is not None else None


def list_active_schedules() -> List[dict]:
    _ensure_loaded()
    with _STATE_LOCK:
        return [dict(rec) for rec in _ACTIVE_SCHEDULES.values()]


async def schedule_shutdown(
    bot,
    chat_id,
    owner_id,
    duration_raw: str,
    *,
    duration_seconds: Optional[int] = None,
    created_at: Optional[float] = None,
    logger=None,
) -> dict:
    """ثبت یا جایگزینی اتمیک زمان‌بندی خاموشی گروه توسط مالک ثبت‌شدهٔ ربات.

    اگر برای این گروه از قبل زمان‌بندی فعالی وجود داشته باشد، به‌صورت اتمیک با
    ``schedule_id`` جدید جایگزین می‌شود تا هرگز دو زمان‌بندی متناقض هم‌زمان وجود
    نداشته باشد.
    """
    if not is_authorized_owner(owner_id):
        raise UnauthorizedOwnerError("only global registered bot owner is permitted")

    secs = (
        int(duration_seconds)
        if duration_seconds is not None
        else parse_duration_seconds(duration_raw)
    )
    if secs < MIN_DURATION_SECONDS:
        raise InvalidDurationError("non_positive")
    if secs > MAX_DURATION_SECONDS:
        raise DurationTooLongError(secs, MAX_DURATION_SECONDS)

    _ensure_loaded()
    gkey = _gkey(chat_id)
    raw_cid = _canonical_chat_id(chat_id)
    start_ts = float(now_timestamp() if created_at is None else created_at)
    target_ts = start_ts + float(secs)
    clean_raw = normalize_text(duration_raw) or f"{secs}s"

    with _STATE_LOCK:
        with runtime_db.transaction(immediate=True) as conn:
            prev = conn.execute(
                "SELECT schedule_id FROM group_shutdown_schedules WHERE group_id = ?",
                (gkey,),
            ).fetchone()
            next_schedule_id = (int(prev["schedule_id"]) + 1) if prev is not None else 1
            conn.execute(
                """
                INSERT INTO group_shutdown_schedules (
                    group_id, raw_chat_id, owner_id, created_at,
                    duration_seconds, duration_raw, shutdown_ts,
                    schedule_id, status, lock_status,
                    executed_at, attempts, next_attempt_ts, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL)
                ON CONFLICT(group_id) DO UPDATE SET
                    raw_chat_id = excluded.raw_chat_id,
                    owner_id = excluded.owner_id,
                    created_at = excluded.created_at,
                    duration_seconds = excluded.duration_seconds,
                    duration_raw = excluded.duration_raw,
                    shutdown_ts = excluded.shutdown_ts,
                    schedule_id = excluded.schedule_id,
                    status = excluded.status,
                    lock_status = excluded.lock_status,
                    executed_at = NULL,
                    attempts = 0,
                    next_attempt_ts = NULL,
                    last_error = NULL
                """,
                (
                    gkey,
                    raw_cid,
                    str(int(owner_id)),
                    start_ts,
                    secs,
                    clean_raw,
                    target_ts,
                    next_schedule_id,
                    STATUS_SCHEDULED,
                    LOCK_PENDING,
                ),
            )
            row = conn.execute(
                "SELECT * FROM group_shutdown_schedules WHERE group_id = ?",
                (gkey,),
            ).fetchone()
        rec = _row_to_dict(row)
        _ACTIVE_SCHEDULES[gkey] = rec

    if logger and hasattr(logger, "log_info"):
        logger.log_info(
            f"[GROUP_SHUTDOWN] زمان‌بندی ثبت شد chat={raw_cid} owner={owner_id} "
            f"duration={secs}s shutdown_ts={target_ts:.3f} schedule_id={rec['schedule_id']}"
        )

    ensure_scheduler(bot, logger=logger or getattr(bot, "logger", None))
    _wake_scheduler()
    return dict(rec)


def cancel_shutdown(chat_id, *, owner_id=None, logger=None) -> bool:
    """لغو زمان‌بندی فعال گروه (فقط توسط مالک ثبت‌شده در صورت ارسال ``owner_id``)."""
    if owner_id is not None and not is_authorized_owner(owner_id):
        raise UnauthorizedOwnerError("only global registered bot owner is permitted")

    _ensure_loaded()
    gkey = _gkey(chat_id)
    with _STATE_LOCK:
        with runtime_db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM group_shutdown_schedules WHERE group_id = ?",
                (gkey,),
            ).fetchone()
            if row is None or str(row["status"]) not in ACTIVE_STATUSES:
                _ACTIVE_SCHEDULES.pop(gkey, None)
                return False
            new_sid = int(row["schedule_id"]) + 1
            conn.execute(
                """
                UPDATE group_shutdown_schedules
                SET status = ?, schedule_id = ?, next_attempt_ts = NULL
                WHERE group_id = ?
                """,
                (STATUS_CANCELLED, new_sid, gkey),
            )
        _ACTIVE_SCHEDULES.pop(gkey, None)

    _wake_scheduler()
    if logger and hasattr(logger, "log_info"):
        logger.log_info(f"[GROUP_SHUTDOWN] زمان‌بندی لغو شد group={gkey}")
    return True


# ---------------------------------------------------------------------------
#  اجرای قفل خودکار گروه با اتصال مستقیم به GroupActions موجود
# ---------------------------------------------------------------------------
def _get_group_actions(bot, logger=None) -> GroupActions:
    actions = getattr(bot, "group_actions", None)
    if actions is not None:
        return actions
    return GroupActions(bot.client, logger or getattr(bot, "logger", None))


def _is_permanent_lock_error(exc: BaseException) -> bool:
    """تشخیص خطاهای دائمی مجوز/عدم پشتیبانی API از خطاهای موقت شبکه."""
    cls_name = type(exc).__name__
    if cls_name in {
        "ChatAdminRequiredError",
        "AdminRankInvalidError",
        "ChatWriteForbiddenError",
        "ChannelPrivateError",
        "ChatInvalidError",
        "ChannelInvalidError",
    }:
        return True
    msg = str(exc or "").upper()
    permanent_markers = (
        "CHAT_ADMIN_REQUIRED",
        "ADMIN_RANK_INVALID",
        "DID NOT TAKE EFFECT",
        "REFUSING TO EDIT RIGHTS",
        "CHANNEL_PRIVATE",
        "CHAT_INVALID",
        "CHANNEL_INVALID",
        "RIGHTS_NOT_MODIFIED",
        "NOT SUPPORTED",
    )
    return any(marker in msg for marker in permanent_markers)


async def _send_group_notification(
    bot,
    chat_id: int,
    peer,
    text: str,
    entities: list,
    logger=None,
) -> bool:
    """ارسال پیام اعلان خاموشی در گروه با حفظ دقیق formatting_entities."""
    client = getattr(bot, "client", None)
    if client is None or not hasattr(client, "send_message"):
        return False
    target = peer if peer is not None else chat_id
    for attempt in range(2):
        try:
            await asyncio.wait_for(
                client.send_message(
                    target,
                    text,
                    formatting_entities=list(entities) if entities else None,
                ),
                timeout=15.0,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            wait_s = flood_seconds(exc)
            if attempt == 0 and wait_s is not None and 0 < wait_s <= 5.0:
                await asyncio.sleep(float(wait_s))
                continue
            if logger and hasattr(logger, "log_error"):
                logger.log_error(
                    f"[GROUP_SHUTDOWN] خطا در ارسال اعلان خاموشی chat={chat_id}: {exc!r}"
                )
            return False
    return False


async def _execute_group_shutdown(
    bot,
    gkey: str,
    expected_schedule_id: int,
    logger=None,
) -> dict:
    """اجرای واقعی خاموشی یک گروه داخل کارگر اختصاصیِ همان گروه.

    مراحل:
    1. بازخوانی رکورد و اطمینان از معتبر بودن ``schedule_id`` (جلوگیری قطعی از اجرای تکراری).
    2. بررسی وضعیت فعلی قفل گروه مستقیماً از سرور (``_fetch_default_banned_rights``).
    3. اگر گروه از قبل قفل است -> عدم انجام عملیات تکراری و ثبت ``already_locked``.
    4. اگر گروه باز است -> قفل واقعی با ``GroupActions.lock_group`` (حفظ سایر دسترسی‌ها)
       + ثبت ``locked`` + ارسال پیام اعلان از ``texts.group_shutdown_executed()``.
    """
    log = logger or getattr(bot, "logger", None)

    with _STATE_LOCK:
        conn = runtime_db.connection()
        with runtime_db._LOCK:
            row = conn.execute(
                "SELECT * FROM group_shutdown_schedules WHERE group_id = ?",
                (gkey,),
            ).fetchone()
        if row is None:
            _ACTIVE_SCHEDULES.pop(gkey, None)
            return {"ok": False, "reason": "missing"}
        rec = _row_to_dict(row)
        if (
            rec["schedule_id"] != int(expected_schedule_id)
            or rec["status"] in {STATUS_EXECUTED, STATUS_CANCELLED, STATUS_FAILED}
        ):
            return {"ok": False, "reason": "stale_or_completed"}

    raw_chat_id = rec["raw_chat_id"]
    actions = _get_group_actions(bot, log)
    peer = None

    try:
        peer = await asyncio.wait_for(
            actions._group_peer(raw_chat_id),
            timeout=LOCK_RPC_TIMEOUT,
        )
        current_rights, rights_source = await asyncio.wait_for(
            actions._fetch_default_banned_rights(raw_chat_id, peer),
            timeout=LOCK_RPC_TIMEOUT,
        )
        already_locked = bool(text_is_banned(current_rights))

        if already_locked:
            peers.remember_lock_state(raw_chat_id, True)
            exec_ts = now_timestamp()
            with _STATE_LOCK:
                with runtime_db.transaction(immediate=True) as conn:
                    cur = conn.execute(
                        """
                        UPDATE group_shutdown_schedules
                        SET status = ?, lock_status = ?, executed_at = ?,
                            next_attempt_ts = NULL, last_error = NULL
                        WHERE group_id = ? AND schedule_id = ? AND status != ?
                        """,
                        (
                            STATUS_EXECUTED,
                            LOCK_ALREADY_LOCKED,
                            exec_ts,
                            gkey,
                            int(expected_schedule_id),
                            STATUS_EXECUTED,
                        ),
                    )
                    committed = cur.rowcount > 0
                _ACTIVE_SCHEDULES.pop(gkey, None)

            if committed:
                if log and hasattr(log, "log_info"):
                    log.log_info(
                        f"[GROUP_SHUTDOWN] گروه از قبل قفل بود (بدون عملیات تکراری) "
                        f"chat={raw_chat_id} source={rights_source} schedule_id={expected_schedule_id}"
                    )
                opt_tpl = texts.group_shutdown_optional_template(
                    "already_locked", chat_id=raw_chat_id
                )
                if opt_tpl:
                    opt_text, opt_entities = (
                        opt_tpl if isinstance(opt_tpl, tuple) else (str(opt_tpl), [])
                    )
                    await _send_group_notification(
                        bot, raw_chat_id, peer, opt_text, opt_entities, log
                    )
            return {"ok": True, "lock_status": LOCK_ALREADY_LOCKED}

        # گروه باز است -> اجرای قفل واقعی از طریق همان سیستم GroupActions ربات
        await asyncio.wait_for(
            actions.lock_group(raw_chat_id),
            timeout=LOCK_RPC_TIMEOUT,
        )
        peers.remember_lock_state(raw_chat_id, True)

        exec_ts = now_timestamp()
        with _STATE_LOCK:
            with runtime_db.transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE group_shutdown_schedules
                    SET status = ?, lock_status = ?, executed_at = ?,
                        next_attempt_ts = NULL, last_error = NULL
                    WHERE group_id = ? AND schedule_id = ? AND status != ?
                    """,
                    (
                        STATUS_EXECUTED,
                        LOCK_LOCKED,
                        exec_ts,
                        gkey,
                        int(expected_schedule_id),
                        STATUS_EXECUTED,
                    ),
                )
                committed = cur.rowcount > 0
            _ACTIVE_SCHEDULES.pop(gkey, None)

        if not committed:
            return {"ok": False, "reason": "superseded"}

        try:
            admin_tools.log_action(
                raw_chat_id,
                {"id": int(rec["owner_id"]), "first_name": "مالک ربات"},
                "خاموشی خودکار زمان‌بندی‌شده گروه",
            )
        except Exception:  # noqa: BLE001
            pass

        msg_text, msg_entities = texts.group_shutdown_executed()
        await _send_group_notification(
            bot, raw_chat_id, peer, msg_text, msg_entities, log
        )

        if log and hasattr(log, "log_info"):
            log.log_info(
                f"[GROUP_SHUTDOWN] قفل خودکار با موفقیت اجرا شد "
                f"chat={raw_chat_id} schedule_id={expected_schedule_id}"
            )
        return {"ok": True, "lock_status": LOCK_LOCKED}

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        err_text = f"{type(exc).__name__}: {exc}"
        attempts = int(rec.get("attempts") or 0) + 1
        permanent = _is_permanent_lock_error(exc)
        wait_s = flood_seconds(exc)
        transient = (not permanent) and (
            wait_s is not None or is_transient(exc) or isinstance(exc, asyncio.TimeoutError)
        )

        if transient and attempts < MAX_EXEC_RETRIES:
            retry_delay = float(wait_s) if (wait_s is not None and wait_s > 0) else min(15.0, 2.0 ** attempts)
            next_ts = now_timestamp() + retry_delay
            with _STATE_LOCK:
                with runtime_db.transaction(immediate=True) as conn:
                    conn.execute(
                        """
                        UPDATE group_shutdown_schedules
                        SET status = ?, attempts = ?, next_attempt_ts = ?, last_error = ?
                        WHERE group_id = ? AND schedule_id = ?
                        """,
                        (
                            STATUS_RETRY_PENDING,
                            attempts,
                            next_ts,
                            err_text,
                            gkey,
                            int(expected_schedule_id),
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM group_shutdown_schedules WHERE group_id = ?",
                        (gkey,),
                    ).fetchone()
                if row is not None and str(row["status"]) in ACTIVE_STATUSES:
                    _ACTIVE_SCHEDULES[gkey] = _row_to_dict(row)
            _wake_scheduler()
            if log and hasattr(log, "log_error"):
                log.log_error(
                    f"[GROUP_SHUTDOWN] خطای موقت در قفل خودکار chat={raw_chat_id} "
                    f"(تلاش {attempts}/{MAX_EXEC_RETRIES}، تلاش بعدی تا {retry_delay:.1f}s): {err_text}"
                )
            return {"ok": False, "reason": "retry_scheduled", "error": err_text}

        # خطای دائمی یا اتمام سقف تلاش‌ها -> ثبت صریح وضعیت failed بدون جایگزینی با حذف پیام
        with _STATE_LOCK:
            with runtime_db.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    UPDATE group_shutdown_schedules
                    SET status = ?, lock_status = ?, attempts = ?,
                        next_attempt_ts = NULL, last_error = ?
                    WHERE group_id = ? AND schedule_id = ?
                    """,
                    (
                        STATUS_FAILED,
                        LOCK_FAILED,
                        attempts,
                        err_text,
                        gkey,
                        int(expected_schedule_id),
                    ),
                )
            _ACTIVE_SCHEDULES.pop(gkey, None)

        if log and hasattr(log, "log_error"):
            log.log_error(
                f"[GROUP_SHUTDOWN] اجرای قفل واقعی گروه توسط API ممکن نشد "
                f"(بدون جایگزینی با حذف پیام) chat={raw_chat_id}: {err_text}"
            )
        opt_err = texts.group_shutdown_optional_template(
            "api_error", chat_id=raw_chat_id, error=err_text
        )
        if opt_err:
            opt_text, opt_entities = (
                opt_err if isinstance(opt_err, tuple) else (str(opt_err), [])
            )
            await _send_group_notification(
                bot, raw_chat_id, peer, opt_text, opt_entities, log
            )
        return {"ok": False, "reason": "api_failed", "error": err_text}


# ---------------------------------------------------------------------------
#  حلقهٔ زمان‌بند مرکزی پایدار (Single Central Scheduler)
# ---------------------------------------------------------------------------
def _collect_due_and_next_wake(now_ts: float) -> Tuple[List[Tuple[str, int]], Optional[float]]:
    """استخراج اتمیک زمان‌بندی‌های سررسیدشده و محاسبهٔ نزدیک‌ترین سررسید بعدی."""
    due: List[Tuple[str, int]] = []
    earliest_next: Optional[float] = None

    with _STATE_LOCK:
        for gkey, rec in list(_ACTIVE_SCHEDULES.items()):
            st = rec.get("status")
            if st == STATUS_EXECUTING:
                continue
            due_ts = (
                rec["next_attempt_ts"]
                if (st == STATUS_RETRY_PENDING and rec.get("next_attempt_ts") is not None)
                else rec["shutdown_ts"]
            )
            if now_ts >= float(due_ts):
                sid = int(rec["schedule_id"])
                with runtime_db.transaction(immediate=True) as conn:
                    cur = conn.execute(
                        """
                        UPDATE group_shutdown_schedules
                        SET status = ?
                        WHERE group_id = ? AND schedule_id = ? AND status IN (?, ?)
                        """,
                        (
                            STATUS_EXECUTING,
                            gkey,
                            sid,
                            STATUS_SCHEDULED,
                            STATUS_RETRY_PENDING,
                        ),
                    )
                    if cur.rowcount > 0:
                        rec["status"] = STATUS_EXECUTING
                        due.append((gkey, sid))
            else:
                if earliest_next is None or float(due_ts) < earliest_next:
                    earliest_next = float(due_ts)

    return due, earliest_next


async def run_due_once(bot=None, logger=None) -> int:
    """بررسی فوری تمام سررسیدها و ارسال به صف اختصاصی گروه‌ها (مفید برای بازیابی و تست)."""
    active_bot = bot if bot is not None else _BOUND_BOT
    active_log = logger if logger is not None else _BOUND_LOGGER
    if active_bot is None:
        return 0

    _ensure_loaded()
    due_items, _ = _collect_due_and_next_wake(now_timestamp())
    dispatched = 0
    futures = []

    for gkey, sid in due_items:
        try:
            fut = await QUEUE.submit(
                gkey,
                lambda gk=gkey, s=sid: _execute_group_shutdown(
                    active_bot, gk, s, logger=active_log
                ),
                dedupe_key=f"shutdown:{sid}",
                logger=active_log,
            )
            futures.append(fut)
            dispatched += 1
        except QueueFull as q_exc:
            if "duplicate" not in str(q_exc).lower():
                # بازگردانی وضعیت به scheduled تا در تیک بعدی مجدداً ارسال شود
                with _STATE_LOCK:
                    with runtime_db.transaction(immediate=True) as conn:
                        conn.execute(
                            """
                            UPDATE group_shutdown_schedules
                            SET status = ?
                            WHERE group_id = ? AND schedule_id = ? AND status = ?
                            """,
                            (STATUS_SCHEDULED, gkey, sid, STATUS_EXECUTING),
                        )
                    if gkey in _ACTIVE_SCHEDULES and _ACTIVE_SCHEDULES[gkey]["schedule_id"] == sid:
                        _ACTIVE_SCHEDULES[gkey]["status"] = STATUS_SCHEDULED
                if active_log and hasattr(active_log, "log_error"):
                    active_log.log_error(
                        f"[GROUP_SHUTDOWN] صف خاموشی موقتاً پر است group={gkey}: {q_exc}"
                    )

    if futures:
        await asyncio.gather(*futures, return_exceptions=True)
    return dispatched


async def _central_scheduler_loop() -> None:
    """حلقهٔ واحد زمان‌بند مرکزی: بدون sleep طولانی و مستقل از ترافیک پیام‌ها."""
    global _WAKE_EVENT
    while True:
        try:
            bot = _BOUND_BOT
            logger = _BOUND_LOGGER
            if bot is not None:
                _ensure_loaded()
                due_items, earliest_next = _collect_due_and_next_wake(now_timestamp())
                for gkey, sid in due_items:
                    try:
                        await QUEUE.submit(
                            gkey,
                            lambda gk=gkey, s=sid, b=bot, lg=logger: _execute_group_shutdown(
                                b, gk, s, logger=lg
                            ),
                            dedupe_key=f"shutdown:{sid}",
                            logger=logger,
                        )
                    except QueueFull as q_exc:
                        if "duplicate" not in str(q_exc).lower():
                            with _STATE_LOCK:
                                with runtime_db.transaction(immediate=True) as conn:
                                    conn.execute(
                                        """
                                        UPDATE group_shutdown_schedules
                                        SET status = ?
                                        WHERE group_id = ? AND schedule_id = ? AND status = ?
                                        """,
                                        (STATUS_SCHEDULED, gkey, sid, STATUS_EXECUTING),
                                    )
                                if (
                                    gkey in _ACTIVE_SCHEDULES
                                    and _ACTIVE_SCHEDULES[gkey]["schedule_id"] == sid
                                ):
                                    _ACTIVE_SCHEDULES[gkey]["status"] = STATUS_SCHEDULED
            else:
                earliest_next = None

            ev = _WAKE_EVENT
            if ev is None:
                ev = _WAKE_EVENT = asyncio.Event()
            ev.clear()

            # اگر در حین ارسال به صف، سررسید تازه‌ای رسیده باشد بدون مکث ادامه بده
            with _STATE_LOCK:
                now_ts = now_timestamp()
                immediate = any(
                    rec.get("status") in {STATUS_SCHEDULED, STATUS_RETRY_PENDING}
                    and now_ts
                    >= float(
                        rec["next_attempt_ts"]
                        if (
                            rec.get("status") == STATUS_RETRY_PENDING
                            and rec.get("next_attempt_ts") is not None
                        )
                        else rec["shutdown_ts"]
                    )
                    for rec in _ACTIVE_SCHEDULES.values()
                )
            if immediate and bot is not None:
                continue

            if earliest_next is None:
                wait_timeout = MAX_SCHEDULER_TICK
            else:
                remaining = earliest_next - now_timestamp()
                wait_timeout = max(0.01, min(remaining, MAX_SCHEDULER_TICK))

            try:
                await asyncio.wait_for(ev.wait(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _BOUND_LOGGER and hasattr(_BOUND_LOGGER, "log_error"):
                _BOUND_LOGGER.log_error(
                    f"[GROUP_SHUTDOWN] خطا در حلقهٔ زمان‌بند مرکزی: {exc!r}"
                )
            await asyncio.sleep(0.25)


def ensure_scheduler(bot, logger=None) -> Optional[asyncio.Task]:
    """اطمینان از فعال بودن تک‌تسک زمان‌بند مرکزی روی حلقهٔ رویداد جاری."""
    global _BOUND_BOT, _BOUND_LOGGER, _SCHEDULER_TASK, _SCHEDULER_LOOP, _WAKE_EVENT
    _BOUND_BOT = bot
    if logger is not None:
        _BOUND_LOGGER = logger
    elif getattr(bot, "logger", None) is not None:
        _BOUND_LOGGER = bot.logger

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    if (
        _SCHEDULER_TASK is not None
        and not _SCHEDULER_TASK.done()
        and _SCHEDULER_LOOP is loop
    ):
        _wake_scheduler()
        return _SCHEDULER_TASK

    _SCHEDULER_LOOP = loop
    _WAKE_EVENT = asyncio.Event()
    _SCHEDULER_TASK = loop.create_task(
        _central_scheduler_loop(),
        name="foxora-group-shutdown-scheduler",
    )
    return _SCHEDULER_TASK


async def stop_scheduler() -> None:
    """توقف تمیز زمان‌بند مرکزی و انتظار برای تخلیهٔ صف (برای خاموشی ربات یا تست‌ها)."""
    global _SCHEDULER_TASK, _SCHEDULER_LOOP, _WAKE_EVENT
    task = _SCHEDULER_TASK
    _SCHEDULER_TASK = None
    _SCHEDULER_LOOP = None
    _WAKE_EVENT = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    await QUEUE.drain(timeout=5.0)


def recover(bot, logger=None) -> dict:
    """بازیابی زمان‌بندی‌های فعال از دیتابیس پس از ری‌استارت و راه‌اندازی زمان‌بند مرکزی.

    اگر زمان خاموشی گروهی در مدت خاموش بودن ربات گذشته باشد، زمان‌بند مرکزی
    بلافاصله پس از بالا آمدن آن را اجرا خواهد کرد.
    """
    _ensure_loaded(force=True, reset_interrupted=True)
    now_ts = now_timestamp()
    active_count = 0
    overdue_count = 0

    with _STATE_LOCK:
        for rec in _ACTIVE_SCHEDULES.values():
            active_count += 1
            if now_ts >= float(rec["shutdown_ts"]):
                overdue_count += 1

    ensure_scheduler(bot, logger=logger)
    _wake_scheduler()

    log = logger or getattr(bot, "logger", None)
    if log and hasattr(log, "log_info") and active_count > 0:
        log.log_info(
            f"[GROUP_SHUTDOWN] بازیابی پس از ری‌استارت: active={active_count}, overdue={overdue_count}"
        )
    return {"active": active_count, "overdue": overdue_count}


def reset_runtime_caches() -> None:
    """پاک‌سازی کش‌های حافظه و بازنشانی صف (ویژهٔ شبیه‌سازی ری‌استارت در تست‌ها)."""
    global _READY_CONN_ID, _SCHEDULER_TASK, _SCHEDULER_LOOP, _WAKE_EVENT, _BOUND_BOT, _BOUND_LOGGER
    if _SCHEDULER_TASK is not None and not _SCHEDULER_TASK.done():
        _SCHEDULER_TASK.cancel()
    _SCHEDULER_TASK = None
    _SCHEDULER_LOOP = None
    _WAKE_EVENT = None
    _BOUND_BOT = None
    _BOUND_LOGGER = None
    with _STATE_LOCK:
        _ACTIVE_SCHEDULES.clear()
        _READY_CONN_ID = None
    reset_queue(QUEUE)
