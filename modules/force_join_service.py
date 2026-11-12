"""🔒 سرویس «عضویت اجباری کانال» — منطق کم‌مصرف، پایدار و fail-safe.

معماری در یک نگاه (ارزان‌ترین لایه اول):

1. **رویداد (هزینهٔ صفر)** — ``events.ChatAction`` هم ورود و هم خروج کانال را
   می‌دهد (``UpdateChannelParticipant`` → ``user_joined``/``user_left``/
   ``user_kicked``). ورود/خروج فوراً در دیتابیس و کش حافظه ثبت می‌شود.
2. **کش حافظه** — وضعیت هر (کاربر، کانال) با TTL مستقل برای «عضو» و
   «غیرعضو».
3. **SQLite** — همان وضعیت پایدار + ``access_hash`` کاربر و کانال؛ همین
   چیزی است که بعد از ری‌استارت صفر فراخوانی اضافه می‌دهد.
4. **API (حداکثر یک فراخوانی)** — ``channels.getParticipant`` برای همان یک
   کاربر. هرگز ``getParticipants`` در مسیر پیام صدا زده نمی‌شود.

قواعد سخت:

* در قطعی/تایم‌اوت API **عضویت تأیید نمی‌شود** و پیام کاربر **حذف نمی‌شود**
  (fail-open). فقط وقتی «عضو نیست» حذف می‌کند که شاهد واقعی داشته باشیم
  (رویداد خروج، ``UserNotParticipant`` یا اسنپ‌شات کامل).
* single-flight: برای هر (کاربر، کانال) فقط یک درخواست هم‌زمان؛ بقیه منتظر
  همان نتیجه می‌مانند.
* سقف هم‌زمانی با ``asyncio.Semaphore``.
* «خارج‌شده» فقط از اسنپ‌شات **کامل** استنتاج می‌شود؛ اسنپ‌شات ناقص هرگز
  باعث حذف نمی‌شود.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from modules import force_join_api as api
from modules import force_join_storage as storage
from modules.force_join_ids import canonical_group_key, canonical_group_key_str, channel_key

try:  # entity های واقعی سروش پلاس (Bold / نقل‌قول شیشه‌ای)
    from splusthon.tl.types import (  # type: ignore
        MessageEntityBlockquote,
        MessageEntityBold,
    )
except Exception:  # pragma: no cover - محیط بدون کتابخانه
    class MessageEntityBold:  # type: ignore
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:  # type: ignore
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


def _u16(text: str) -> int:
    """طول رشته به واحد UTF-16 (استاندارد offset در سروش پلاس/تلگرام)."""
    return len(text.encode("utf-16-le")) // 2


def user_label(user_entity=None, user_id=None) -> str:
    """«@یوزرنیم» و اگر نبود «نام نمایشی» و اگر آن هم نبود شناسهٔ عددی."""
    label = _display_name(user_entity)
    if label:
        return label
    return str(user_id) if user_id else "کاربر"

# ---------------------------------------------------------------------------
# تنظیمات (همه از محیط، بدون تغییر کد)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int, minimum: int = 0,
             maximum: int = 10 ** 9) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float = 0.0,
               maximum: float = 10 ** 9) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def member_ttl() -> int:
    """چند ثانیه وضعیت «عضو» بدون بررسی دوباره معتبر است."""
    return _env_int("FORCE_JOIN_MEMBER_TTL", 300, 30, 7 * 24 * 3600)


def not_member_ttl() -> int:
    """چند ثانیه وضعیت «غیرعضو» معتبر است (کوتاه‌تر، تا عضو تازه اذیت نشود)."""
    return _env_int("FORCE_JOIN_NOT_MEMBER_TTL", 20, 10, 24 * 3600)


def member_grace_seconds() -> int:
    """در قطعی API، آخرین وضعیت «عضو» تا این مدت پذیرفته می‌شود."""
    return _env_int("FORCE_JOIN_MEMBER_GRACE_SECONDS", 24 * 3600, 0, 30 * 24 * 3600)


def not_member_grace_seconds() -> int:
    """در قطعی API، شاهد «غیرعضو» تا این مدت همچنان اجرا می‌شود."""
    return _env_int("FORCE_JOIN_NOT_MEMBER_GRACE_SECONDS", 20, 0, 24 * 3600)


def api_timeout() -> float:
    return _env_float("FORCE_JOIN_API_TIMEOUT", 8.0, 1.0, 60.0)


def resolve_timeout() -> float:
    return _env_float("FORCE_JOIN_RESOLVE_TIMEOUT", 20.0, 2.0, 120.0)


def max_concurrency() -> int:
    return _env_int("FORCE_JOIN_MAX_CONCURRENCY", 2, 1, 16)


# --- کشِ سریعِ عضویت (همه با فلگ خاموش‌شدنی؛ خاموش = رفتار دقیقاً مثل قبل) ---

def cache_enabled() -> bool:
    """آیا لایهٔ کشِ سریعِ عضویت فعال است؟ (خاموش = کدِ قبلی)"""
    return _env_flag("FORCE_JOIN_CACHE_ENABLED", True)


def fast_member_ttl() -> int:
    """TTL «عضو است» در حالتِ مسیرِ ارزان (پیش‌فرض ۶۰ ثانیه)."""
    return _env_int("FORCE_JOIN_FAST_MEMBER_TTL", 60, 10, 7 * 24 * 3600)


def fast_not_member_ttl() -> int:
    """TTL «عضو نیست» در حالتِ مسیرِ ارزان (پیش‌فرض ۴ ثانیه: ۳ تا ۵)."""
    return _env_int("FORCE_JOIN_FAST_NOT_MEMBER_TTL", 4, 1, 300)


def fast_ttl_always() -> bool:
    """TTLهای کوتاه حتی وقتی فقط «اسکنِ کامل» در دسترس است (پیش‌فرض: نه).

    چرا پیش‌فرض خاموش: روی سروری که مسیرِ تک‌کاربره ندارد، TTL کوتاه یعنی
    اسکنِ کاملِ بیشتر — خلافِ هدفِ کم‌کردنِ درخواست‌ها.
    """
    return _env_flag("FORCE_JOIN_FAST_TTL_ALWAYS", False)


def cheap_route_enabled() -> bool:
    """آیا مسیرِ تک‌کاربره (GetParticipant) اول امتحان شود؟"""
    return _env_flag("FORCE_JOIN_CHEAP_ROUTE", True)


def cheap_route_retry_seconds() -> int:
    """اگر سرور مسیرِ تک‌کاربره را نداشت، چند ثانیه دوباره امتحان نشود."""
    return _env_int("FORCE_JOIN_CHEAP_ROUTE_RETRY", 3600, 60, 7 * 24 * 3600)


def gate_await_timeout() -> float:
    """سقف انتظار تصمیم‌گیری در مسیر پیام؛ بیشتر از این = fail-open."""
    return _env_float("FORCE_JOIN_GATE_TIMEOUT", 3.0, 0.5, 30.0)


def notice_cooldown() -> int:
    """فاصلهٔ حداقلی ارسال پیام راهنما برای همان کاربر (ضد اسپم)."""
    return _env_int("FORCE_JOIN_NOTICE_COOLDOWN", 600, 0, 24 * 3600)


def notice_per_minute() -> int:
    """سقف پیام راهنما در دقیقه برای هر گروه."""
    return _env_int("FORCE_JOIN_NOTICE_PER_MINUTE", 20, 1, 120)


def exempt_bots() -> bool:
    return _env_int("FORCE_JOIN_EXEMPT_BOTS", 1, 0, 1) == 1


def check_on_group_join() -> bool:
    """ورود کاربر به گروه، عضویت کانال را پیش‌بررسی (prefetch) کند؟"""
    return _env_int("FORCE_JOIN_PREFETCH_ON_JOIN", 1, 0, 1) == 1


def sync_interval() -> int:
    """فاصلهٔ اسنپ‌شات جبرانی (تشخیص خروج‌های بی‌رویداد)."""
    return _env_int("FORCE_JOIN_SYNC_INTERVAL", 6 * 3600, 300, 7 * 24 * 3600)


def event_snapshot_interval() -> int:
    """در حالت رویدادمحور، اسنپ‌شات جبرانی دیرتر لازم است."""
    return _env_int("FORCE_JOIN_EVENT_SNAPSHOT_INTERVAL", 12 * 3600, 600,
                    7 * 24 * 3600)


def sync_budget() -> float:
    return _env_float("FORCE_JOIN_SYNC_BUDGET", 90.0, 5.0, 1800.0)


def sync_rpc_delay() -> float:
    return _env_float("FORCE_JOIN_SYNC_RPC_DELAY", 0.35, 0.0, 30.0)


def sync_page_limit() -> int:
    return _env_int("FORCE_JOIN_SYNC_PAGE_LIMIT", 0, 0, 500_000)


def sync_max_members() -> int:
    """سقف تعداد عضو ذخیره‌شده در هر اسنپ‌شات (۰ = بی‌سقف)."""
    return _env_int("FORCE_JOIN_SYNC_MAX_MEMBERS", 20000, 0, 1_000_000)


def start_delay() -> float:
    return _env_float("FORCE_JOIN_START_DELAY", 180.0, 0.0, 3600.0)


# ---------------------------------------------------------------------------
# زنجیرهٔ مسیرهای بررسی عضویت (Route chain)
# ---------------------------------------------------------------------------
# واقعیتِ اندازه‌گیری‌شده روی سروش پلاس: ``channels.getParticipant`` برای این
# حساب ``RPCError 422: NOT_SUPPORTED`` می‌دهد، یعنی سرور این متد را پیاده
# نکرده است. پس «یک راه» نداریم؛ چند مسیر داریم و هر کدام که سرور جواب داد
# استفاده می‌شود. ترتیب = ارزان‌ترین/دقیق‌ترین اول:
#
#   1) get_participant : channels.getParticipant  — ارزان‌ترین (اگر پشتیبانی شود)
#   2) bot_api         : getChatMember در Bot API — همان روش ربات‌های تلگرامی
#   3) search          : ChannelParticipantsSearch هدفمند (فقط «پیدا شد» قطعی است)
#   4) snapshot        : اسنپ‌شات کامل اعضا + diff (فقط وقتی تازه و کامل است)

ROUTE_PARTICIPANT = "get_participant"
ROUTE_BOT_API = "bot_api"
ROUTE_SEARCH = "search"
ROUTE_SNAPSHOT = "snapshot"
#: پیمایش هدفمند فهرست اعضا تا پیدا شدنِ همان یک کاربر (حکم قطعی، سقف‌دار).
#: روی سروش این تنها راه قطعی است چون ``getParticipant`` وجود ندارد.
ROUTE_SCAN = "scan"

ROUTE_LABELS = {
    ROUTE_PARTICIPANT: "channels.getParticipant",
    ROUTE_BOT_API: "Bot API getChatMember",
    ROUTE_SEARCH: "جست‌وجوی هدفمند عضو",
    ROUTE_SCAN: "پیمایش هدفمند اعضا",
    ROUTE_SNAPSHOT: "اسنپ‌شات کامل اعضا",
}

#: مسیرهایی که «عضو نیست» را هم قطعی اعلام می‌کنند (بقیه فقط تأیید می‌کنند)
ROUTE_DECIDES_ABSENCE = (ROUTE_PARTICIPANT, ROUTE_BOT_API, ROUTE_SCAN,
                         ROUTE_SNAPSHOT)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


def bot_api_token() -> Optional[str]:
    """توکن ربات رسمی سروش (همان ``BOT_TOKEN`` پروژه).

    این ربات باید **ادمین همان کانال** باشد؛ وگرنه ``getChatMember``
    دسترسی نمی‌دهد.
    """
    for name in ("SOROUSH_BOT_TOKEN", "BOT_TOKEN", "FOXORA_BOT_TOKEN"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    try:
        from config import config_manager  # type: ignore
        for key in ("soroush_bot_token", "bot_token", "BOT_TOKEN"):
            value = (config_manager.get(key) or "").strip()
            if value:
                return value
    except Exception:
        pass
    return None


def channel_routes(record) -> dict:
    """مسیرهای فعالِ این کانال (ذخیره‌شده در زمان ثبت/سنجش)."""
    cursor = record.get("last_update_cursor") or {}
    stored = cursor.get("routes") if isinstance(cursor, dict) else None
    routes = {
        ROUTE_PARTICIPANT: bool((stored or {}).get(ROUTE_PARTICIPANT)),
        ROUTE_BOT_API: bool((stored or {}).get(ROUTE_BOT_API)),
        ROUTE_SEARCH: bool((stored or {}).get(ROUTE_SEARCH)),
        ROUTE_SNAPSHOT: bool((stored or {}).get(ROUTE_SNAPSHOT)),
        ROUTE_SCAN: bool((stored or {}).get(ROUTE_SCAN)),
    }
    if stored is None:
        # رکوردهای legacy شواهدی از پشتیبانی سرور ندارند. محافظه‌کارانه
        # channels.getParticipant را پیش‌فرض خاموش می‌کنیم؛ فقط نتیجهٔ probe
        # موفق (ذخیره‌شده در routes) اجازهٔ فعال‌کردن این مسیر را می‌دهد.
        routes[ROUTE_PARTICIPANT] = False
        routes[ROUTE_BOT_API] = bool(bot_api_token())
    return routes


def route_order(routes) -> list:
    order = []
    # ترتیب = ارزان‌ترین/دقیق‌ترین اول. «پیمایش هدفمند» بعد از جست‌وجو و
    # قبل از اسنپ‌شات است: مثل همان یک RPC برای کانال کوچک، ولی حکم قطعی
    # («عضو نیست») هم می‌دهد.
    for route in (ROUTE_PARTICIPANT, ROUTE_BOT_API, ROUTE_SEARCH, ROUTE_SCAN,
                  ROUTE_SNAPSHOT):
        if routes.get(route):
            order.append(route)
    return order


def merge_cursor(group_id, extra: dict, *, merge: bool = True) -> dict:
    """به‌روزرسانی امن کرسر JSON (کلیدهای دیگر مثل ``routes`` حفظ می‌شوند).

    باگ واقعی: نوشتن کرسر با یک dict تازه، ``routes`` را پاک می‌کرد و
    کانال بی‌دلیل «بدون مسیر» می‌شد.
    """
    record = storage.get_group(group_id)
    if record is None:
        return {}
    cursor = dict(record.get("last_update_cursor") or {})
    cursor.update(extra or {})
    try:
        storage.set_sync_state(group_id, cursor=cursor)
    except Exception:
        return cursor
    return cursor


def save_channel_routes(group_id, routes: dict, *, merge: bool = True) -> None:
    """ثبت پایدار مسیرهای سالم یک گروه (کرسر JSON محلی)."""
    record = storage.get_group(group_id)
    if record is None:
        return
    cursor = dict(record.get("last_update_cursor") or {})
    current = dict(cursor.get("routes") or {}) if merge else {}
    for route in (ROUTE_PARTICIPANT, ROUTE_BOT_API, ROUTE_SEARCH, ROUTE_SCAN,
                  ROUTE_SNAPSHOT):
        if route in routes:
            current[route] = bool(routes[route])
    merge_cursor(group_id, {"routes": current})
    refresh_group_cache(group_id)


def disable_route(bot, record, route, reason) -> None:
    """مسیری که سرور جواب نمی‌دهد را برای همیشه کنار می‌گذاریم — با اعلام."""
    routes = channel_routes(record)
    if not routes.get(route):
        return
    routes[route] = False
    save_channel_routes(record["group_id"], routes)
    _log(bot, f"ROUTE DISABLED {ROUTE_LABELS.get(route, route)} "
              f"group_id={record['group_id']} reason={reason}", error=True)


# --- تنظیمات اسنپ‌شات -------------------------------------------------------

def scan_max_members() -> int:
    """سقف تعداد عضوی که مسیر «پیمایش هدفمند» می‌بیند (۰ = بی‌سقف)."""
    return _env_int("FORCE_JOIN_SCAN_MAX_MEMBERS", 5000, 0, 1_000_000)


def scan_budget() -> float:
    return _env_float("FORCE_JOIN_SCAN_BUDGET", 20.0, 1.0, 300.0)


def scan_delay() -> float:
    return _env_float("FORCE_JOIN_SCAN_DELAY", 0.1, 0.0, 5.0)


def scan_cooldown() -> float:
    """فاصلهٔ حداقلی دو پیمایش هدفمند روی یک کانال (ثانیه)."""
    return _env_float("FORCE_JOIN_SCAN_COOLDOWN", 5.0, 0.0, 300.0)


def notice_on_join() -> bool:
    return _env_flag("FORCE_JOIN_NOTICE_ON_JOIN", True)


def confirm_on_join() -> bool:
    """آیا پس از عضویتِ کاربر در کانال، پیام «تأیید شد» فرستاده شود؟"""
    return _env_flag("FORCE_JOIN_CONFIRM_JOIN", True)


def confirm_window() -> int:
    """چند ثانیه بعد از راهنما، منتظر عضویت کاربر بمانیم؟"""
    return _env_int("FORCE_JOIN_CONFIRM_WINDOW", 1800, 30, 86400)


def confirm_max_checks() -> int:
    return _env_int("FORCE_JOIN_CONFIRM_MAX_CHECKS", 20, 1, 200)


def confirm_interval() -> float:
    return _env_float("FORCE_JOIN_CONFIRM_INTERVAL", 30.0, 5.0, 3600.0)


def confirm_first_delay() -> float:
    """اولین بررسی بعد از ارسال راهنما — کوتاه، تا تأیید فوری باشد."""
    return _env_float("FORCE_JOIN_CONFIRM_FIRST_DELAY", 3.0, 1.0, 600.0)


def confirm_tick() -> float:
    """فاصلهٔ چرخهٔ ناظر تأیید (بیشتر از این، تأخید محسوس می‌شود)."""
    return _env_float("FORCE_JOIN_CONFIRM_TICK", 2.0, 0.5, 60.0)


#: زمان‌بندی تلاش‌ها (ثانیه) — سریع شروع می‌شود، بعد عقب می‌افتد.
_CONFIRM_BACKOFF = (3.0, 4.0, 6.0, 8.0, 12.0, 20.0, 30.0, 45.0, 60.0, 90.0,
                    120.0, 180.0, 300.0)


def confirm_backoff(try_index: int) -> float:
    index = max(0, min(int(try_index), len(_CONFIRM_BACKOFF) - 1))
    return _CONFIRM_BACKOFF[index]


# ---------------------------------------------------------------------------
# 🛡 محافظ کارایی: هیچ بررسی‌ای نباید ربات را کند کند
# ---------------------------------------------------------------------------

def rpc_per_minute() -> int:
    """سقف کل RPC ماژول در دقیقه (۰ = بی‌سقف). سهمیهٔ جدا از بقیهٔ ربات."""
    return _env_int("FORCE_JOIN_RPC_PER_MINUTE", 120, 0, 100_000)


def gate_scan_budget() -> float:
    """سقف زمانی پیمایش در **مسیر پیام** — کوتاه تا پیام کاربر معطل نماند."""
    return _env_float("FORCE_JOIN_GATE_SCAN_BUDGET", 1.5, 0.3, 10.0)


def gate_poll_scan_max_members() -> int:
    """در مسیر پیام، کانال بزرگ‌تر از این هرگز پیمایش نمی‌شود (می‌رود پس‌زمینه)."""
    return _env_int("FORCE_JOIN_GATE_SCAN_MAX_MEMBERS", 3000, 0, 1_000_000)


_rpc_window: list = []


def _rpc_budget_allows(cost: int = 1) -> bool:
    """token-bucket سادهٔ یک‌دقیقه‌ای؛ در نبود سهمیه، بررسی انجام **نمی‌شود**.

    این همان تضمین «ربات کند نمی‌شود» است: حتی اگر هزار کاربر هم‌زمان
    بیایند، سهمیهٔ این ماژول از بقیهٔ ربات جدا و محدود است.
    """
    limit = rpc_per_minute()
    if not limit:
        return True
    now = time.time()
    cutoff = now - 60.0
    while _rpc_window and _rpc_window[0] < cutoff:
        _rpc_window.pop(0)
    if len(_rpc_window) + max(1, cost) > limit:
        return False
    for _ in range(max(1, cost)):
        _rpc_window.append(now)
    return True


def snapshot_base_interval() -> int:
    return _env_int("FORCE_JOIN_SNAPSHOT_REFRESH", 300, 60, 86400)


def snapshot_interval_for(size) -> int:
    """فاصلهٔ تازه‌سازی اسنپ‌شات بر اساس تعداد اعضا (پله‌ای)."""
    base = snapshot_base_interval()
    size = _safe_int(size)
    if size <= 2000:
        return base
    if size <= 10000:
        return max(base, 900)
    if size <= 50000:
        return max(base, 3600)
    return max(base, 6 * 3600)


def snapshot_trust_seconds(members=None) -> int:
    """تا این سن، اسنپ‌شاتِ کامل برای «عضو نیست» معتبر است.

    اگر صریح تنظیم نشود **خودکار** از بازهٔ تازه‌سازی همان کانال حساب
    می‌شود (۲× بازه + ۶۰ ثانیه). این باگ واقعی بود: بازهٔ تازه‌سازی در حالت
    ``event`` دوازده ساعت بود ولی اعتبار اسنپ‌شات ۶۰۰ ثانیه، پس اسنپ‌شات
    عملاً **همیشه کهنه** بود و زنجیره هر بار به «نامعلوم» می‌رسید.
    """
    explicit = _env_int("FORCE_JOIN_SNAPSHOT_TRUST", 0, 0, 86400)
    if explicit:
        return explicit
    return max(600, 2 * snapshot_interval_for(members) + 60)


def snapshot_state(record) -> dict:
    cursor = record.get("last_update_cursor") or {}
    if not isinstance(cursor, dict):
        cursor = {}
    completed = cursor.get("snapshot_completed_at")
    started = cursor.get("snapshot_started_at")
    complete = bool(cursor.get("complete")) and bool(completed)
    age = (time.time() - float(completed)) if complete else None
    return {
        "complete": complete,
        "completed_at": float(completed) if completed else None,
        "started_at": float(started) if started else None,
        "age": age,
        "members": _safe_int(cursor.get("members")),
        "reason": cursor.get("reason"),
    }


def snapshot_membership(record, user_id):
    """حکم بر پایهٔ اسنپ‌شات کامل و تازه. ``(status|None, reason)``."""
    state = snapshot_state(record)
    if not state["complete"]:
        return None, "snapshot_incomplete"
    if state["age"] is None or \
            state["age"] > snapshot_trust_seconds(state.get("members")):
        return None, "snapshot_stale"
    stored = storage.get_member(record["channel_id"], user_id)
    verified = float((stored or {}).get("last_verified_at") or 0)
    if stored and stored.get("membership_status") == storage.STATUS_MEMBER \
            and state["started_at"] and verified >= state["started_at"]:
        return storage.STATUS_MEMBER, "snapshot"
    if stored and float((stored or {}).get("join_date") or 0) and \
            float(stored.get("join_date") or 0) >= (state["started_at"] or 0):
        # ورود بعد از شروع اسنپ‌شات → در فهرست نیست ولی عضو است.
        return storage.STATUS_MEMBER, "snapshot_join_date"
    if not stored or stored.get("membership_status") != storage.STATUS_MEMBER:
        return storage.STATUS_NOT_MEMBER, "snapshot"
    return None, "snapshot_conflict"


MAX_PENDING_CHECKS = _env_int("FORCE_JOIN_MAX_PENDING", 200, 10, 5000)

# ---------------------------------------------------------------------------
# وضعیت درون‌حافظه
# ---------------------------------------------------------------------------

#: کلید گروه → رکورد کانال (بدون I/O در مسیر پیام)
_group_cache: dict = {}
#: کلید گروه → زمان آخرین خواندن از دیتابیس (شامل کش منفی)
_group_cache_at: dict = {}
#: channel_id → کلیدهای گروه‌هایی که این کانال را اجباری کرده‌اند
_channel_index: dict = {}
_index_at: float = 0.0
#: مدت اعتبار «این گروه کانال اجباری ندارد» تا مسیر پیام SQLite نزند.
_NEGATIVE_CACHE_TTL = 60.0

#: (channel_id, user_id) → (status, verified_at_epoch)
_member_cache: dict = {}
#: اسنپ‌شات کاملِ کوتاه‌عمر برای جواب‌دادن به کاربران دیگر همان کانال.
#: channel_key → (verified_at_epoch, set(member_ids)); فقط بعد از اسکن کامل پر می‌شود.
_full_scan_cache: dict = {}
_FULL_SCAN_CACHE_TTL = 45.0
_FULL_SCAN_CACHE_MAX = 256
#: (channel_id, user_id) → Future در حال اجرا (single-flight)
_inflight: dict = {}
#: (group_key, user_id) → زمان آخرین پیام راهنما
_notice_sent: dict = {}
#: chat key → deque زمان ارسال پیام راهنما در یک دقیقهٔ اخیر
_notice_window: dict = {}
#: chat key → زمان پایان ممنوعیت FloodWait
_flood_until: dict = {}
#: قفل تک‌اجرایی همگام‌سازی هر گروه
_sync_locks: dict = {}
_pending_prefetch: dict = {}
_semaphore: Optional[asyncio.Semaphore] = None
_registered_bots: set = set()

STATS = {
    "decisions": 0,
    "allowed": 0,
    "blocked": 0,
    "unknown_allowed": 0,
    "cache_hits": 0,
    "db_hits": 0,
    "api_checks": 0,
    "api_errors": 0,
    "notices": 0,
    "deletes": 0,
    "events_join": 0,
    "events_leave": 0,
    "scans": 0,
    "snapshot_decisions": 0,
    "deferred": 0,
    # چند بررسیِ هم‌زمان به همان پیمایشِ در جریان چسبیدند (RPCِ کمتر)
    "scan_coalesced": 0,
}


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max_concurrency())
    return _semaphore


def _log(bot, message: str, *, error: bool = False) -> None:
    logger = getattr(bot, "logger", None)
    try:
        if error:
            logger.log_error(f"FORCE JOIN {message}")
        else:
            logger.log_info(f"FORCE JOIN {message}")
    except Exception:
        pass


def _display_name(user) -> Optional[str]:
    if user is None:
        return None
    username = getattr(user, "username", None)
    if username:
        return "@" + str(username).lstrip("@")
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    name = (first + " " + last).strip()
    return name or None


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# کش تنظیمات گروه
# ---------------------------------------------------------------------------

def _rebuild_channel_index() -> None:
    """ایندکس کانال → گروه‌ها تا رویدادهای کانال به دیتابیس نزنند."""
    global _index_at
    index = {}
    for key, record in _group_cache.items():
        index.setdefault(int(record["channel_id"]), []).append(key)
    _channel_index.clear()
    _channel_index.update(index)
    _index_at = time.time()


def refresh_group_cache(group_id=None) -> Optional[dict]:
    """رکورد کانال را از SQLite در کش حافظه تازه می‌کند."""
    if group_id is None:
        _group_cache.clear()
        _group_cache_at.clear()
        for record in storage.list_groups():
            key = str(record["group_id"])
            _group_cache[key] = record
            _group_cache_at[key] = time.time()
        _rebuild_channel_index()
        return None
    key = canonical_group_key_str(group_id)
    record = storage.get_group(key)
    if record is None:
        _group_cache.pop(key, None)
    else:
        _group_cache[key] = record
    # حتی «ندارد» هم کش می‌شود تا مسیر هر پیام SQLite نخواند.
    _group_cache_at[key] = time.time()
    _rebuild_channel_index()
    return record


def groups_cached_for_channel(channel_id) -> list:
    """گروه‌های این کانال از حافظه (بدون I/O در مسیر رویداد)."""
    cid = channel_key(channel_id)
    keys = _channel_index.get(cid)
    if keys is None and (time.time() - _index_at) > _NEGATIVE_CACHE_TTL:
        try:
            refresh_group_cache()
        except Exception:
            keys = None
        keys = _channel_index.get(cid)
    if not keys:
        return []
    return [_group_cache[key] for key in keys if key in _group_cache]


def cached_group(group_id) -> Optional[dict]:
    """رکورد کانال از حافظه (بدون I/O) — تنها تابعی که مسیر پیام صدا می‌زند."""
    key = canonical_group_key_str(group_id)
    if not key or key == "0":
        return None
    record = _group_cache.get(key)
    if record is not None:
        return record
    if (time.time() - _group_cache_at.get(key, 0.0)) < _NEGATIVE_CACHE_TTL:
        return None
    try:
        return refresh_group_cache(key)
    except Exception:
        return None


def is_enabled_cached(group_id) -> bool:
    """آیا برای این گروه عضویت اجباری فعال است؟ (بدون API، تقریباً بدون I/O)"""
    record = cached_group(group_id)
    return bool(record and record.get("force_join_enabled"))


def enabled_group_ids() -> list:
    refresh_group_cache()
    return [int(key) for key, record in _group_cache.items()
            if record.get("force_join_enabled")]


# ---------------------------------------------------------------------------
# ورودی کانال/کاربر
# ---------------------------------------------------------------------------

async def ensure_channel_input(bot, record, *, allow_resolve: bool = True):
    """``InputChannel`` از ``access_hash`` ذخیره‌شده — در حالت عادی صفر RPC.

    فقط اگر ``access_hash`` در دیتابیس نباشد (نخستین اجرا یا کش پاک‌شده)
    یک resolve انجام و نتیجه پایدار ذخیره می‌شود.
    """
    channel_id = record["channel_id"]
    access_hash = record.get("channel_access_hash")
    if access_hash is not None:
        peer = api.input_channel(channel_id, access_hash)
        if peer is not None:
            return peer
    # کش peer خود کلاینت (بدون RPC در اکثر موارد)
    client = getattr(bot, "client", None)
    cache = getattr(bot, "reply_input_peer_cache", None) or {}
    for candidate in (canonical_group_key(channel_id),
                      -channel_id, channel_id):
        try:
            cached = cache.get(candidate)
        except Exception:
            cached = None
        if cached is not None and api.channel_access_hash(cached) is not None:
            peer = api.input_channel(channel_id,
                                     api.channel_access_hash(cached))
            if peer is not None:
                try:
                    storage.set_channel_access_hash(
                        channel_id, api.channel_access_hash(cached)
                    )
                except Exception:
                    pass
                return peer
    if not allow_resolve or client is None:
        return None
    resolved = await api.resolve_channel(client, channel_id,
                                         timeout=resolve_timeout())
    if not resolved.get("ok"):
        _log(bot, f"RESOLVE FAILED channel_id={channel_id} "
                  f"reason={resolved.get('reason')}", error=True)
        return None
    access_hash = resolved.get("access_hash")
    if access_hash is not None:
        try:
            storage.set_channel_access_hash(channel_id, access_hash)
            refresh_group_cache(record["group_id"])
        except Exception:
            pass
    return api.input_channel(channel_id, access_hash)


def _peer_hash_ok(peer) -> bool:
    """آیا این InputPeer شناسهٔ قابل‌استفاده دارد؟ (``access_hash`` صفر بی‌فایده است)"""
    value = getattr(peer, "access_hash", None)
    try:
        return value is not None and int(value) != 0
    except (TypeError, ValueError):
        return False


def _search_queries(user_entity) -> list:
    """عبارت‌های جست‌وجو برای پیدا کردن آبجکت کامل کاربر در گروه."""
    queries = []
    username = getattr(user_entity, "username", None)
    if username:
        queries.append(str(username).lstrip("@"))
    first = (getattr(user_entity, "first_name", None) or "").strip()
    last = (getattr(user_entity, "last_name", None) or "").strip()
    if first:
        queries.append(first)
    if first and last:
        queries.append(f"{first} {last}")
    return [q for q in queries if q]


async def resolve_user_input(bot, group_chat_id, user_id, user_entity=None,
                             stored_hash=None):
    """``InputPeerUser`` کاربر با زنجیرهٔ لایه‌ای — خروجی ``(peer, source)``.

    ترتیب (ارزان‌ترین اول):

    1. آبجکت همراه پیام/رویداد (بدون هیچ RPC)
    2. ``access_hash`` ذخیره‌شده در SQLite (بدون RPC)
    3. کش entity خود کلاینت
    4. **جست‌وجوی هدفمند با limit کوچک** در همان گروه (چون کاربر در گروه
       هست و پیام داده) — تنها راه وقتی رویداد، ``access_hash`` نداده باشد
    5. ``client.get_entity`` به‌عنوان آخرین تلاش

    خروجی ``(None, "unresolved")`` یعنی بررسی ممکن نیست (⇒ unknown ⇒
    fail-open و پیام حذف نمی‌شود)، نه «عضو نیست».
    """
    peer = api.input_user(user_entity)
    if peer is not None and _peer_hash_ok(peer):
        return peer, "message_object"
    if stored_hash:
        peer = api.input_user_parts(user_id, stored_hash)
        if peer is not None and _peer_hash_ok(peer):
            return peer, "db_access_hash"

    client = getattr(bot, "client", None)
    if client is None:
        return None, "no_client"

    try:
        cached = await asyncio.wait_for(
            client.get_input_entity(int(user_id)), timeout=resolve_timeout()
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        cached = None
    cached_peer = api.input_user(cached) if cached is not None else None
    if cached_peer is not None and _peer_hash_ok(cached_peer):
        return cached_peer, "client_cache"

    # جست‌وجوی هدفمند در گروه (سقف کوچک) برای گرفتن access_hash واقعی
    queries = _search_queries(user_entity)
    if queries:
        group_input = await _group_input_peer(bot, group_chat_id)
        if group_input is not None:
            for query in queries[:2]:
                found = await api.search_participants(
                    client, group_input, query, user_id, limit=20,
                    timeout=api_timeout(),
                )
                user = found.get("user")
                if user is not None:
                    peer = api.input_user(user)
                    if peer is not None and _peer_hash_ok(peer):
                        return peer, "group_search"

    try:
        entity = await asyncio.wait_for(
            client.get_entity(int(user_id)), timeout=resolve_timeout()
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        entity = None
    peer = api.input_user(entity) if entity is not None else None
    if peer is not None and _peer_hash_ok(peer):
        return peer, "get_entity"
    return None, "unresolved"


async def _group_input_peer(bot, chat_id):
    """``InputChannel``/``InputPeer`` همان گروه — از کش، بدون RPC در حالت گرم."""
    cache = getattr(bot, "reply_input_peer_cache", None) or {}
    for candidate in (chat_id, canonical_group_key(chat_id),
                      channel_key(chat_id)):
        try:
            peer = cache.get(candidate)
        except Exception:
            peer = None
        if peer is not None:
            return peer
    client = getattr(bot, "client", None)
    if client is None:
        return None
    for candidate in (canonical_group_key(chat_id), chat_id):
        try:
            return await asyncio.wait_for(
                client.get_input_entity(candidate), timeout=resolve_timeout()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# بررسی عضویت یک کاربر
# ---------------------------------------------------------------------------

def _fast_ttl_active(record=None) -> bool:
    """TTL کوتاه فقط وقتی معنی دارد که بررسی ارزان باشد.

    اگر فقط «اسکنِ کامل» در دسترس است، TTL کوتاه تعدادِ اسکن را چند برابر
    می‌کند، پس همان TTLهای فعلی می‌مانند (هدف: کم‌کردنِ درخواست‌ها).
    """
    if not cache_enabled():
        return False
    if fast_ttl_always():
        return True
    if record is None:
        return False
    try:
        return bool(channel_routes(record).get(ROUTE_PARTICIPANT))
    except Exception:
        return False


def _ttl_for(status: str, record=None) -> int:
    if _fast_ttl_active(record):
        return (fast_member_ttl() if status == storage.STATUS_MEMBER
                else fast_not_member_ttl())
    return member_ttl() if status == storage.STATUS_MEMBER else not_member_ttl()


def _remember(channel_id, user_id, status, verified_at):
    _member_cache[(channel_id, user_id)] = (status, verified_at)


def _completed_scan_status(record, user_id, now=None):
    """Use only a fresh, complete member set; partial/error scans never decide absence."""
    now = time.time() if now is None else now
    key = channel_key(record["channel_id"])
    entry = _full_scan_cache.get(key)
    if not entry:
        return None
    verified_at, member_ids = entry
    age = now - verified_at
    if age > _FULL_SCAN_CACHE_TTL:
        _full_scan_cache.pop(key, None)
        return None
    if _safe_int(user_id) in member_ids:
        return storage.STATUS_MEMBER
    # The shared snapshot lives 30–60s, but absence still has a shorter TTL.
    if age > _ttl_for(storage.STATUS_NOT_MEMBER, record):
        return None
    return storage.STATUS_NOT_MEMBER


def forget_user(channel_id, user_id) -> None:
    """باطل‌سازی فوری کش حافظه (رویداد خروج/ورود از همین استفاده می‌کند)."""
    _member_cache.pop((channel_key(channel_id), _safe_int(user_id)), None)
    # Membership changed after the snapshot: it must not answer for other users.
    _full_scan_cache.pop(channel_key(channel_id), None)


async def _api_check(bot, record, user_id, user_entity=None):
    """یک فراخوانی ``channels.getParticipant`` + ثبت پایدار نتیجه.

    همهٔ جزئیات (منبع ``InputPeer``، متن خطا، زمان) در ``_last_diag`` نگه
    داشته می‌شود تا فرمان «تست عضویت» بتواند دقیقاً بگوید کجای زنجیره
    شکسته است — نه اینکه بی‌صدا fail-open شود.
    """
    channel_id = record["channel_id"]
    STATS["api_checks"] += 1
    diag = {
        "at": time.time(),
        "user_id": user_id,
        "channel_id": channel_id,
        "group_id": record["group_id"],
        "input_source": None,
        "channel_input": False,
        "state": None,
        "error": None,
    }
    channel_input = await ensure_channel_input(bot, record)
    diag["channel_input"] = channel_input is not None
    if channel_input is None:
        STATS["api_errors"] += 1
        diag.update(state="error", error="channel_unresolved")
        _record_last_error(channel_id, "channel_unresolved")
        _last_diag[(channel_key(channel_id), _safe_int(user_id))] = diag
        return storage.STATUS_UNKNOWN, api.RESULT_ERROR, None

    stored = storage.get_member(channel_id, user_id)
    user_input, source = await resolve_user_input(
        bot, record["group_id"], user_id, user_entity,
        stored_hash=(stored or {}).get("access_hash"),
    )
    diag["input_source"] = source
    if user_input is None:
        STATS["api_errors"] += 1
        diag.update(state="error", error="user_input_unresolved")
        _record_last_error(channel_id, "user_input_unresolved")
        _last_diag[(channel_key(channel_id), _safe_int(user_id))] = diag
        _log(bot, f"USER INPUT UNRESOLVED user_id={user_id} "
                  f"channel_id={channel_id} (fail-open)", error=True)
        return storage.STATUS_UNKNOWN, api.RESULT_ERROR, None

    # کش پایدار access_hash همان کاربر برای بررسی‌های بعدی (یک فراخوانی)
    found_hash = getattr(user_input, "access_hash", None)
    if found_hash:
        try:
            storage.set_member_access_hash(channel_id, user_id, found_hash)
        except Exception:
            pass

    result = await api.get_participant(
        getattr(bot, "client", None), channel_input, user_input,
        timeout=api_timeout(),
    )
    state = result["state"]
    diag["state"] = state
    _last_diag[(channel_key(channel_id), _safe_int(user_id))] = diag

    if state == api.RESULT_MEMBER:
        storage.set_member(
            channel_id, user_id, storage.STATUS_MEMBER, source=storage.SOURCE_RPC,
            access_hash=found_hash or getattr(user_entity, "access_hash", None),
            join_date=result.get("join_date"),
        )
        _remember(channel_id, user_id, storage.STATUS_MEMBER, time.time())
        _record_decision(record["group_id"], user_id, "member", "api", source)
        return storage.STATUS_MEMBER, state, result

    if state == api.RESULT_NOT_MEMBER:
        storage.set_member(
            channel_id, user_id, storage.STATUS_NOT_MEMBER,
            source=storage.SOURCE_RPC,
            access_hash=found_hash or getattr(user_entity, "access_hash", None),
        )
        _remember(channel_id, user_id, storage.STATUS_NOT_MEMBER, time.time())
        _record_decision(record["group_id"], user_id, "not_member", "api", source)
        return storage.STATUS_NOT_MEMBER, state, result

    error = result.get("error")
    diag["error"] = f"{type(error).__name__}: {error}" if error else state
    _record_last_error(channel_id, diag["error"])
    if state == api.RESULT_NOT_SUPPORTED:
        # سروش این متد را ندارد؛ نه خطای شبکه است نه مشکل access_hash.
        _log(bot, f"NOT SUPPORTED channels.getParticipant "
                  f"channel_id={channel_id} (مسیر بعدی امتحان می‌شود)")
        _record_decision(record["group_id"], user_id, "unknown", "api",
                         "not_supported")
        return storage.STATUS_UNKNOWN, state, result
    if state == api.RESULT_FLOOD:
        seconds = result.get("seconds") or 60.0
        _flood_until[channel_id] = time.time() + float(seconds)
        _log(bot, f"FLOOD channel_id={channel_id} seconds={seconds:g}",
             error=True)
    elif state == api.RESULT_NO_ACCESS:
        _log(bot, f"NO ACCESS user_id={user_id} channel_id={channel_id} "
                  "(فهرست اعضا مخفی است یا حساب در کانال ادمین/عضو نیست)",
             error=True)
    else:
        _log(bot, f"CHECK ERROR user_id={user_id} channel_id={channel_id} "
                  f"error={diag['error']}", error=True)
    STATS["api_errors"] += 1
    _record_decision(record["group_id"], user_id, "unknown", "api", diag["error"])
    return storage.STATUS_UNKNOWN, state, result


# ---------------------------------------------------------------------------
# اجرای زنجیرهٔ مسیرها
# ---------------------------------------------------------------------------

#: group_key → Task تازه‌سازی اسنپ‌شات در پس‌زمینه
_refresh_tasks: dict = {}


def _store_verdict(record, user_id, status, source, *, access_hash=None,
                   join_date=None, layer="api", detail=None) -> None:
    storage.set_member(
        record["channel_id"], user_id, status, source=source,
        access_hash=access_hash, join_date=join_date,
    )
    _remember(record["channel_id"], user_id, status, time.time())
    _record_decision(record["group_id"], user_id, status, layer, detail)


async def _bot_api_route(record, user_id):
    """مسیر ``getChatMember`` (Bot API) — یک درخواست HTTP، بدون پیمایش."""
    result = await api.bot_api_get_chat_member(
        bot_api_token(), record["channel_id"], user_id,
        timeout=min(api_timeout(), max(2.0, gate_await_timeout() - 1.0)),
    )
    state = result.get("state")
    if state == "member":
        return storage.STATUS_MEMBER, "member", result
    if state == "not_member":
        return storage.STATUS_NOT_MEMBER, "not_member", result
    return storage.STATUS_UNKNOWN, state or "error", result


def _search_query(user_entity) -> Optional[str]:
    if user_entity is None:
        return None
    for attr in ("username", "usernames", "first_name", "last_name"):
        value = getattr(user_entity, attr, None)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if hasattr(value, "username"):
            value = getattr(value, "username", None)
        text = str(value or "").strip().lstrip("@")
        if text and len(text) <= 32:
            return text
    return None


async def _search_route(bot, record, user_id, user_entity=None):
    """جست‌وجوی هدفمند نام کاربر.

    فقط «پیدا شد» قطعی است (کاربر عضو است). «پیدا نشد» **نیست**، چون
    نتیجه ممکن است وارد صفحهٔ اول نشده باشد.
    """
    query = _search_query(user_entity)
    if not query:
        return storage.STATUS_UNKNOWN, "no_query", None
    channel_input = await ensure_channel_input(bot, record)
    client = getattr(bot, "client", None)
    if client is None or channel_input is None:
        return storage.STATUS_UNKNOWN, "no_access", None
    found = await api.search_participants(
        client, channel_input, query, user_id, limit=20,
        timeout=api_timeout(),
    )
    state = found.get("state")
    if state == "found":
        user = found.get("user")
        return storage.STATUS_MEMBER, state, {
            "user": user,
            "access_hash": getattr(user, "access_hash", None),
        }
    if state in (api.RESULT_NO_ACCESS,):
        return storage.STATUS_UNKNOWN, state, None
    return storage.STATUS_UNKNOWN, state or "absent", None


async def _scan_route(bot, record, user_id, *, inline=False):
    """پیمایش هدفمند فهرست اعضا تا همین یک کاربر (حکم قطعی روی سروش).

    محدودیت‌ها: سقف تعداد عضو، بودجهٔ زمانی، و cooldown هر کانال.
    ``inline=True`` یعنی در **مسیر پیام** هستیم؛ پس بودجه کوتاه است
    (۱٫۵ ثانیه) و کانال بزرگ هرگز همان‌جا پیمایش نمی‌شود تا پیام کاربر
    معطل نماند. اگر پیمایش کامل نشود «نامعلوم» است، نه «غیرعضو».
    """
    channel_id = record["channel_id"]
    key = channel_key(channel_id)
    now = time.time()
    if now < _scan_until.get(key, 0.0):
        return storage.STATUS_UNKNOWN, "cooldown", None
    if inline:
        state = snapshot_state(record)
        if state.get("members") and state["members"] > gate_poll_scan_max_members():
            # کانال بزرگ: پیمایش در پس‌زمینه، نه در مسیر پیام
            _schedule_snapshot_refresh(bot, record)
            return storage.STATUS_UNKNOWN, "deferred_big_channel", None
    if not _rpc_budget_allows(1):
        return storage.STATUS_UNKNOWN, "budget_exhausted", None
    channel_input = await ensure_channel_input(bot, record)
    client = getattr(bot, "client", None)
    if client is None or channel_input is None:
        return storage.STATUS_UNKNOWN, "no_access", None
    budget = (gate_scan_budget() if inline
              else min(scan_budget(), max(2.0, gate_await_timeout() - 1.0)))
    _scan_until[key] = now + scan_cooldown()
    STATS["scans"] += 1
    found = await api.find_member(
        client, channel_input, user_id,
        max_members=scan_max_members(),
        budget_seconds=budget,
        delay=scan_delay(),
    )
    state = found.get("state")
    # Every successfully read page is positive evidence for its listed users.
    # A complete pass additionally gives a short-lived answer for every ID in
    # this channel, including negatives. Errors/partial passes are never cached.
    scanned_ids = found.get("member_ids") or ()
    verified_at = time.time()
    for scanned_id in scanned_ids:
        _remember(channel_id, _safe_int(scanned_id), storage.STATUS_MEMBER,
                  verified_at)
    if state == "found":
        position = _safe_int(found.get("position"))
        level = (20 if position and position <= 20 else
                 100 if position and position <= 100 else
                 500 if position and position <= 500 else "full")
        _log(bot, f"MEMBER CHECK progressive chat={record['group_id']} "
                  f"user={user_id} level={level} rpc_count={found.get('pages', 0)}")
        return storage.STATUS_MEMBER, state, found
    if state == "absent" and found.get("complete"):
        member_ids = {_safe_int(value) for value in scanned_ids}
        _full_scan_cache[channel_key(channel_id)] = (verified_at, member_ids)
        if len(_full_scan_cache) > _FULL_SCAN_CACHE_MAX:
            oldest = min(_full_scan_cache.items(),
                         key=lambda item: item[1][0])[0]
            _full_scan_cache.pop(oldest, None)
        _log(bot, f"MEMBER CHECK full scan done chat={record['group_id']} "
                  f"members_count={len(member_ids)}")
        _log(bot, f"MEMBER CHECK progressive chat={record['group_id']} "
                  f"user={user_id} level=full rpc_count={found.get('pages', 0)}")
        return storage.STATUS_NOT_MEMBER, state, found
    # ناقص/خطا → اسنپ‌شات را در پس‌زمینه تازه کن تا پیام بعدی حکم بگیرد.
    _schedule_snapshot_refresh(bot, record)
    return storage.STATUS_UNKNOWN, state or found.get("reason") or "partial", found


def _snapshot_route(bot, record, user_id):
    status, reason = snapshot_membership(record, user_id)
    if status is not None:
        return status, reason, None
    _schedule_snapshot_refresh(bot, record)
    return storage.STATUS_UNKNOWN, reason, None


def _schedule_snapshot_refresh(bot, record) -> bool:
    """تازه‌سازی اسنپ‌شات در پس‌زمینه (بی‌مسدودسازی مسیر پیام)."""
    if not _env_flag("FORCE_JOIN_BACKGROUND_REFRESH", True):
        return False
    if not channel_routes(record).get(ROUTE_SNAPSHOT):
        return False
    key = str(record["group_id"])
    running = _refresh_tasks.get(key)
    if running is not None and not running.done():
        return False
    if not needs_sync(record):
        return False

    async def runner():
        try:
            await snapshot_sync(bot, record["group_id"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log(bot, f"SNAPSHOT REFRESH FAILED group_id={key} "
                      f"error={type(error).__name__}", error=True)

    try:
        task = asyncio.ensure_future(runner())
    except Exception:
        return False
    _refresh_tasks[key] = task
    return True


#: (channel_key, user_id) → Task پیمایشِ در جریان (single-flightِ مسیرِ اسکن)
_scan_inflight: dict = {}
#: channel_id → تا این زمان مسیرِ تک‌کاربره دوباره امتحان نمی‌شود
_cheap_probe_until: dict = {}


def _cheap_probe_allowed(channel_id) -> bool:
    """آیا امتحان‌کردنِ مسیرِ تک‌کاربره الان به‌صرفه است؟ (cooldown بعد از NOT_SUPPORTED)"""
    if not cache_enabled() or not cheap_route_enabled():
        return False
    try:
        return time.time() >= _cheap_probe_until.get(channel_id, 0.0)
    except Exception:
        return False


async def _scan_single_flight(bot, record, user_id):
    """همان پیمایش برای همان کاربر فقط یک‌بار اجرا شود؛ بقیه منتظرِ همان نتیجه.

    چرا امن: همهٔ منتظران **همان حکمِ** اجرای اصلی را می‌گیرند (نه حکمِ
    دیگری)، پس تصمیمِ gate دقیقاً عوض نمی‌شود؛ فقط RPCِ تکراری حذف می‌شود.
    """
    if not cache_enabled():
        return await _scan_route(bot, record, user_id, inline=True)
    key = (channel_key(record["channel_id"]), _safe_int(user_id))
    running = _scan_inflight.get(key)
    if running is not None:
        STATS["scan_coalesced"] += 1
        _log(bot, f"MEMBER CHECK scan coalesced chat={record['group_id']} "
                  f"user={user_id}")
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            raise
        except Exception:
            return storage.STATUS_UNKNOWN, "inflight_error", None

    task = asyncio.ensure_future(
        _scan_route(bot, record, user_id, inline=True)
    )
    _scan_inflight[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _scan_inflight.get(key) is task:
            _scan_inflight.pop(key, None)


async def _routed_check(bot, record, user_id, user_entity=None):
    """زنجیرهٔ مسیرها: اولین پاسخ قطعی برنده است؛ هر شکست دیده می‌شود.

    خروجی: ``(status, layer, diag)``
    """
    channel_id = record["channel_id"]
    routes = channel_routes(record)
    order = route_order(routes)
    diag = {
        "at": time.time(),
        "user_id": user_id,
        "channel_id": channel_id,
        "group_id": record["group_id"],
        "routes": order,
        "trace": [],
        "state": None,
        "route": None,
        "error": None,
    }
    key = (channel_key(channel_id), _safe_int(user_id))

    # مسیرِ تک‌کاربره (GetParticipant) اول امتحان شود، حتی اگر probe قدیمی
    # آن را خاموش کرده باشد؛ نتیجه‌اش همان زنجیرهٔ فعلی را طی می‌کند و اگر
    # سرور پشتیبانی نکند، با cooldownِ بلند کنار گذاشته می‌شود.
    # فقط وقتی حداقل یک مسیرِ سالم ثبت شده است: اگر مالک همه را خاموش کرده
    # («no_route»)، دستِ خودش را عوض نمی‌کنیم.
    if order and ROUTE_PARTICIPANT not in order and _cheap_probe_allowed(channel_id):
        order.insert(0, ROUTE_PARTICIPANT)
        _log(bot, f"CHEAP ROUTE probe user_id={user_id} channel_id={channel_id}")

    if not order:
        diag.update(state="no_route", error="no_route")
        _last_diag[key] = diag
        _record_last_error(
            channel_id,
            "no_route: هیچ مسیر سالمی برای بررسی عضویت ثبت نشده است "
            "(«تست عضویت» را اجرا کنید)",
        )
        _log(bot, f"NO ROUTE user_id={user_id} channel_id={channel_id}",
             error=True)
        return storage.STATUS_UNKNOWN, "no_route", diag

    STATS["api_checks"] += 1
    for route in order:
        try:
            if route == ROUTE_PARTICIPANT:
                status, raw_state, detail = await _single_flight_check(
                    bot, record, user_id, user_entity
                )
                diag["trace"].append({route: raw_state})
                if status in (storage.STATUS_MEMBER, storage.STATUS_NOT_MEMBER):
                    diag.update(route=route, state=raw_state)
                    _last_diag[key] = diag
                    return status, "api", diag
                error = detail.get("error") if isinstance(detail, dict) else None
                if api.is_not_supported(error) or \
                        raw_state == api.RESULT_NOT_SUPPORTED:
                    disable_route(bot, record, route, "NOT_SUPPORTED")
                    if route == ROUTE_PARTICIPANT:
                        # همین مسیر ارزان پشتیبانی نمی‌شود: تا یک ساعت
                        # دوباره امتحان نشود (هیچ RPCِ بی‌نتیجه‌ای).
                        _cheap_probe_until[channel_id] = (
                            time.time() + cheap_route_retry_seconds()
                        )
                    record = storage.get_group(record["group_id"]) or record
                continue

            if route == ROUTE_BOT_API:
                status, outcome, payload = await _bot_api_route(record, user_id)
                diag["trace"].append({route: outcome})
                if status in (storage.STATUS_MEMBER, storage.STATUS_NOT_MEMBER):
                    _store_verdict(record, user_id, status,
                                   storage.SOURCE_BOT_API, layer="bot_api",
                                   detail=outcome)
                    diag.update(route=route, state=outcome)
                    _last_diag[key] = diag
                    return status, "bot_api", diag
                if outcome == "unsupported":
                    disable_route(bot, record, route, "NOT_SUPPORTED")
                    record = storage.get_group(record["group_id"]) or record
                elif outcome == "no_access":
                    _record_last_error(
                        channel_id,
                        "bot_api_no_access: رباتِ BOT_TOKEN ادمین این کانال نیست",
                    )
                continue

            if route == ROUTE_SEARCH:
                status, outcome, payload = await _search_route(
                    bot, record, user_id, user_entity
                )
                diag["trace"].append({route: outcome})
                if status == storage.STATUS_MEMBER:
                    _store_verdict(
                        record, user_id, status, storage.SOURCE_SEARCH,
                        access_hash=(payload or {}).get("access_hash"),
                        layer="search", detail=outcome,
                    )
                    diag.update(route=route, state=outcome)
                    _last_diag[key] = diag
                    return status, "search", diag
                if outcome == api.RESULT_NO_ACCESS:
                    disable_route(bot, record, route, "NO_ACCESS")
                    record = storage.get_group(record["group_id"]) or record
                continue

            if route == ROUTE_SCAN:
                status, outcome, payload = await _scan_single_flight(
                    bot, record, user_id
                )
                diag["trace"].append({route: outcome})
                if status in (storage.STATUS_MEMBER, storage.STATUS_NOT_MEMBER):
                    _store_verdict(
                        record, user_id, status, storage.SOURCE_SCAN,
                        access_hash=getattr((payload or {}).get("user"),
                                            "access_hash", None),
                        layer="scan", detail=outcome,
                    )
                    diag.update(route=route, state=outcome)
                    _last_diag[key] = diag
                    return status, "scan", diag
                if outcome == api.RESULT_NO_ACCESS:
                    disable_route(bot, record, route, "NO_ACCESS")
                    record = storage.get_group(record["group_id"]) or record
                continue

            if route == ROUTE_SNAPSHOT:
                status, outcome, _ = _snapshot_route(bot, record, user_id)
                diag["trace"].append({route: outcome})
                if status in (storage.STATUS_MEMBER, storage.STATUS_NOT_MEMBER):
                    _store_verdict(record, user_id, status,
                                   storage.SOURCE_SNAPSHOT,
                                   layer="snapshot", detail=outcome)
                    diag.update(route=route, state=outcome)
                    _last_diag[key] = diag
                    return status, "snapshot", diag
                continue
        except asyncio.CancelledError:
            raise
        except Exception as error:  # یک مسیر خراب، کل زنجیره را نمی‌شکند
            diag["trace"].append({route: f"exception:{type(error).__name__}"})
            continue

    diag.update(state="exhausted", error="no_route_conclusive")
    _last_diag[key] = diag
    _record_last_error(channel_id, "no_route_conclusive: هیچ مسیری پاسخ قطعی نداد")
    STATS["api_errors"] += 1
    _record_decision(record["group_id"], user_id, "unknown", "chain",
                     diag["trace"][-1] if diag["trace"] else "no_route")
    return storage.STATUS_UNKNOWN, "exhausted", diag


#: آخرین تصمیم‌ها برای گزارش «وضعیت کانال» (حلقهٔ کران‌دار)
_recent_decisions: list = []
#: channel_id → آخرین متن خطای بررسی (برای نمایش به مالک)
_last_error: dict = {}
#: (channel_id, user_id) → جزئیات آخرین تلاش API (برای «تست عضویت»)
_last_diag: dict = {}
#: channel_key → زمان پایان ممنوعیت پیمایش هدفمند (ضد سیل)
_scan_until: dict = {}
#: (group_key, user_id) → انتظار «تأیید عضویت» بعد از ارسال راهنما
_awaiting_confirm: dict = {}
#: پارامترهای تکرارِ بررسی برای همان انتظار
_CONFIRM_MAX_ENTRIES = 2000
#: channel_key → Task در حال پیمایش برای تأیید (یک پاس برای همهٔ منتظران)
_confirm_inflight: dict = {}
_RECENT_LIMIT = 25


def _record_decision(group_id, user_id, status, layer, detail=None) -> None:
    try:
        _recent_decisions.append({
            "at": time.time(),
            "group_id": group_id,
            "user_id": _safe_int(user_id),
            "status": status,
            "layer": layer,
            "detail": detail,
        })
        if len(_recent_decisions) > _RECENT_LIMIT:
            del _recent_decisions[:-_RECENT_LIMIT]
    except Exception:
        pass


def _record_last_error(channel_id, text) -> None:
    _last_error[channel_key(channel_id)] = (time.time(), str(text)[:180])


def recent_decisions(group_id=None, limit: int = 5) -> list:
    rows = [
        row for row in _recent_decisions
        if group_id is None or str(row["group_id"]) == str(canonical_group_key(group_id))
    ]
    return list(reversed(rows))[:limit]


def last_error(channel_id):
    return _last_error.get(channel_key(channel_id))


async def _single_flight_check(bot, record, user_id, user_entity=None):
    """هم‌زمانی کنترل‌شده: یک درخواست برای هر (کاربر، کانال)."""
    channel_id = record["channel_id"]
    key = (channel_id, user_id)
    running = _inflight.get(key)
    if running is not None:
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            raise
        except Exception:
            return storage.STATUS_UNKNOWN, api.RESULT_ERROR, None

    async def runner():
        async with _sem():
            return await _api_check(bot, record, user_id, user_entity)

    task = asyncio.ensure_future(runner())
    _inflight[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _inflight.get(key) is task:
            _inflight.pop(key, None)


async def resolve_status(bot, record, user_id, user_entity=None, *,
                         force: bool = False):
    """تصمیم عضویت با ارزان‌ترین لایهٔ ممکن.

    ``force=True`` یعنی کش حافظه/دیتابیس **نادیده** گرفته شود و همین حالا
    بررسی تازه انجام شود. برای «تأیید عضویت» و «تست عضویت» لازم است؛
    وگرنه حکمِ «غیرعضو» تا ۵ دقیقه در کش می‌ماند و کاربری که همین حالا
    عضو کانال شده، تأیید نمی‌گیرد.

    خروجی: ``(status, layer, detail)`` که ``status`` یکی از
    ``member`` / ``not_member`` / ``unknown`` و ``layer`` یکی از
    ``memory`` / ``db`` / ``api`` / ``grace`` / ``fail_open`` است.
    """
    channel_id = record["channel_id"]
    now = time.time()

    if force:
        _member_cache.pop((channel_id, user_id), None)

    entry = None if force else _member_cache.get((channel_id, user_id))
    if entry is not None:
        status, verified_at = entry
        if now - verified_at <= _ttl_for(status, record):
            STATS["cache_hits"] += 1
            _log(bot, f"MEMBER CHECK cache hit chat={record['group_id']} "
                      f"user={user_id} result={str(status == storage.STATUS_MEMBER).lower()}")
            _record_decision(record["group_id"], user_id, status, "memory")
            return status, "memory", None
        _member_cache.pop((channel_id, user_id), None)

    # A fresh complete scan is shared across all queried users in this channel.
    # This is distinct from targeted checks; no partial scan may infer absence.
    if not force:
        scanned_status = _completed_scan_status(record, user_id, now)
        if scanned_status is not None:
            _remember(channel_id, user_id, scanned_status, now)
            STATS["cache_hits"] += 1
            _log(bot, f"MEMBER CHECK cache hit chat={record['group_id']} "
                      f"user={user_id} result={str(scanned_status == storage.STATUS_MEMBER).lower()}")
            _record_decision(record["group_id"], user_id, scanned_status,
                             "full_scan_cache")
            return scanned_status, "full_scan_cache", None

    stored = storage.get_member(channel_id, user_id)
    if stored and stored.get("last_verified_at") and not force:
        age = now - float(stored["last_verified_at"])
        if age <= _ttl_for(stored["membership_status"], record):
            STATS["db_hits"] += 1
            _log(bot, f"MEMBER CHECK cache hit chat={record['group_id']} "
                      f"user={user_id} result={str(stored['membership_status'] == storage.STATUS_MEMBER).lower()}")
            _remember(channel_id, user_id, stored["membership_status"],
                      float(stored["last_verified_at"]))
            _record_decision(record["group_id"], user_id,
                             stored["membership_status"], "db")
            return stored["membership_status"], "db", stored
        # وضعیت کهنه ولی معلوم: برای سیاست grace نگه داشته می‌شود.
    else:
        age = None

    if now < _flood_until.get(channel_id, 0.0):
        # سرویس محدودیت گذاشته؛ اسنپ‌شات/بررسی را عقب می‌اندازیم.
        return _grace_decision(stored, age), "grace", stored

    try:
        status, layer, detail = await asyncio.wait_for(
            _routed_check(bot, record, user_id, user_entity),
            timeout=gate_await_timeout(),
        )
    except asyncio.TimeoutError:
        STATS["api_errors"] += 1
        _log(bot, f"CHECK TIMEOUT user_id={user_id} "
                  f"channel_id={channel_id} (fail-open)")
        return _grace_decision(stored, age), "fail_open", stored
    except asyncio.CancelledError:
        raise
    except Exception as error:
        STATS["api_errors"] += 1
        _log(bot, f"CHECK FAILED user_id={user_id} channel_id={channel_id} "
                  f"error={type(error).__name__} (fail-open)", error=True)
        return _grace_decision(stored, age), "fail_open", stored

    if status == storage.STATUS_UNKNOWN:
        return _grace_decision(stored, age), "grace", detail
    return status, layer or "api", detail


def _grace_decision(stored, age):
    """تصمیم در حالت «نمی‌دانیم»: شاهد تازهٔ «غیرعضو» اجرا می‌شود، بقیه آزاد.

    این همان fail-safe است: در قطعی API عضویت تأیید نمی‌شود و پیام کسی هم
    حذف نمی‌شود مگر شاهد واقعیِ تازه از خروج داشته باشیم.
    """
    if not stored:
        return storage.STATUS_UNKNOWN
    status = stored.get("membership_status")
    verified_at = stored.get("last_verified_at")
    if not verified_at:
        return storage.STATUS_UNKNOWN
    age = max(0.0, time.time() - float(verified_at))
    if status == storage.STATUS_NOT_MEMBER and \
            age <= not_member_grace_seconds():
        return storage.STATUS_NOT_MEMBER
    if status == storage.STATUS_MEMBER and age <= member_grace_seconds():
        return storage.STATUS_MEMBER
    return storage.STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# مسیر پیام: دروازهٔ عضویت
# ---------------------------------------------------------------------------

def local_exemption(bot, chat_id, user_id, user_entity=None):
    """معافیت‌های ارزان و محلی (بدون RPC).

    مالک ربات، مالک گروه، ادمین ثبت‌شده، VIP و (در صورت تنظیم) ربات‌ها.
    """
    try:
        from modules.owner_check import is_global_owner
        if is_global_owner(user_id):
            return "global_owner"
    except Exception:
        pass
    if user_id and user_id == _safe_int(getattr(bot, "bot_account_id", 0)):
        return "self"
    try:
        from modules.admin_tools import has_admin_permission
        if has_admin_permission(chat_id, user_id,
                                getattr(user_entity, "username", None)):
            return "admin"
    except Exception:
        pass
    try:
        from modules import vip_storage
        if vip_storage.is_vip(chat_id, user_id):
            return "vip"
    except Exception:
        pass
    if exempt_bots() and getattr(user_entity, "bot", False):
        return "bot"
    return None


def _event_sender(event):
    """فرستندهٔ رویداد (اگر هندلر آن را پاس نداده باشد)."""
    for attr in ("sender", "_sender"):
        value = getattr(event, attr, None)
        if value is not None:
            return value
    try:
        return getattr(getattr(event, "from_id", None), "user_id", None)
    except Exception:
        return None


def moderation_reason(bot, chat_id, user_id, text, sender=None):
    """آیا این پیام «تخلف» است؟ اگر بله، عضویت اجباری نباید دخالت کند.

    دلیل: کاربری که نام تبلیغاتی، کلمهٔ فیلترشده، آگهی یا هر تخلف دیگری
    دارد باید مسیر عادی مجازات (اخطار/سکوت/اخراج) را ببیند، نه پیام
    «اول عضو کانال شوید». این کاربران اغلب ربات‌های تبلیغاتی‌اند و
    فرستادن راهنمای عضویت برایشان هم بی‌فایده است هم گروه را شلوغ می‌کند.

    خروجی: رشتهٔ دلیل (برای لاگ) یا ``None``. هیچ RPC‌ای انجام نمی‌شود؛
    فقط بررسی‌های محلی و ارزان.
    """
    text = str(text or "")
    # ۱) نام تبلیغاتی کاربر (همان سنجه‌ای که مسیر بنِ نام آگهی استفاده می‌کند)
    if sender is not None:
        try:
            from modules import ad_name_detector
            if ad_name_detector.reason(sender):
                return "ad_name"
        except Exception:
            pass
        # ۲) نام فیلترشدهٔ خود گروه («فیلتر اسم …»)
        try:
            from modules.group_names_storage import find_matching_filtered_name
            if find_matching_filtered_name(sender, group_id=chat_id):
                return "filtered_name"
        except Exception:
            pass
    # ۳) کلمهٔ فیلترشدهٔ گروه («فیلتر …»)
    try:
        from modules.group_words_storage import (
            find_matching_filter_word,
            get_words,
        )
        if find_matching_filter_word(text, get_words(chat_id)):
            return "filter_word"
    except Exception:
        pass
    # ۴) کلمات ممنوعهٔ سراسری (حالت سخت‌گیرانه) — همان تشخیص spam_detector
    try:
        detector = getattr(bot, "detector", None)
        check = getattr(detector, "check_banned_words", None)
        if callable(check):
            hit = check(text, chat_id)
            if isinstance(hit, tuple):
                hit = hit[0]
            if hit:
                return "banned_word"
    except Exception:
        pass
    # ۵) متن تبلیغاتی/آگهی
    try:
        from modules import big_spam
        if big_spam.looks_promotional(text):
            return "promotional"
    except Exception:
        pass
    # ۶) کاربر از قبل بن یا سکوت‌شده است (پیامش حذف/مجازات می‌شود)
    try:
        from modules.banned_storage import is_banned as storage_is_banned
        username = getattr(sender, "username", None) if sender is not None else None
        if storage_is_banned(chat_id, user_id, username):
            return "banned"
    except Exception:
        pass
    try:
        tracker = getattr(bot, "tracker", None)
        for name in ("is_banned", "is_muted"):
            check = getattr(tracker, name, None)
            if callable(check) and check(chat_id, user_id):
                return name.lstrip("is_").lower()
    except Exception:
        pass
    return None


async def gate_message(bot, event, chat_id, user_id, user_entity=None, *,
                       exempt: bool = False) -> bool:
    """آیا پیام این کاربر باید حذف شود؟

    ``True`` یعنی «حذف شد / رسیدگی شد → مسیر پیام باید متوقف شود».
    این تابع **هرگز** استثنا پرتاب نمی‌کند؛ هر خطای داخلی یعنی fail-open.
    """
    try:
        record = cached_group(chat_id)
        if not record or not record.get("force_join_enabled"):
            return False
        if exempt or not user_id:
            return False
        if api.entity_is_channel(user_entity):
            # پست بی‌نامِ کانال، عضو گروه حساب نمی‌شود.
            return False
        if local_exemption(bot, chat_id, user_id, user_entity):
            return False
        # فرمان فقط-خواندنی «وضعیت کانال» برای همه آزاد است (حتی غیرعضو)،
        # وگرنه کاربر برای دیدن وضعیت نمی‌توانست چیزی بپرسد.
        text = getattr(getattr(event, "message", None), "message", None) or ""
        matched = match_command(text)
        if matched is not None and matched[0] == "status":
            return False
        # «راهنمای کانال» و «راهنمای اجرا» فقط متن راهنما هستند؛ غیرعضو هم
        # باید بتواند بخواندشان (دقیقاً همان کسی که راهنما را لازم دارد).
        if normalize_text(text) in ("راهنمای کانال", "راهنمای اجرا"):
            return False

        STATS["decisions"] += 1
        status, layer, detail = await resolve_status(
            bot, record, user_id, user_entity
        )
        if status == storage.STATUS_MEMBER:
            STATS["allowed"] += 1
            if is_awaiting_confirm(chat_id, user_id):
                # کاربری که راهنما گرفته بود حالا عضو است → «تأیید شد»
                _schedule_confirm(bot, chat_id, user_id)
            return False
        if status != storage.STATUS_NOT_MEMBER:
            # نامعلوم → اجازه بده (fail-open)؛ حذف بدون شاهد ممنوع است.
            STATS["unknown_allowed"] += 1
            _log(bot, f"GATE unknown -> allow chat_id={chat_id} "
                      f"user_id={user_id} layer={layer}")
            # ولی «اجازهٔ بی‌صدا» ممنوع: کاربر راهنما می‌گیرد (بدون حذف)
            # فقط اگر مالک سیاست سخت‌گیرانه را روشن کرده باشد.
            if _env_flag("FORCE_JOIN_STRICT_UNKNOWN", False):
                _strict_reason = moderation_reason(
                    bot, chat_id, user_id, text,
                    user_entity if user_entity is not None else _event_sender(event),
                )
                if not _strict_reason:
                    _maybe_send_notice(bot, event, chat_id, record, user_id)
            return False

        # تخلف (نام تبلیغاتی/کلمهٔ فیلتر/آگهی/بن‌شده) اول به مسیر مجازات
        # می‌رود: به چنین کاربری «اول عضو کانال شوید» نمی‌گوییم و پیامش را
        # هم به‌عنوان غیرعضو حذف نمی‌کنیم؛ خودِ ماژول‌های تنبیه تصمیم می‌گیرند.
        reason = moderation_reason(
            bot, chat_id, user_id, text,
            user_entity if user_entity is not None else _event_sender(event),
        )
        if reason:
            STATS["deferred"] += 1
            _log(bot, f"GATE deferred to moderation chat_id={chat_id} "
                      f"user_id={user_id} reason={reason}")
            return False

        STATS["blocked"] += 1
        _log(bot, f"GATE blocked chat_id={chat_id} user_id={user_id} "
                  f"layer={layer} channel_id={record['channel_id']}")
        await _enforce(bot, event, chat_id, record, user_id, user_entity)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _log(bot, f"GATE FAILED (fail-open) chat_id={chat_id} "
                  f"user_id={user_id} error={type(error).__name__}", error=True)
        return False


async def _enforce(bot, event, chat_id, record, user_id,
                   user_entity=None) -> None:
    """حذف پیام کاربر غیرعضو + پیام راهنما با کنترل نرخ."""
    message = getattr(event, "message", None)
    message_id = getattr(message, "id", None)
    if message_id:
        _delete_message(bot, chat_id, message_id)
    _maybe_send_notice(bot, event, chat_id, record, user_id, user_entity)


def _delete_message(bot, chat_id, message_id) -> None:
    queue = getattr(bot, "message_delete_queue", None)
    try:
        if queue is not None:
            queue.enqueue(chat_id, [message_id], priority=0)
            STATS["deletes"] += 1
            return
    except Exception:
        pass

    async def fallback():
        try:
            client = getattr(bot, "client", None)
            if client is not None:
                await client.delete_messages(chat_id, [message_id])
                STATS["deletes"] += 1
        except Exception as error:
            _log(bot, f"DELETE FAILED chat_id={chat_id} "
                      f"message_id={message_id} error={type(error).__name__}",
                 error=True)

    try:
        asyncio.get_running_loop().create_task(fallback(), name="force-join-delete")
    except RuntimeError:
        pass


def notice_text(record, name=None) -> str:
    """متن پیام راهنما (لینک کانال) — قالب مصوبِ روباه."""
    link = record.get("channel_link") or ""
    who = name or "کاربر"
    lines = [
        f"کاربر : {who}",
        "",
        "ارسال پیام در این گروه مجاز نیست، اول عضو کانال زیر شوید",
        "",
        f"📢 لینک کانال : {link}" if link else "📢 لینک کانال : از مدیر گروه بپرسید",
        "",
        "بعد از تایید عضویت میتوانید پیام بدهید",
    ]
    return "\n".join(lines)


def notice_entities(text, name=None) -> list:
    """خط «کاربر : …» داخل نقل‌قول سروش پلاس."""
    who = name or "کاربر"
    line = f"کاربر : {who}"
    if not text.startswith(line):
        return []
    return [MessageEntityBlockquote(offset=0, length=_u16(line))]


def confirm_text(record, name=None) -> str:
    """متن پیام «عضویت تأیید شد» — قالب مصوبِ روباه."""
    who = name or "کاربر"
    return (f"💌 کاربر : {who}\n"
            "عضو کانال شد، میتوانید پیام ارسال کنید")


def confirm_entities(text, name=None) -> list:
    """خط کاربر در نقل‌قول، جملهٔ تأیید Bold."""
    who = name or "کاربر"
    first = f"💌 کاربر : {who}"
    second = "عضو کانال شد، میتوانید پیام ارسال کنید"
    entities = [MessageEntityBlockquote(offset=0, length=_u16(first))]
    index = text.find(second)
    if index >= 0:
        entities.append(MessageEntityBold(offset=_u16(text[:index]),
                                          length=_u16(second)))
    return entities


def _notice_allowed(chat_id, user_id) -> bool:
    """کنترل نرخ دو لایه: cooldown هر کاربر + سقف دقیقه‌ای هر گروه."""
    now = time.time()
    key = (canonical_group_key_str(chat_id), _safe_int(user_id))
    last = _notice_sent.get(key, 0.0)
    if now - last < notice_cooldown():
        return False
    window = _notice_window.setdefault(str(canonical_group_key(chat_id)), [])
    cutoff = now - 60
    while window and window[0] < cutoff:
        window.pop(0)
    if len(window) >= notice_per_minute():
        return False
    if len(_notice_sent) > 5000:
        cutoff_old = now - max(notice_cooldown(), 60)
        for stale in [k for k, ts in _notice_sent.items() if ts < cutoff_old]:
            _notice_sent.pop(stale, None)
    _notice_sent[key] = now
    window.append(now)
    return True


async def _deliver_text(bot, chat_id, text, *, entities=None,
                        event=None, urgent=False) -> bool:
    """ارسال متن؛ تأیید عضویتِ رویداد با اولویت ۰ و بدون تأخیر poll."""
    sender = getattr(bot, "outgoing_sender", None)
    extra = {"formatting_entities": list(entities)} if entities else {}

    def capture(sent):
        try:
            from modules.notice_cleanup import capture_sent
            capture_sent(bot, chat_id, sent)
        except Exception:
            pass

    # A join event is already a positive membership proof. Queue its
    # confirmation as urgent instead of waiting for confirm_first_delay/tick.
    if urgent and sender is not None:
        try:
            sender.enqueue_send(chat_id, text, priority=0, on_done=capture,
                                channel="main", **extra)
            return True
        except Exception:
            pass

    if event is not None and sender is not None:
        try:
            sender.enqueue_reply_notice(event, text, on_done=capture, **extra)
            return True
        except TypeError:
            try:   # امضای قدیمی بدون entity
                sender.enqueue_reply_notice(event, text, on_done=capture)
                return True
            except Exception:
                pass
        except Exception:
            pass
    if event is not None:
        try:
            capture(await event.reply(text, **extra))
            return True
        except TypeError:
            try:
                capture(await event.reply(text))
                return True
            except Exception:
                pass
        except Exception:
            pass
    client = getattr(bot, "client", None)
    if client is None:
        return False
    try:
        capture(await asyncio.wait_for(
            client.send_message(chat_id, text, **extra), timeout=api_timeout()))
        return True
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _log(bot, f"DELIVER FAILED chat_id={chat_id} "
                  f"error={type(error).__name__}", error=True)
        return False


def await_join_confirmation(record, chat_id, user_id, name=None) -> bool:
    """ثبت انتظارِ «تأیید عضویت» برای کاربری که راهنما گرفته است.

    چرا لازم است: رویداد ``UpdateChannelParticipant`` فقط به **ادمین**
    کانال می‌رسد؛ پس وقتی حساب ادمین نیست، هیچ‌کس خبر نمی‌دهد که کاربر
    عضو شد. این صف، همان کاربر را تا `FORCE_JOIN_CONFIRM_WINDOW` ثانیه
    دوره‌ای با زنجیرهٔ مسیرها بررسی می‌کند و به‌محض تأیید، پیام «تأیید
    شد» را در گروه می‌فرستد.
    """
    if not confirm_on_join():
        return False
    group_key = canonical_group_key_str(chat_id)
    uid = _safe_int(user_id)
    if not group_key or not uid:
        return False
    if len(_awaiting_confirm) >= _CONFIRM_MAX_ENTRIES:
        # قدیمی‌ترها را دور بریز تا حافظه بی‌نهایت رشد نکند
        for stale in sorted(_awaiting_confirm,
                            key=lambda k: _awaiting_confirm[k]["at"])[:200]:
            _awaiting_confirm.pop(stale, None)
    now = time.time()
    _awaiting_confirm[(group_key, uid)] = {
        "user_id": uid,
        "at": now,
        "next_at": now + confirm_first_delay(),
        "deadline": now + confirm_window(),
        "tries": 0,
        "chat_id": chat_id,
        "group_id": record.get("group_id"),
        "channel_id": record.get("channel_id"),
        "name": name,
    }
    return True


async def confirm_membership(bot, chat_id, user_id, *, name=None) -> bool:
    """ارسال فوری پیام «عضویت تأیید شد» و حذف از صف انتظار."""
    key = (canonical_group_key_str(chat_id), _safe_int(user_id))
    entry = _awaiting_confirm.pop(key, None)
    if entry is None:
        return False
    record = storage.get_group(chat_id) or {}
    who = name or entry.get("name") or "کاربر"
    text = confirm_text(record, who)
    sent = await _deliver_text(bot, chat_id, text,
                               entities=confirm_entities(text, who),
                               urgent=True)
    if sent:
        _log(bot, f"JOIN CONFIRM queued chat={chat_id} user={user_id} priority=0")
    _record_decision(record.get("group_id"), user_id, "member", "confirm_join")
    return sent


def is_awaiting_confirm(chat_id, user_id) -> bool:
    return (canonical_group_key_str(chat_id), _safe_int(user_id)) in \
        _awaiting_confirm


def _schedule_confirm(bot, chat_id, user_id, name=None) -> bool:
    """نسخهٔ غیرمسدودساز برای مسیر پیام (fire-and-forget)."""
    key = (canonical_group_key_str(chat_id), _safe_int(user_id))
    if key not in _awaiting_confirm:
        return False

    async def runner():
        try:
            await confirm_membership(bot, chat_id, user_id, name=name)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(runner(),
                                              name="force-join-confirm")
        return True
    except RuntimeError:
        return False


async def _confirm_batch(bot, channel_key_str, entries):
    """یک پاس بررسی برای **همهٔ** منتظرانِ یک کانال (یک پیمایش، نه n پیمایش)."""
    record = storage.get_group(entries[0][1].get("chat_id"))
    if record is None or not record.get("force_join_enabled"):
        for key, _entry in entries:
            _awaiting_confirm.pop(key, None)
        return 0

    user_ids = [entry.get("user_id") or key[1] for key, entry in entries]
    user_ids = [uid for uid in user_ids if uid]
    member_ids = set()
    layer = "scan"

    # ۱) اگر توکن ربات هست: getChatMember برای همان کاربران (HTTP، بدون فهرست)
    token = bot_api_token()
    if token:
        for uid in user_ids:
            if not _rpc_budget_allows(1):
                break
            result = await api.bot_api_get_chat_member(
                token, record["channel_id"], uid, timeout=api_timeout())
            if result.get("state") == "member":
                member_ids.add(int(uid))
            layer = "bot_api"
    else:
        # ۲) وگرنه: یک پیمایش مشترک برای همهٔ منتظران
        channel_input = await ensure_channel_input(bot, record)
        client = getattr(bot, "client", None)
        if client is not None and channel_input is not None and \
                _rpc_budget_allows(1):
            STATS["scans"] += 1
            found = await api.find_members(
                client, channel_input, user_ids,
                max_members=scan_max_members(),
                budget_seconds=scan_budget(),
                delay=scan_delay(),
            )
            for uid, user in (found.get("found") or {}).items():
                member_ids.add(int(uid))
                storage.set_member_access_hash(
                    record["channel_id"], uid,
                    getattr(user, "access_hash", None))
            if found.get("state") == api.RESULT_NO_ACCESS:
                for key, _entry in entries:
                    _awaiting_confirm.pop(key, None)
                return 0

    confirmed = 0
    now = time.time()
    for key, entry in entries:
        uid = entry.get("user_id") or key[1]
        if uid and int(uid) in member_ids:
            storage.set_member(record["channel_id"], uid,
                               storage.STATUS_MEMBER, source=storage.SOURCE_SCAN)
            _remember(record["channel_id"], uid, storage.STATUS_MEMBER, now)
            forget_user(record["channel_id"], uid)
            await confirm_membership(bot, entry.get("chat_id"), uid,
                                     name=entry.get("name"))
            confirmed += 1
            _log(bot, f"CONFIRM SENT chat_id={entry.get('chat_id')} "
                      f"user_id={uid} layer={layer}")
            continue
        if now > entry.get("deadline", 0) or \
                entry.get("tries", 0) >= confirm_max_checks():
            _awaiting_confirm.pop(key, None)
            _log(bot, f"CONFIRM EXPIRED chat_id={entry.get('chat_id')} "
                      f"user_id={uid} tries={entry.get('tries')}")
            continue
        entry["tries"] = entry.get("tries", 0) + 1
        entry["next_at"] = now + confirm_backoff(entry["tries"])
    return confirmed


def _schedule_confirm_poll(bot, channel_key_str, entries) -> bool:
    """اجرای پاس تأیید در پس‌زمینه (هرگز مسیر پیام را بلاک نمی‌کند)."""
    running = _confirm_inflight.get(channel_key_str)
    if running is not None and not running.done():
        return False

    async def runner():
        try:
            async with _sem():
                await _confirm_batch(bot, channel_key_str, entries)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log(bot, f"CONFIRM BATCH FAILED channel={channel_key_str} "
                      f"error={type(error).__name__}", error=True)
        finally:
            if _confirm_inflight.get(channel_key_str) is task:
                _confirm_inflight.pop(channel_key_str, None)

    task = None
    try:
        task = asyncio.ensure_future(runner())
    except Exception:
        return False
    _confirm_inflight[channel_key_str] = task
    return True


async def confirm_pending(bot, *, limit: int = 0) -> int:
    """یک دور بررسی صف انتظار — **گروه‌بندی‌شده بر اساس کانال**.

    تأخیر تأیید از این‌جا می‌آید: اولین بررسی ۳ ثانیه بعد از ارسال راهنما و
    ناظر هر ۲ ثانیه صف را می‌بیند ⇒ در عمل ۳ تا ۵ ثانیه. برای همهٔ منتظران
    یک کانال فقط **یک** پیمایش انجام می‌شود.
    """
    now = time.time()
    buckets: dict = {}
    for key, entry in list(_awaiting_confirm.items()):
        if now < entry.get("next_at", 0):
            continue
        channel = channel_key(entry.get("channel_id") or entry.get("chat_id"))
        buckets.setdefault(str(channel), []).append((key, entry))
    total = 0
    for channel_key_str, entries in buckets.items():
        if limit and total >= limit:
            break
        # اگر پاسِ قبلی همین کانال در جریان است، به دور بعد واگذار می‌شود.
        running = _confirm_inflight.get(channel_key_str)
        if running is not None and not running.done():
            continue
        total += len(entries)
        await _confirm_batch(bot, channel_key_str, entries)
    return total


async def confirm_watcher(bot, *, logger=None) -> None:
    """ناظر تأیید عضویت — سریع، سبک و مستقل از مسیر پیام."""
    await asyncio.sleep(confirm_first_delay())
    while True:
        try:
            if _awaiting_confirm:
                await confirm_pending(bot)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log(bot, f"CONFIRM WATCHER error={type(error).__name__}",
                 error=True)
        await asyncio.sleep(confirm_tick())


async def send_notice(bot, chat_id, record, *, event=None, name=None) -> bool:
    """ارسال پیام راهنما (لینک کانال) — با event یا مستقیماً به گروه."""
    who = name or "کاربر"
    text = notice_text(record, who)
    sent = await _deliver_text(bot, chat_id, text,
                               entities=notice_entities(text, who),
                               event=event)
    if sent:
        STATS["notices"] += 1
    return sent


async def notify_join(bot, record, chat_id, user_id, user_entity=None) -> bool:
    """کاربر تازه‌وارد گروه: اگر عضویت کانال تأیید نشد، لینک را بفرست.

    این همان چیزی است که کاربر انتظار دارد: «به‌محض دیدن کاربرِ تازه، به
    او بگو عضو کانال شود». بررسی هم با زنجیرهٔ مسیرها انجام می‌شود تا
    کاربری که از قبل عضو کانال است پیام اضافه نگیرد.
    """
    if not notice_on_join():
        return False
    try:
        status, layer, _ = await resolve_status(
            bot, record, user_id, user_entity, force=True
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        status, layer = storage.STATUS_UNKNOWN, f"error:{type(error).__name__}"
    if status == storage.STATUS_MEMBER:
        _record_decision(record["group_id"], user_id, "member", "join_event")
        return False
    if not _notice_allowed(chat_id, user_id):
        return False
    who = user_label(user_entity, user_id)
    sent = await send_notice(bot, chat_id, record, name=who)
    if sent:
        # حالا منتظر می‌مانیم تا کاربر عضو کانال شود و «تأیید شد» را بگوییم.
        await_join_confirmation(record, chat_id, user_id, who)
    _record_decision(record["group_id"], user_id, status, f"join_notice:{layer}")
    _log(bot, f"JOIN NOTICE chat_id={chat_id} user_id={user_id} "
              f"status={status} layer={layer} sent={sent}")
    return sent


def _maybe_send_notice(bot, event, chat_id, record, user_id,
                       user_entity=None) -> None:
    if not _notice_allowed(chat_id, user_id):
        return
    who = user_label(user_entity, user_id)
    text = notice_text(record, who)
    entities = notice_entities(text, who)
    sender = getattr(bot, "outgoing_sender", None)
    extra = {"formatting_entities": entities} if entities else {}

    def capture(sent):
        try:
            from modules.notice_cleanup import capture_sent
            capture_sent(bot, chat_id, sent)
        except Exception:
            pass

    STATS["notices"] += 1
    await_join_confirmation(record, chat_id, user_id, who)
    try:
        if sender is not None:
            sender.enqueue_reply_notice(event, text, on_done=capture, **extra)
            return
    except TypeError:
        try:
            sender.enqueue_reply_notice(event, text, on_done=capture)
            return
        except Exception:
            pass
    except Exception:
        pass

    async def fallback():
        try:
            capture(await event.reply(text, **extra))
        except TypeError:
            capture(await event.reply(text))
        except Exception as error:
            _log(bot, f"NOTICE FAILED chat_id={chat_id} "
                      f"error={type(error).__name__}", error=True)

    try:
        asyncio.get_running_loop().create_task(fallback(),
                                              name="force-join-notice")
    except RuntimeError:
        pass


async def self_input_peer(bot):
    """``InputPeerUser`` حساب خودمان بدون RPC (برای سنجش نقش در کانال)."""
    client = getattr(bot, "client", None)
    if client is None:
        return None
    try:
        peer = await asyncio.wait_for(client.get_me(input_peer=True),
                                      timeout=api_timeout())
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    return api.input_user(peer) or peer


def _participant_is_admin(participant) -> bool:
    name = type(participant).__name__.lower()
    return "admin" in name or "creator" in name


async def probe_access(bot, channel_id, access_hash) -> dict:
    """سنجش **واقعی** اینکه کدام مسیرهای بررسی عضویت روی این سرور جواب می‌دهند.

    چهار مسیر جداگانه امتحان می‌شوند و نتیجهٔ هرکدام ثبت می‌شود:

    * ``get_participant`` — ``channels.getParticipant`` (سروش: NOT_SUPPORTED)
    * ``bot_api`` — ``getChatMember`` در Bot API (روش ربات‌های تلگرامی)
    * ``search`` — ``channels.getParticipants`` (پیش‌نیاز جست‌وجوی هدفمند)
    * ``snapshot`` — پیمایش کامل فهرست اعضا برای diff دوره‌ای

    هیچ‌چیز حدس زده نمی‌شود: هر مسیر یا پاسخ واقعی داده یا کنار گذاشته
    می‌شود، و دلیلش در ``warnings`` می‌آید.
    """
    result = {
        "ok": False,
        "reason": None,
        "can_view_participants": None,
        "participants_hidden": None,
        "participants_count": None,
        "is_admin": False,
        "is_member": None,
        "per_user_check": False,
        "snapshot_check": False,
        "routes": {
            ROUTE_PARTICIPANT: False,
            ROUTE_BOT_API: False,
            ROUTE_SEARCH: False,
            ROUTE_SCAN: False,
            ROUTE_SNAPSHOT: False,
        },
        "route_notes": {},
        "sync_mode": storage.SYNC_MODE_PROBE,
        "warnings": [],
    }
    client = getattr(bot, "client", None)
    channel_input = api.input_channel(channel_id, access_hash)
    if client is None or channel_input is None:
        result["reason"] = "unavailable"
        return result

    full = await api.get_full_channel(client, channel_input,
                                      timeout=resolve_timeout())
    if full.get("ok"):
        result["ok"] = True
        result["can_view_participants"] = full.get("can_view_participants")
        result["participants_hidden"] = full.get("participants_hidden")
        result["participants_count"] = full.get("participants_count")
    else:
        result["reason"] = full.get("reason")
        if full.get("reason") in (api.RESULT_NO_ACCESS, api.RESULT_NOT_MEMBER):
            result["warnings"].append(
                "کانال در دسترس نیست یا فهرست اعضا مخفی است."
            )

    me = await self_input_peer(bot)
    my_id = getattr(me, "user_id", None) or getattr(me, "user_id", None)
    if my_id is None and me is not None:
        my_id = getattr(getattr(me, "peer", None), "user_id", None) or \
            getattr(me, "id", None)

    # ---- مسیر ۱: channels.getParticipant ----
    if me is not None:
        mine = await api.get_participant(client, channel_input, me,
                                         timeout=api_timeout())
        state = mine.get("state")
        if state in (api.RESULT_MEMBER, api.RESULT_NOT_MEMBER):
            result["per_user_check"] = True
            result["routes"][ROUTE_PARTICIPANT] = True
            result["is_member"] = state == api.RESULT_MEMBER
            result["is_admin"] = _participant_is_admin(mine.get("participant"))
            result["route_notes"][ROUTE_PARTICIPANT] = state
        else:
            result["route_notes"][ROUTE_PARTICIPANT] = state
            if state == api.RESULT_NOT_SUPPORTED:
                result["warnings"].append(
                    "سرور سروش «channels.getParticipant» را پیاده نکرده است "
                    "(422 NOT_SUPPORTED)؛ مسیر جایگزین فعال می‌شود."
                )
            else:
                result["warnings"].append(
                    f"channels.getParticipant پاسخ نداد ({state})."
                )

    # ---- مسیر ۲: Bot API getChatMember ----
    token = bot_api_token()
    if not token:
        result["route_notes"][ROUTE_BOT_API] = "no_token"
    elif not my_id:
        result["route_notes"][ROUTE_BOT_API] = "no_self_id"
    else:
        probe_member = await api.bot_api_get_chat_member(
            token, channel_id, my_id, timeout=api_timeout()
        )
        state = probe_member.get("state")
        result["route_notes"][ROUTE_BOT_API] = state
        if state in ("member", "not_member"):
            result["routes"][ROUTE_BOT_API] = True
        elif state == "no_access":
            result["warnings"].append(
                "رباتِ BOT_TOKEN ادمین این کانال نیست یا به کانال دسترسی "
                "ندارد؛ getChatMember جواب نداد."
            )
        elif state in ("unsupported", "error"):
            result["warnings"].append(
                f"Bot API getChatMember روی سروش جواب نداد ({state})."
            )

    # ---- مسیر ۳/۴: پیمایش فهرست اعضا ----
    listing = await api.list_probe(client, channel_input,
                                   timeout=resolve_timeout())
    list_state = listing.get("state")
    result["route_notes"][ROUTE_SNAPSHOT] = list_state
    list_ok = list_state in ("ok", "empty")
    if list_ok:
        result["routes"][ROUTE_SEARCH] = True
    elif list_state == api.RESULT_NOT_SUPPORTED:
        result["warnings"].append(
            "channels.getParticipants هم پشتیبانی نمی‌شود؛ اسنپ‌شات ممکن نیست."
        )
    elif list_state == api.RESULT_NO_ACCESS:
        result["warnings"].append(
            "فهرست اعضای کانال قابل خواندن نیست (ChatAdminRequired یا مخفی)."
        )

    # ---- تشخیص ادمین بودن بدون getParticipant ----
    if not result["is_admin"] and list_ok and my_id:
        admins = await api.list_admins(client, channel_input,
                                       timeout=resolve_timeout())
        if admins.get("state") == "ok" and int(my_id) in admins["user_ids"]:
            result["is_admin"] = True
            result["route_notes"]["is_admin"] = "admins_page"

    hidden = bool(result["participants_hidden"])
    can_view = result["can_view_participants"]
    admin_or_visible = bool(
        result["is_admin"] or (can_view is not False and not hidden)
    )
    result["routes"][ROUTE_SNAPSHOT] = bool(list_ok and admin_or_visible)
    # پیمایش هدفمند همان متد را با صفحه‌بندی می‌خواند؛ اگر یک صفحه جواب داد،
    # این مسیر هم در دسترس است (و برخلاف اسنپ‌شات همیشه تازه است).
    result["routes"][ROUTE_SCAN] = bool(list_ok)
    result["snapshot_check"] = result["routes"][ROUTE_SNAPSHOT]

    # ---- انتخاب حالت همگام‌سازی ----
    routes = result["routes"]
    if routes[ROUTE_SNAPSHOT] and result["is_admin"]:
        result["sync_mode"] = storage.SYNC_MODE_EVENT
    elif routes[ROUTE_SNAPSHOT]:
        result["sync_mode"] = storage.SYNC_MODE_SNAPSHOT
    elif routes[ROUTE_BOT_API]:
        result["sync_mode"] = storage.SYNC_MODE_BOT_API
    elif routes[ROUTE_SCAN] and not routes[ROUTE_SNAPSHOT]:
        result["sync_mode"] = storage.SYNC_MODE_SNAPSHOT
    elif routes[ROUTE_PARTICIPANT]:
        result["sync_mode"] = storage.SYNC_MODE_PROBE
    else:
        result["sync_mode"] = storage.SYNC_MODE_PROBE
        result["warnings"].append(
            "هیچ مسیری برای بررسی عضویت جواب نداد؛ عضویت اجباری فعال نمی‌شود "
            "تا پیام کاربران واقعی حذف نشود."
        )
    if result["is_member"] is False:
        result["warnings"].append(
            "حساب ربات عضو کانال نیست؛ برای بررسی مطمئن، اول در کانال عضو شوید."
        )
    return result


async def register_channel(bot, chat_id, target):
    """ثبت/تغییر کانال اجباری یک گروه + سنجش دسترسی.

    خروجی ``(ok, message, record)``.
    """
    client = getattr(bot, "client", None)
    if client is None:
        return False, "❌ کلاینت در دسترس نیست.", None
    resolved = await api.resolve_channel(client, target,
                                         timeout=resolve_timeout())
    if not resolved.get("ok"):
        reason = resolved.get("reason")
        if reason == "not_a_channel":
            return (False,
                    "❌ این شناسه یک کانال نیست. لینک یا آیدی کانال را بدهید.",
                    None)
        return (False,
                "❌ کانال پیدا نشد.\n"
                "لینک کامل (مثل https://splus.ir/xxx) یا آیدی عددی را بدهید.\n"
                f"دلیل: {reason}", None)

    channel_id = resolved["channel_id"]
    access_hash = resolved.get("access_hash")
    if access_hash is None:
        return (False,
                "❌ دسترسی به کانال خوانده نشد (access_hash ندارد). "
                "مطمئن شوید حساب ربات در کانال عضو است.", None)

    probe = await probe_access(bot, channel_id, access_hash)
    routes = probe.get("routes") or {}
    # کافی است **یک** مسیر بتواند حکم قطعی بدهد؛ لازم نیست getParticipant
    # پشتیبانی شود (روی سروش نمی‌شود).
    enabled = any(routes.get(route) for route in ROUTE_DECIDES_ABSENCE)
    record = storage.save_group(
        chat_id,
        channel_id=channel_id,
        channel_access_hash=access_hash,
        channel_link=resolved.get("link"),
        channel_title=resolved.get("title"),
        enabled=enabled,
        sync_mode=probe.get("sync_mode") or storage.SYNC_MODE_PROBE,
    )
    save_channel_routes(record["group_id"], routes)
    record = refresh_group_cache(record["group_id"]) or record
    try:
        from modules import group_storage
        group_storage.update_group_title(chat_id, resolved.get("title") or "")
    except Exception:
        pass

    # قالب مصوبِ روباه: فقط چهار خط. جزئیات فنی (حالت همگام‌سازی و
    # مسیرها) عمداً نمایش داده نمی‌شود؛ در «تست عضویت»/لاگ هست.
    lines = []
    if enabled:
        lines.append("✅ کانال اجباری ثبت شد.")
    else:
        lines.append("⚠️ کانال ثبت شد ولی عضویت اجباری فعال نشد.")
    lines.append(f"📢 {resolved.get('title') or '—'}")
    if resolved.get("link"):
        lines.append(f"🔗 {resolved['link']}")
    lines.append("")
    if probe.get("participants_count") is not None:
        lines.append(f"👥 تعداد اعضای کانال: "
                     f"{_digits(probe['participants_count'])}")
    if not enabled:
        # فقط در حالت شکست، دلیل به مالک گفته می‌شود (بدون ادعای موفقیت).
        for warning in probe.get("warnings") or ():
            lines.append(f"• {warning}")
        lines.append("برای فعال/غیرفعال کردن: «تنظیم عضویت اجباری» / "
                     "«لغو عضویت اجباری»")
    _log(bot, f"REGISTER group_id={record['group_id']} channel_id={channel_id} "
              f"enabled={enabled} mode={probe.get('sync_mode')}")
    return enabled, "\n".join(lines), record


async def enable_channel(bot, chat_id):
    """فعال‌سازی عضویت اجباری — فقط اگر بررسی عضویت واقعاً ممکن باشد.

    اگر حالت ثبت‌شده ``probe`` باشد (یعنی هنگام ثبت نتوانستیم تأیید کنیم)،
    یک سنجش دوباره انجام می‌شود؛ فعال‌سازی کورکورانه ممنوع است چون در آن
    حالت تنها نتیجه «نامعلوم» است و کاربران واقعی آسیب می‌بینند.
    """
    record = storage.get_group(chat_id)
    if record is None:
        return False, "ℹ️ اول کانال را ثبت کنید: «کانال من <لینک یا آیدی>»"
    if record["force_join_enabled"]:
        return True, "ℹ️ عضویت اجباری از قبل فعال بود."

    routes = channel_routes(record)
    if record.get("channel_access_hash") is None or \
            record.get("sync_mode") == storage.SYNC_MODE_PROBE or \
            not any(routes.get(route) for route in ROUTE_DECIDES_ABSENCE):
        probe = await probe_access(bot, record["channel_id"],
                                   record.get("channel_access_hash"))
        routes = probe.get("routes") or {}
        save_channel_routes(record["group_id"], routes)
        record = storage.get_group(record["group_id"]) or record
        if not any(routes.get(route) for route in ROUTE_DECIDES_ABSENCE):
            lines = ["❌ امکان بررسی عضویت برای این کانال وجود ندارد؛ "
                     "عضویت اجباری فعال نشد."]
            for warning in probe.get("warnings") or ():
                lines.append(f"• {warning}")
            return False, "\n".join(lines)
        storage.set_sync_state(record["group_id"],
                               sync_mode=probe.get("sync_mode"))

    storage.set_enabled(chat_id, True)
    record = refresh_group_cache(chat_id) or record
    active = [ROUTE_LABELS[r] for r in ROUTE_LABELS
              if channel_routes(record).get(r)]
    return True, ("✅ عضویت اجباری فعال شد.\n"
                  f"🧭 حالت همگام‌سازی: {record.get('sync_mode')}\n"
                  f"🛣 مسیرها: {'، '.join(active) if active else '—'}\n"
                  f"👥 اعضای تأییدشده: "
                  f"{_digits(storage.member_count(record['channel_id']))}")


def disable_channel(chat_id):
    record = storage.get_group(chat_id)
    if record is None:
        return False, "ℹ️ برای این گروه کانال اجباری ثبت نشده است."
    if not record["force_join_enabled"]:
        return True, "ℹ️ عضویت اجباری از قبل غیرفعال بود."
    storage.set_enabled(chat_id, False)
    refresh_group_cache(chat_id)
    return True, "🛑 عضویت اجباری غیرفعال شد (پیام‌ها دیگر حذف نمی‌شوند)."


def remove_channel(chat_id):
    if storage.delete_group(chat_id):
        refresh_group_cache(chat_id)
        return True, "🗑 کانال اجباری و وضعیت اعضای آن حذف شد."
    return False, "ℹ️ رکوردی برای حذف وجود نداشت."


# ---------------------------------------------------------------------------
# رویدادهای عضویت کانال/گروه
# ---------------------------------------------------------------------------

def _event_user_ids(event) -> list:
    ids = getattr(event, "user_ids", None)
    if ids:
        return [_safe_int(value) for value in ids if _safe_int(value)]
    single = getattr(event, "user_id", None)
    return [_safe_int(single)] if _safe_int(single) else []


async def handle_chat_action(bot, event) -> None:
    """رویداد ``ChatAction``: ورود/خروج کانال اجباری + prefetch ورود گروه."""
    try:
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return
        user_ids = _event_user_ids(event)
        if not user_ids:
            return

        cid = channel_key(chat_id)
        # ایندکس حافظه اول؛ SQLite فقط اگر ایندکس کهنه باشد.
        groups = groups_cached_for_channel(cid) if cid else []
        if groups:
            await _handle_channel_membership(bot, event, groups, user_ids)
            return

        if not (getattr(event, "user_joined", False)
                or getattr(event, "user_added", False)):
            return
        record = cached_group(chat_id)
        if not record or not record.get("force_join_enabled"):
            return
        if not check_on_group_join():
            return
        # آبجکت واقعی کاربر (همراه access_hash) گرفته می‌شود؛ پیش‌تر خودِ
        # event پاس داده می‌شد که access_hash نداشت و بررسی بعدی بی‌نتیجه
        # می‌ماند.
        users = {}
        try:
            entity = await event.get_user()
            if entity is not None:
                users[_safe_int(getattr(entity, "id", 0))] = entity
        except Exception:
            pass
        if not users:
            try:
                for entity in await event.get_users():
                    users[_safe_int(getattr(entity, "id", 0))] = entity
            except Exception:
                pass
        for user_id in user_ids:
            schedule_prefetch(bot, record, user_id, users.get(user_id))
        if notice_on_join():
            # همان لحظهٔ ورود، لینک کانال را بفرست (نه بعد از حذف پیام).
            for user_id in user_ids:
                try:
                    await notify_join(bot, record, chat_id, user_id,
                                      users.get(user_id))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _log(bot, f"CHAT ACTION FAILED error={type(error).__name__}", error=True)


async def _handle_channel_membership(bot, event, groups, user_ids) -> None:
    """ورود/خروج واقعی در کانال اجباری → ثبت فوری در دیتابیس و کش."""
    joined = bool(getattr(event, "user_joined", False)
                  or getattr(event, "user_added", False))
    left = bool(getattr(event, "user_left", False)
                or getattr(event, "user_kicked", False))
    if not joined and not left:
        return
    status = storage.STATUS_MEMBER if joined else storage.STATUS_NOT_MEMBER
    source = (storage.SOURCE_EVENT_JOIN if joined
              else storage.SOURCE_EVENT_LEAVE)
    event_users = {}
    try:
        entity = await event.get_user()
        if entity is not None:
            event_users[_safe_int(getattr(entity, "id", 0))] = entity
    except Exception:
        pass
    for record in groups:
        channel_id = record["channel_id"]
        for user_id in user_ids:
            access_hash = getattr(event_users.get(user_id), "access_hash", None)
            if access_hash:
                storage.set_member_access_hash(channel_id, user_id, access_hash)
            storage.set_member(channel_id, user_id, status, source=source,
                               access_hash=access_hash)
            # باطل‌سازی **هم دیتابیس و هم کش حافظه** (الزام پرامپت)
            forget_user(channel_id, user_id)
            _remember(channel_id, user_id, status, time.time())
            if joined:
                _log(bot, f"JOIN EVENT cached chat={record['group_id']} user={user_id}")
        if joined:
            STATS["events_join"] += len(user_ids)
            # اگر همین کاربر راهنمای عضویت گرفته بود، فوراً «تأیید شد» را بگو
            for user_id in user_ids:
                if is_awaiting_confirm(record["group_id"], user_id):
                    _schedule_confirm(bot, record["group_id"], user_id)
        else:
            STATS["events_leave"] += len(user_ids)
        _log(bot, f"EVENT {'join' if joined else 'leave'} "
                  f"channel_id={channel_id} group_id={record['group_id']} "
                  f"users={len(user_ids)}")


def schedule_prefetch(bot, record, user_id, user_entity=None) -> bool:
    """بررسی پس‌زمینهٔ عضویت یک کاربر (بدون بلاک کردن مسیر)."""
    key = (record["channel_id"], _safe_int(user_id))
    if key in _inflight or key in _pending_prefetch:
        return False
    if len(_pending_prefetch) >= MAX_PENDING_CHECKS:
        return False
    _pending_prefetch[key] = (bot, record, user_id, user_entity, time.time())

    async def _drain():
        while _pending_prefetch:
            item_key, item = next(iter(_pending_prefetch.items()))
            _pending_prefetch.pop(item_key, None)
            bot_, record_, user_id_, entity_, _queued = item
            try:
                await resolve_status(bot_, record_, user_id_, entity_)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
    try:
        asyncio.get_running_loop().create_task(_drain(),
                                              name="force-join-prefetch")
    except RuntimeError:
        return False
    return True


# ---------------------------------------------------------------------------
# اسنپ‌شات و همگام‌سازی جبرانی
# ---------------------------------------------------------------------------

def _sync_lock(group_key) -> asyncio.Lock:
    lock = _sync_locks.get(group_key)
    if lock is None:
        lock = asyncio.Lock()
        _sync_locks[group_key] = lock
    return lock


async def snapshot_sync(bot, group_id, *, force: bool = False) -> dict:
    """اسنپ‌شات کامل اعضای کانال + diff برای تشخیص خروج‌های بی‌رویداد.

    اگر اسنپ‌شات **ناقص** بماند (بودجه/خطا/سقف)، مرحلهٔ «خارج‌شده» اجرا
    نمی‌شود؛ دقیقاً همان‌طور که الزام شده است.
    """
    summary = {"status": "ok", "checked": 0, "removed": 0,
               "complete": False, "pages": 0}
    record = storage.get_group(group_id)
    if record is None:
        summary["status"] = "not_configured"
        return summary
    if not record["force_join_enabled"] and not force:
        summary["status"] = "disabled"
        return summary

    lock = _sync_lock(str(record["group_id"]))
    if lock.locked():
        summary["status"] = "busy"
        return summary

    async with lock:
        client = getattr(bot, "client", None)
        channel_input = await ensure_channel_input(bot, record)
        if client is None or channel_input is None:
            summary["status"] = "no_access"
            merge_cursor(record["group_id"], {"status": "no_access"})
            storage.set_sync_state(record["group_id"],
                                   last_sync_time=int(time.time()),
                                   sync_mode=record["sync_mode"])
            return summary
        started_at = int(time.time())
        cap = sync_max_members() or None
        page = await api.iter_members(
            client, channel_input,
            limit=cap,
            budget_seconds=sync_budget(),
            delay=sync_rpc_delay(),
        )
        summary["pages"] = page["pages"]
        users = page["users"]
        if users:
            rows = [{"user_id": getattr(user, "id", None),
                     "access_hash": getattr(user, "access_hash", None)}
                    for user in users]
            stored = storage.set_members_bulk(record["channel_id"], rows,
                                              source=storage.SOURCE_SNAPSHOT)
            summary["checked"] = stored
            for row in rows:
                uid = _safe_int(row["user_id"])
                if uid:
                    _member_cache.pop((record["channel_id"], uid), None)
        summary["complete"] = bool(page["complete"])
        if page["complete"] and users:
            known = storage.members_with_status(record["channel_id"],
                                                storage.STATUS_MEMBER)
            present = {_safe_int(row["user_id"]) for row in rows}
            missing = [row["user_id"] for row in known
                       if _safe_int(row["user_id"]) not in present]
            if missing:
                summary["removed"] = storage.mark_missing_as_not_member(
                    record["channel_id"], missing, before_ts=started_at,
                    source=storage.SOURCE_SNAPSHOT,
                )
                for uid in missing:
                    forget_user(record["channel_id"], uid)
        cursor = {
            "snapshot_started_at": started_at,
            "snapshot_completed_at": int(time.time()) if summary["complete"] else None,
            "complete": summary["complete"],
            "members": len(users),
            "pages": summary["pages"],
            "reason": page.get("reason"),
        }
        if not summary["complete"]:
            summary["status"] = page.get("reason") or "partial"
        merge_cursor(record["group_id"], cursor)
        storage.set_sync_state(
            record["group_id"],
            last_sync_time=int(time.time()) if summary["complete"] else
            record["last_sync_time"],
        )
        refresh_group_cache(record["group_id"])
        _log(bot, "SNAPSHOT "
                  f"group_id={record['group_id']} channel_id={record['channel_id']} "
                  f"members={len(users)} removed={summary['removed']} "
                  f"complete={summary['complete']} status={summary['status']}")
    return summary


def needs_sync(record, now=None) -> bool:
    """آیا وقت اسنپ‌شات جبرانی رسیده است؟"""
    now = time.time() if now is None else now
    routes = channel_routes(record)
    per_user = routes.get(ROUTE_PARTICIPANT) or routes.get(ROUTE_BOT_API)
    needs_fresh_snapshot = (not per_user) and (
        routes.get(ROUTE_SCAN) or routes.get(ROUTE_SNAPSHOT)
    )
    if needs_fresh_snapshot:
        # اسنپ‌شات (تقریباً) تنها منبع حکم است → باید تازه بماند؛ وگرنه
        # «اعتبار ۶۰۰ ثانیه» با «تازه‌سازی ۱۲ ساعته» جمع نمی‌شد.
        state = snapshot_state(record)
        interval = snapshot_interval_for(state.get("members"))
    elif record.get("sync_mode") == storage.SYNC_MODE_EVENT:
        interval = event_snapshot_interval()
    else:
        interval = sync_interval()
    last = record.get("last_sync_time") or 0
    return (now - float(last)) >= interval


def _has_unsynced_groups() -> bool:
    """آیا گروهی هست که هنوز هیچ اسنپ‌شاتی ندارد؟ (برای شروع فوری ناظر)"""
    try:
        refresh_group_cache()
    except Exception:
        return False
    for record in _group_cache.values():
        if not record.get("force_join_enabled"):
            continue
        if not record.get("last_sync_time"):
            return True
    return False


async def watcher(bot, *, logger=None) -> None:
    """ناظر سبک: اسنپ‌شات جبرانی دوره‌ای + پاک‌سازی کش‌های کهنه."""
    delay = start_delay()
    if delay and not _has_unsynced_groups():
        await asyncio.sleep(delay)
    while True:
        try:
            refresh_group_cache()
            for record in list(_group_cache.values()):
                if not record.get("force_join_enabled"):
                    continue
                if not needs_sync(record):
                    continue
                try:
                    await snapshot_sync(bot, record["group_id"])
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _log(bot, f"SYNC FAILED group_id={record['group_id']} "
                              f"error={type(error).__name__}", error=True)
                await asyncio.sleep(max(2.0, sync_rpc_delay()))
            _prune_caches()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log(bot, f"WATCHER LOOP error={type(error).__name__}", error=True)
        await asyncio.sleep(300)


def _prune_caches() -> None:
    """جلوگیری از رشد بی‌نهایت کش حافظه."""
    now = time.time()
    if len(_member_cache) > 50_000:
        for key, (_status, verified_at) in list(_member_cache.items()):
            if now - verified_at > member_ttl():
                _member_cache.pop(key, None)
    for key in [k for k, ts in _notice_sent.items()
                if now - ts > max(notice_cooldown(), 3600)]:
        _notice_sent.pop(key, None)
    for key in [k for k, until in _flood_until.items() if until < now]:
        _flood_until.pop(key, None)


# ---------------------------------------------------------------------------
# تشخیص عیب (برای مالک)
# ---------------------------------------------------------------------------

def _status_label(status) -> str:
    return {
        storage.STATUS_MEMBER: "عضو است ✅",
        storage.STATUS_NOT_MEMBER: "عضو نیست ⛔️",
        storage.STATUS_UNKNOWN: "نامعلوم ❓ (پیام حذف نمی‌شود)",
    }.get(status, str(status))


async def diagnose_short(bot, chat_id, user_id=None,
                         user_entity=None) -> str:
    """پاسخ یک‌خطی «تست عضویت»: کاربر عضو هست یا نیست.

    هیچ جزئیات فنی نشان داده نمی‌شود؛ گزارش کامل با «تشخیص عضویت» است.
    """
    record = cached_group(chat_id) or storage.get_group(chat_id)
    if record is None:
        return "ℹ️ برای این گروه کانالی ثبت نشده است."
    if not user_id:
        return "ℹ️ روی پیام کاربر ریپلای کنید یا @یوزرنیم/آیدی بدهید."
    if user_entity is None:
        client = getattr(bot, "client", None)
        if client is not None:
            try:
                user_entity = await asyncio.wait_for(
                    client.get_entity(int(user_id)), timeout=resolve_timeout())
            except asyncio.CancelledError:
                raise
            except Exception:
                user_entity = None
    status, _layer, _detail = await resolve_status(
        bot, record, user_id, user_entity, force=True
    )
    if status == storage.STATUS_MEMBER:
        return "کاربر در کانال عضو هست"
    if status == storage.STATUS_NOT_MEMBER:
        return "کاربر در کانال عضو نیست"
    return "وضعیت عضویت این کاربر مشخص نشد."


async def diagnose(bot, chat_id, user_id=None, user_entity=None) -> str:
    """گزارش دقیق «چرا حذف شد / چرا نشد» برای یک کاربر.

    هیچ حدسی در خروجی نیست: منبع ``InputPeer``، پاسخ واقعی
    ``getParticipant``، متن خطا و حکم نهایی نوشته می‌شود.
    """
    record = cached_group(chat_id) or storage.get_group(chat_id)
    if record is None:
        return ("ℹ️ برای این گروه کانال اجباری ثبت نشده است. "
                "اول «کانال من <لینک یا آیدی>» را بزنید.")
    lines = ["🧪 تشخیص عضویت اجباری کانال"]
    lines.append(f"کانال: {record.get('channel_title') or '—'}")
    lines.append(f"شناسهٔ کانال: {_digits(record['channel_id'])}")
    lines.append(f"حالت همگام‌سازی: {record.get('sync_mode')}")
    lines.append(f"وضعیت قابلیت: {'روشن ✅' if record.get('force_join_enabled') else 'خاموش ⛔️'}")
    lines.append(f"access_hash کانال: "
                 f"{'دارد' if record.get('channel_access_hash') else 'ندارد ❗️'}")
    routes = channel_routes(record)
    active = [ROUTE_LABELS[r] for r in ROUTE_LABELS if routes.get(r)]
    lines.append("مسیرهای سالم: " + ("، ".join(active) if active else "هیچ ❗️"))
    state = snapshot_state(record)
    if routes.get(ROUTE_SNAPSHOT):
        if state["complete"]:
            lines.append(f"اسنپ‌شات: کامل، {_digits(state['members'])} عضو، "
                         f"{_digits(int(state['age'] or 0))} ثانیه پیش "
                         f"(اعتبار {_digits(snapshot_trust_seconds(state.get('members'))) } ثانیه)")
        else:
            lines.append(f"اسنپ‌شات: ناقص/ندارد ({state.get('reason') or '—'})")

    if not user_id:
        target = recent_decisions(chat_id, limit=5)
        if not target:
            lines.append("")
            lines.append("هنوز هیچ تصمیمی در این گروه ثبت نشده است. "
                         "روی پیام کاربر ریپلای کنید و «تست عضویت» را بزنید.")
            return "\n".join(lines)
        lines.append("")
        lines.append("آخرین تصمیم‌ها:")
        for row in target:
            age = max(0, int(time.time() - row["at"]))
            lines.append(
                f"• {_digits(row['user_id'])} — {_status_label(row['status'])} "
                f"(لایه: {row['layer']}، {_digits(age)} ثانیه پیش)"
                + (f" — {row['detail']}" if row.get("detail") else "")
            )
        return "\n".join(lines)

    # اگر با آیدی عددی صدا زده شدیم، آبجکت کاربر را می‌گیریم تا جست‌وجوی
    # هدفمند (برای access_hash) هم قابل انجام باشد.
    if user_id and user_entity is None:
        client = getattr(bot, "client", None)
        if client is not None:
            try:
                user_entity = await asyncio.wait_for(
                    client.get_entity(int(user_id)), timeout=resolve_timeout()
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                user_entity = None

    # بررسی واقعی همین حالا
    status, layer, stored = await resolve_status(
        bot, record, user_id, user_entity, force=True
    )
    diag = _last_diag.get((channel_key(record["channel_id"]), _safe_int(user_id)))
    exempt = local_exemption(bot, chat_id, user_id, user_entity)
    lines.append("")
    lines.append(f"کاربر: {_digits(user_id)}")
    lines.append(f"معافیت: {exempt or 'ندارد'}")
    if exempt:
        lines.append("نتیجه: این کاربر معاف است و پیامش هرگز حذف نمی‌شود.")
        return "\n".join(lines)
    lines.append(f"InputPeer: {getattr(user_entity, 'access_hash', None) or '—'}")
    lines.append(f"حکم: {_status_label(status)} (لایه: {layer})")
    if stored:
        ago = int(time.time() - float(stored.get("last_verified_at") or 0))
        lines.append(f"آخرین تأیید در دیتابیس: {_digits(ago)} ثانیه پیش "
                     f"(منبع: {stored.get('source') or '—'})")
    if diag:
        if diag.get("routes"):
            lines.append("ترتیب مسیرها: " +
                         " → ".join(ROUTE_LABELS.get(r, r) for r in diag["routes"]))
        if diag.get("trace"):
            lines.append("نتیجهٔ هر مسیر: " + "، ".join(
                f"{ROUTE_LABELS.get(list(item)[0], list(item)[0])}="
                f"{list(item.values())[0]}" for item in diag["trace"]
            )[:400])
        if diag.get("route"):
            lines.append("مسیر برنده: " +
                         f"{ROUTE_LABELS.get(diag['route'], diag['route'])}")
        if diag.get("input_source"):
            lines.append(f"منبع InputPeer در آخرین تلاش: {diag['input_source']}")
        lines.append(f"پاسخ getParticipant: {diag.get('state')}")
        if diag.get("error"):
            lines.append(f"خطا: {diag['error']}")
    error = last_error(record["channel_id"])
    if error:
        lines.append(f"آخرین خطای ثبت‌شده: {error[1]}")
    lines.append("")
    if status == storage.STATUS_NOT_MEMBER:
        lines.append("پیام این کاربر حذف می‌شود و پیام راهنما ارسال خواهد شد.")
    elif status == storage.STATUS_MEMBER:
        lines.append("پیام این کاربر آزاد است.")
    else:
        lines.append("چون وضعیت نامعلوم است، پیام حذف نمی‌شود (سیاست fail-safe).")
        if not active:
            lines.append("هیچ مسیر بررسی سالمی نیست: دستور "
                         "«python -m tools.soroush_membership_probe» را روی "
                         "سرور اجرا کنید تا مسیرهای ممکنِ این حساب مشخص شود.")
        elif routes.get(ROUTE_SNAPSHOT) and not state["complete"]:
            lines.append("اسنپ‌شات اعضا کامل نشده؛ برای تشخیص فوری، ربات را "
                         "ادمین کانال کنید یا فهرست اعضا را مخفی نگذارید.")
        else:
            lines.append("اگر این حالت تکرار شد، ربات رسمی را ادمین کانال کنید "
                         "تا مسیر getChatMember هم فعال شود.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ثبت و راه‌اندازی
# ---------------------------------------------------------------------------

def register(bot, *, logger=None, start_watcher: bool = True) -> bool:
    """هندلر رویداد را ثبت و ناظر را راه‌اندازی می‌کند (idempotent)."""
    marker = id(bot)
    if marker in _registered_bots:
        return False
    client = getattr(bot, "client", None)

    if client is not None and hasattr(client, "on"):
        try:
            from splusthon import events as _events

            async def _on_chat_action(event):
                await handle_chat_action(bot, event)

            client.on(_events.ChatAction())(_on_chat_action)
            _registered_bots.add(marker)
        except Exception as error:
            _log(bot, f"REGISTER FAILED error={type(error).__name__}", error=True)
            return False

    try:
        refresh_group_cache()
    except Exception as error:
        _log(bot, f"CACHE WARM FAILED error={type(error).__name__}", error=True)

    if start_watcher:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(watcher(bot, logger=logger),
                             name="force-join-watcher")
            if confirm_on_join():
                # «تأیید عضویت» بدون نیاز به رویداد ادمینِ کانال
                loop.create_task(confirm_watcher(bot, logger=logger),
                                 name="force-join-confirm")
        except RuntimeError:
            pass
    _log(bot, f"REGISTERED groups={len(_group_cache)}")
    return True


# ---------------------------------------------------------------------------
# دستورهای متنی
# ---------------------------------------------------------------------------

_COMMAND_SPECS = (
    # (عبارت، عمل) — عبارت‌های بلندتر اول
    ("حذف کانال من", "remove"),
    ("تنظیم عضویت اجباری", "enable"),
    ("لغو عضویت اجباری", "disable"),
    ("وضعیت کانال", "status"),
    ("تست عضویت کانال", "diagnose"),
    ("تشخیص عضویت", "diagnose"),
    # «تست عضویت» عمداً کوتاه است: فقط «عضو هست» یا «عضو نیست».
    ("تست عضویت", "diagnose_short"),
    ("کانال من", "register"),
)

_STRIP_PREFIXES = ("!", "/", ".", "،")


def normalize_text(text: str) -> str:
    """یکسان‌سازی متن فرمان.

    تحمل می‌کند: «ي و ك» عربی، «ى/ئ»، نیم‌فاصله، فاصله‌های اضافه، نشانه‌های
    نامرئی RTL/LTR، پیشوندهای ``! / .`` و منشن ربات در ابتدای پیام.
    """
    value = str(text or "").strip()
    if not value:
        return ""
    value = (value.replace("\u200c", " ")
                  .replace("\u200f", "")
                  .replace("\u200e", "")
                  .replace("\u064a", "\u06cc")
                  .replace("\u0643", "\u06a9")
                  .replace("\u0649", "\u06cc")
                  .replace("\u0626", "\u06cc")
                  .replace("\u06c0", "\u0647"))
    value = " ".join(value.split())
    while value[:1] and value[:1] in _STRIP_PREFIXES:
        value = value[1:].lstrip()
    if not value:
        return ""
    # منشن ربات در ابتدای پیام: «@MyBot کانال من …»
    if value.startswith("@"):
        parts = value.split(" ", 1)
        if len(parts) == 2:
            value = parts[1].strip()
    return value


def match_command(text: str):
    """``(action, argument)`` یا ``None``.

    عمل‌ها: ``register`` (با آرگومان)، ``remove``، ``enable``، ``disable``،
    ``status``، ``diagnose`` (گزارش کامل)، ``diagnose_short`` (یک‌خطی).
    """
    value = normalize_text(text)
    if not value:
        return None
    for phrase, action in _COMMAND_SPECS:
        if value == phrase:
            return action, ""
        if action in ("register", "diagnose", "diagnose_short") and \
                value.startswith(phrase + " "):
            return action, value[len(phrase):].strip()
    return None


def is_command(text: str) -> bool:
    return match_command(text) is not None


# ---------------------------------------------------------------------------
# گزارش وضعیت
# ---------------------------------------------------------------------------

def _digits(value) -> str:
    """ارقام فارسی برای نمایش."""
    table = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    return str(value).translate(table)


def _human_age(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 90:
        return f"{_digits(seconds)} ثانیه پیش"
    if seconds < 5400:
        return f"{_digits(seconds // 60)} دقیقه پیش"
    if seconds < 172800:
        return f"{_digits(seconds // 3600)} ساعت پیش"
    return f"{_digits(seconds // 86400)} روز پیش"


def status_text(chat_id) -> str:
    """متن فرمان «وضعیت کانال» — همهٔ اعداد واقعی و بدون ادعا."""
    record = storage.get_group(chat_id)
    if record is None:
        return ("ℹ️ برای این گروه کانال اجباری ثبت نشده است.\n"
                "مالک گروه می‌تواند با «کانال من <لینک یا آیدی>» ثبت کند.")
    stats = storage.stats(chat_id)
    lines = [
        "🔒 وضعیت عضویت اجباری کانال",
        f"وضعیت: {'روشن ✅' if stats['enabled'] else 'خاموش ⛔️'}",
        f"کانال: {stats.get('channel_title') or '—'}",
        f"لینک: {stats.get('channel_link') or '—'}",
        f"شناسهٔ کانال: {_digits(stats['channel_id'])}",
        f"حالت همگام‌سازی: {stats.get('sync_mode') or '—'}",
        "مسیرهای تأیید: " + (
            "، ".join(ROUTE_LABELS[r] for r in ROUTE_LABELS
                      if channel_routes(storage.get_group(chat_id) or {}).get(r))
            or "هیچ ❗️"),
        f"اعتبار اسنپ‌شات: {_digits(snapshot_trust_seconds())} ثانیه",
        f"آخرین همگام‌سازی: {_human_age(stats.get('last_sync_age_seconds'))}",
        f"اعضای تأییدشده: {_digits(stats.get('members', 0))}",
        f"غیرعضوهای ثبت‌شده: {_digits(stats.get('not_members', 0))}",
        f"کل رکوردها: {_digits(stats.get('tracked', 0))}",
    ]
    cursor = stats.get("cursor") or {}
    if cursor:
        lines.append(
            "اسنپ‌شات آخر: "
            f"{'کامل' if cursor.get('complete') else 'ناقص'}"
            f" ({_digits(cursor.get('members') or 0)} عضو)"
        )
    awaiting = len(_awaiting_confirm)
    if awaiting:
        lines.append(f"در انتظار تأیید عضویت: {_digits(awaiting)} نفر")
    error = last_error(record["channel_id"])
    if error:
        ago = max(0, int(time.time() - error[0]))
        lines.append(f"⚠️ آخرین خطای بررسی ({_digits(ago)} ثانیه پیش): {error[1]}")
    recent = recent_decisions(chat_id, limit=3)
    if recent:
        lines.append("")
        lines.append("آخرین تصمیم‌ها:")
        for row in recent:
            lines.append(
                f"• {_digits(row['user_id'])} — {_status_label(row['status'])} "
                f"({row['layer']})"
            )
    lines.append("")
    lines.append("برای بررسی دقیق یک کاربر: روی پیامش ریپلای کنید و "
                 "«تست عضویت» را بزنید.")
    return "\n".join(lines)


def runtime_stats() -> dict:
    """شمارنده‌های زنده (برای «وضعیت کانال» و بنچمارک)."""
    payload = dict(STATS)
    payload["api"] = api.call_stats()
    payload["memory_entries"] = len(_member_cache)
    payload["awaiting_confirm"] = len(_awaiting_confirm)
    payload["rpc_last_minute"] = len(_rpc_window)
    payload["rpc_per_minute"] = rpc_per_minute()
    payload["groups"] = len(_group_cache)
    return payload


def format_runtime_stats(chat_id=None) -> str:
    data = runtime_stats()
    api_calls = data["api"]
    total_api = sum(api_calls.values())
    lines = [
        "📊 آمار مصرف API این قابلیت (از شروع اجرا)",
        f"تصمیم‌گیری: {_digits(data['decisions'])}",
        f"اجازه: {_digits(data['allowed'])}",
        f"حذف (غیرعضو): {_digits(data['blocked'])}",
        f"نامعلوم/آزاد: {_digits(data['unknown_allowed'])}",
        f"کش حافظه: {_digits(data['cache_hits'])}",
        f"کش دیتابیس: {_digits(data['db_hits'])}",
        f"بررسی API: {_digits(data['api_checks'])}",
        f"خطای API: {_digits(data['api_errors'])}",
        f"کل فراخوانی‌های شبکه: {_digits(total_api)}",
        f"پیام راهنما: {_digits(data['notices'])}",
        "رویداد ورود/خروج کانال: "
        f"{_digits(data['events_join'])}/{_digits(data['events_leave'])}",
    ]
    return "\n".join(lines)


def reset_runtime_state() -> None:
    """پاک کردن وضعیت درون‌حافظه (فقط تست)."""
    global _semaphore, _index_at
    _index_at = 0.0
    _group_cache.clear()
    _group_cache_at.clear()
    _channel_index.clear()
    _member_cache.clear()
    _full_scan_cache.clear()
    _inflight.clear()
    _notice_sent.clear()
    _notice_window.clear()
    _flood_until.clear()
    _sync_locks.clear()
    _pending_prefetch.clear()
    _recent_decisions.clear()
    _last_error.clear()
    _last_diag.clear()
    _scan_until.clear()
    _scan_inflight.clear()
    _cheap_probe_until.clear()
    _refresh_tasks.clear()
    _awaiting_confirm.clear()
    _confirm_inflight.clear()
    _rpc_window.clear()
    _semaphore = None
    _registered_bots.clear()
    for key in STATS:
        STATS[key] = 0
    api.reset_counters()
