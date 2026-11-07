"""📊 «نمودار» — ثبتِ عضویت/ریزشِ اعضا و ساختِ گزارشِ رشد گروه.

سه وظیفهٔ کاملاً مستقل:

1. **ثبتِ رخداد** — هر ورود/خروجِ عضو در دفترِ رخدادِ زمان‌دار
   (``membership_events`` در همان SQLite پروژه) ذخیره می‌شود. تاریخِ
   عضویت و ترکِ گروه فقط از همین دفتر خوانده می‌شود؛ هیچ عددی حدس زده
   نمی‌شود.
2. **پایشِ نسخهٔ اول (baseline)** — یک اسنپ‌شاتِ سبک از اعضای فعلی گروه
   گرفته می‌شود تا عضویت‌هایی که *پیش از نصبِ این نسخه* رخ داده‌اند گم
   نشوند. اگر دفترِ رخداد قدیمی‌تر از بازه‌ای نباشد، آن بازه «تکمیل نشده»
   گزارش می‌شود.
3. **ساختِ گزارش** — قالبِ ثابتِ «نمودار» با اعدادِ Bold و نتیجه‌گیریِ
   صادقانه.

قانونِ دادهٔ ناقص (مهم‌ترین قراردادِ این ماژول): اگر داده‌ای موجود نباشد
یا بازهٔ زمانی به‌طور کامل پوشش داده نشده باشد، به‌جای عدد «تکمیل نشده»
نوشته می‌شود. هیچ عددِ تخمینی یا جعلی ساخته نمی‌شود.

بازه‌ها **غلتان** هستند، نه تقویمی: «امروز» = ۲۴ ساعتِ گذشته، «این هفته»
= ۷ روزِ گذشته و «این ماه» = ۳۰ روزِ گذشته. هر ورود/خروجی که در این
پنجره‌ها ثبت شده باشد شمرده می‌شود؛ چند ورود در یک دقیقه هم همه ثبت و
شمارش می‌شوند (هیچ رخدادی به‌خاطرِ نزدیکیِ زمانی حذف نمی‌شود).
"""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from modules.group_id import normalize_group_id
from modules import runtime_db

# ---------------------------------------------------------------------------
# ثابت‌ها
# ---------------------------------------------------------------------------

COMMAND = "نمودار"

#: طولِ هر بازه به روز — «این هفته» هفت روز و «این ماه» سی روز است.
WINDOW_DAYS = {"day": 1, "week": 7, "month": 30}

#: متنی که وقتی داده موجود/کامل نیست داخل همان براکت نوشته می‌شود.
INCOMPLETE = "تکمیل نشده"

VERDICT_GROWTH = "گروه در حال پیشرفت هست"
VERDICT_DECLINE = "گروه در حال افت هست"
VERDICT_UNKNOWN = "داده کافی برای نتیجه‌گیری وجود ندارد"

BOLD = "bold"
QUOTE = "quote"

#: عمرِ نگه‌داریِ رخدادها (روز) — کهنه‌ترها با هرس پاک می‌شوند.
RETENTION_DAYS = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS membership_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('join', 'leave')),
    event_ts REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'event'
);
CREATE INDEX IF NOT EXISTS idx_membership_events_group_time
    ON membership_events(group_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_membership_events_group_user
    ON membership_events(group_id, user_id);
CREATE TABLE IF NOT EXISTS membership_baseline (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    first_ts REAL NOT NULL,
    PRIMARY KEY(group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_membership_baseline_group
    ON membership_baseline(group_id, first_ts);
CREATE TABLE IF NOT EXISTS membership_state (
    group_id TEXT PRIMARY KEY,
    tracker_since REAL NOT NULL,
    baseline_at REAL,
    baseline_complete INTEGER NOT NULL DEFAULT 0,
    last_sync_ts REAL,
    server_count INTEGER,
    server_count_at REAL
);
"""

_schema_lock = threading.Lock()
_schema_ready = False


def _ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        connection = runtime_db.connection()
        connection.executescript(_SCHEMA)
        # ارتقای افزایشی: دیتابیس‌های ساخته‌شده با نسخهٔ اول هم باید بخوانند.
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(membership_state)")}
        if "server_count" not in columns:
            connection.execute(
                "ALTER TABLE membership_state ADD COLUMN server_count INTEGER")
        if "server_count_at" not in columns:
            connection.execute(
                "ALTER TABLE membership_state ADD COLUMN server_count_at REAL")
        _schema_ready = True


def ensure_storage() -> None:
    """ساختِ جدول‌ها (idempotent و ارزان) — برای مسیرهای تست/نگه‌داری."""
    _ensure_schema()


def now_ts() -> float:
    return time.time()


def normalize_chat(chat_id) -> str:
    return normalize_group_id(chat_id)


def _clean_id(user_id):
    value = str(user_id)
    if value in ("", "0", "None", "none", "null"):
        return None
    return value


# ---------------------------------------------------------------------------
# ثبتِ رخداد
# ---------------------------------------------------------------------------

def current_flag(chat_id, user_id):
    """آخرین وضعیتِ عضویتِ کاربر: ``True`` عضو، ``False`` غیرعضو،
    ``None`` اگر هیچ سرنخی نیست."""
    group = normalize_chat(chat_id)
    user = _clean_id(user_id)
    if user is None:
        return None
    _ensure_schema()
    row = runtime_db.query_one(
        "SELECT event FROM membership_events WHERE group_id=? AND user_id=? "
        "ORDER BY event_ts DESC, id DESC LIMIT 1",
        (group, user),
    )
    if row is not None:
        return row[0] == "join"
    base = runtime_db.query_one(
        "SELECT 1 FROM membership_baseline WHERE group_id=? AND user_id=?",
        (group, user),
    )
    return True if base is not None else None


def is_member(chat_id, user_id) -> bool:
    return current_flag(chat_id, user_id) is True


def record_join(chat_id, user_id, ts=None, *, source="event") -> bool:
    """ثبتِ ورودِ یک عضو. اگر کاربر از قبل عضو باشد، رخدادِ تکراری نمی‌سازد."""
    group = normalize_chat(chat_id)
    user = _clean_id(user_id)
    if user is None:
        return False
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    if current_flag(chat_id, user) is True:
        return False
    runtime_db.execute(
        "INSERT INTO membership_events(group_id,user_id,event,event_ts,source) "
        "VALUES(?,?,'join',?,?)",
        (group, user, stamp, str(source)),
    )
    return True


def record_leave(chat_id, user_id, ts=None, *, source="event") -> bool:
    """ثبتِ ترکِ گروه. اگر کاربر از قبل غیرعضو بوده، ریزشِ تکراری نمی‌سازد."""
    group = normalize_chat(chat_id)
    user = _clean_id(user_id)
    if user is None:
        return False
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    if current_flag(chat_id, user) is not True:
        return False
    runtime_db.execute(
        "INSERT INTO membership_events(group_id,user_id,event,event_ts,source) "
        "VALUES(?,?,'leave',?,?)",
        (group, user, stamp, str(source)),
    )
    return True


def member_ids(chat_id) -> list:
    """شناسهٔ اعضای فعلی (رشته) بر اساسِ آخرین وضعیتِ ثبت‌شده."""
    group = normalize_chat(chat_id)
    _ensure_schema()
    rows = runtime_db.query_all(
        "SELECT user_id, event FROM membership_events WHERE group_id=? "
        "ORDER BY event_ts ASC, id ASC",
        (group,),
    )
    state = {}
    for user_id, event in rows:
        state[user_id] = event
    members = {user for user, event in state.items() if event == "join"}
    for row in runtime_db.query_all(
        "SELECT user_id FROM membership_baseline WHERE group_id=?", (group,)
    ):
        if state.get(row[0]) is None:
            members.add(row[0])
    return sorted(members)


def set_server_count(chat_id, total, ts=None) -> None:
    """ثبتِ تعدادِ اعضای اعلام‌شدهٔ خودِ سروش.

    فقط وقتی استفاده می‌شود که فهرست اعضا در دسترس نیست (گروهِ بزرگ یا
    خطای شبکه). این عدد «تعداد اعضای فعلی» را واقعی نگه می‌دارد و هیچ
    رخداد/شمارشِ بازه‌ای از آن ساخته نمی‌شود.
    """
    group = normalize_chat(chat_id)
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    runtime_db.execute(
        "INSERT INTO membership_state(group_id,tracker_since,server_count,"
        "server_count_at) VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET "
        "server_count=excluded.server_count, "
        "server_count_at=excluded.server_count_at",
        (group, stamp, int(total), stamp),
    )


def server_count(chat_id):
    """``(total, at)`` آخرین تعدادِ اعلامیِ سروش، یا ``(None, None)``."""
    state = get_state(chat_id)
    if state.get("server_count") is None:
        return (None, None)
    return (state["server_count"], state["server_count_at"])


def has_any_data(chat_id) -> bool:
    group = normalize_chat(chat_id)
    _ensure_schema()
    row = runtime_db.query_one(
        "SELECT (SELECT COUNT(*) FROM membership_events WHERE group_id=?) + "
        "(SELECT COUNT(*) FROM membership_baseline WHERE group_id=?)",
        (group, group),
    )
    return bool(row and int(row[0]) > 0)


# ---------------------------------------------------------------------------
# وضعیتِ پایش
# ---------------------------------------------------------------------------

def get_state(chat_id) -> dict:
    group = normalize_chat(chat_id)
    _ensure_schema()
    row = runtime_db.query_one(
        "SELECT tracker_since, baseline_at, baseline_complete, last_sync_ts, "
        "server_count, server_count_at FROM membership_state WHERE group_id=?",
        (group,),
    )
    if row is None:
        return {"tracker_since": None, "baseline_at": None,
                "baseline_complete": False, "last_sync_ts": None,
                "server_count": None, "server_count_at": None}
    return {
        "tracker_since": float(row[0]) if row[0] is not None else None,
        "baseline_at": float(row[1]) if row[1] is not None else None,
        "baseline_complete": bool(row[2]),
        "last_sync_ts": float(row[3]) if row[3] is not None else None,
        "server_count": int(row[4]) if row[4] is not None else None,
        "server_count_at": float(row[5]) if row[5] is not None else None,
    }


def ensure_tracker(chat_id, ts=None) -> dict:
    """اگر این گروه هنوز پایش نشده، «شروعِ پایش» همین حالا ثبت می‌شود."""
    group = normalize_chat(chat_id)
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    runtime_db.execute(
        "INSERT INTO membership_state(group_id,tracker_since) VALUES(?,?) "
        "ON CONFLICT(group_id) DO NOTHING",
        (group, stamp),
    )
    return get_state(chat_id)


def baseline_count(chat_id) -> int:
    group = normalize_chat(chat_id)
    _ensure_schema()
    row = runtime_db.query_one(
        "SELECT COUNT(*) FROM membership_baseline WHERE group_id=?", (group,)
    )
    return int(row[0]) if row else 0


def save_baseline(chat_id, ids, ts=None) -> int:
    """ذخیرهٔ اسنپ‌شاتِ اعضای فعلی به‌عنوانِ «وضعیتِ لحظهٔ نصب».

    اعضایی که همین حالا رخدادِ ورود دارند نیازی به ردیفِ اسنپ‌شات ندارند.
    """
    group = normalize_chat(chat_id)
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    inserted = 0
    with runtime_db.transaction():
        for user_id in ids:
            user = _clean_id(user_id)
            if user is None:
                continue
            exists = runtime_db.query_one(
                "SELECT 1 FROM membership_events WHERE group_id=? AND user_id=? "
                "AND event='join' LIMIT 1",
                (group, user),
            )
            if exists is not None:
                continue
            cursor = runtime_db.execute(
                "INSERT INTO membership_baseline(group_id,user_id,first_ts) "
                "VALUES(?,?,?) ON CONFLICT(group_id,user_id) DO NOTHING",
                (group, user, stamp),
            )
            if cursor is not None and cursor.rowcount:
                inserted += 1
        runtime_db.execute(
            "INSERT INTO membership_state(group_id,tracker_since,baseline_at,"
            "baseline_complete) VALUES(?,?,?,1) ON CONFLICT(group_id) DO UPDATE "
            "SET baseline_at=excluded.baseline_at, baseline_complete=1",
            (group, stamp, stamp),
        )
    return inserted


def save_baseline_legacy(chat_id, ids, ts=None) -> int:
    """مثل :func:`save_baseline`، ولی رخدادهای «join» را هم در همان بازهٔ
    قدیمی می‌نویسد تا تاریخِ عضویتِ اعضای فعلی از دست نرود."""
    group = normalize_chat(chat_id)
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    inserted = 0
    with runtime_db.transaction():
        for user_id in ids:
            user = _clean_id(user_id)
            if user is None:
                continue
            if current_flag(chat_id, user) is not None:
                continue
            runtime_db.execute(
                "INSERT INTO membership_events"
                "(group_id,user_id,event,event_ts,source) VALUES(?,?,'join',?,?)",
                (group, user, stamp, "legacy"),
            )
            inserted += 1
        runtime_db.execute(
            "INSERT INTO membership_state(group_id,tracker_since,baseline_at,"
            "baseline_complete) VALUES(?,?,?,1) ON CONFLICT(group_id) DO UPDATE "
            "SET baseline_at=excluded.baseline_at, baseline_complete=1",
            (group, stamp, stamp),
        )
    return inserted


def start_tracking(chat_id, ts=None) -> bool:
    """علامت‌گذاریِ «پایشِ این گروه شروع شد» بدونِ تکیه بر فهرست اعضا.

    وقتی فهرست اعضا در دسترس نیست (گروهِ بزرگ یا خطای شبکه) هم باید
    ورود/خروج‌های ثبت‌شده شمارش شوند؛ پس فقط شروعِ پایش ثبت می‌شود و هیچ
    شناسه‌ای ذخیره نمی‌گردد. تعداد اعضا در این حالت از خودِ سروش می‌آید.
    """
    state = ensure_tracker(chat_id, ts)
    if state["baseline_complete"]:
        return False
    save_baseline(chat_id, [], ts)
    return True


def set_baseline_incomplete(chat_id, ts=None) -> None:
    """اسنپ‌شات ناقص ماند: زمانِ پایش ثبت می‌شود ولی داده‌های قدیمی‌تر
    از آن «در دسترس» نیستند."""
    group = normalize_chat(chat_id)
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    runtime_db.execute(
        "INSERT INTO membership_state(group_id,tracker_since,baseline_complete) "
        "VALUES(?,?,0) ON CONFLICT(group_id) DO UPDATE SET "
        "tracker_since=MIN(membership_state.tracker_since, excluded.tracker_since), "
        "baseline_complete=0",
        (group, stamp),
    )


def mark_synced(chat_id, ts=None) -> None:
    group = normalize_chat(chat_id)
    stamp = float(ts if ts is not None else now_ts())
    _ensure_schema()
    runtime_db.execute(
        "UPDATE membership_state SET last_sync_ts=? WHERE group_id=?",
        (stamp, group),
    )


def sync_members(chat_id, current_ids, ts=None) -> dict:
    """همگام‌سازیِ فهرستِ زندهٔ اعضا با دفترِ رخداد.

    * عضوِ تازه در فهرست که در دفتر نبود → رخدادِ «join» با همین زمان.
    * عضوی که از فهرست حذف شده → رخدادِ «leave» با همین زمان.
    * اگر هنوز اسنپ‌شات نداریم، ساختِ اسنپ‌شات مقدّم است (وگرنه همهٔ
      اعضای فعلی «ورودِ امروز» شمرده می‌شدند).
    """
    stamp = float(ts if ts is not None else now_ts())
    state = ensure_tracker(chat_id, stamp)
    current = [user for user in (_clean_id(value) for value in current_ids)
               if user is not None]
    result = {"joined": 0, "left": 0, "baseline": False}
    if not state["baseline_complete"]:
        save_baseline(chat_id, current, stamp)
        result["baseline"] = True
        mark_synced(chat_id, stamp)
        return result
    known = set(member_ids(chat_id))
    incoming = set(current)
    for user in current:
        if user not in known and record_join(chat_id, user, stamp, source="sync"):
            result["joined"] += 1
    for user in known - incoming:
        if record_leave(chat_id, user, stamp, source="sync"):
            result["left"] += 1
    mark_synced(chat_id, stamp)
    return result


# ---------------------------------------------------------------------------
# بازه‌های زمانی و شمارش
# ---------------------------------------------------------------------------

def _now_local():
    from modules.time_utils import now_local

    return now_local()


#: طولِ هر بازه به ثانیه: «امروز» ۲۴ ساعت، «این هفته» ۷ روز، «این ماه» ۳۰ روز.
WINDOW_SECONDS = {"day": 24 * 3600, "week": 7 * 24 * 3600, "month": 30 * 24 * 3600}


def now_ts_ref(ts=None) -> float:
    """لحظهٔ مرجعِ گزارش (پیش‌فرض: همین حالا)."""
    return float(ts) if ts is not None else now_ts()


def window_start(key: str, ts=None) -> float:
    """شروعِ بازهٔ غلتان: «همین حالا منهای ۲۴ ساعت / ۷ روز / ۳۰ روز».

    دقیقاً همان چیزی که لازم است: هر کسی در طولِ آن پنجره عضو شده یا رفته
    باشد شمرده می‌شود، حتی اگر چند نفر در یک دقیقهٔ همان پنجره آمده باشند.
    """
    return now_ts_ref(ts) - WINDOW_SECONDS[key]


def window_end(ts=None) -> float:
    """پایانِ بازه = همین حالا."""
    return now_ts_ref(ts)


#: نامِ خوانای هر بازه برای پیام‌های توضیحی.
WINDOW_LABELS = {"day": "۲۴ ساعت", "week": "۷ روز", "month": "۳۰ روز"}


def _count_events(chat_id, event: str, since: float, until=None) -> int:
    group = normalize_chat(chat_id)
    _ensure_schema()
    if until is None:
        row = runtime_db.query_one(
            "SELECT COUNT(*) FROM membership_events WHERE group_id=? AND "
            "event=? AND event_ts>=?",
            (group, event, float(since)),
        )
    else:
        row = runtime_db.query_one(
            "SELECT COUNT(*) FROM membership_events WHERE group_id=? AND "
            "event=? AND event_ts>=? AND event_ts<=?",
            (group, event, float(since), float(until)),
        )
    return int(row[0]) if row else 0


def tracking_start(chat_id, ts=None):
    """از چه لحظه‌ای داده‌های این گروه کامل ثبت شده است؟"""
    state = ensure_tracker(chat_id, ts)
    if not state["baseline_complete"]:
        return None
    return state["baseline_at"]


def _window_covered(chat_id, key: str, ts=None):
    """آیا برای این پنجره دادهٔ ثبت‌شده داریم؟

    تا وقتی پایشِ گروه شروع نشده (هیچ اسنپ‌شاتی ساخته نشده) «تکمیل نشده»
    گزارش می‌شود. بعد از آن، هر ورود/خروجی که در پنجره ثبت شده باشد
    شمرده می‌شود؛ حتی اگر ربات همین یک دقیقه پیش نصب شده باشد و یک نفر
    همین حالا عضو شده باشد.
    """
    return bool(ensure_tracker(chat_id, ts)["baseline_complete"])




def joins(chat_id, key: str, ts=None):
    """تعدادِ ورود در پنجرهٔ غلتان — ``None`` اگر پنجره کامل پوشش نداشته باشد."""
    _ensure_schema()
    if not _window_covered(chat_id, key, ts):
        return None
    return _count_events(chat_id, "join", window_start(key, ts), window_end(ts))


def leaves(chat_id, key: str, ts=None):
    """تعدادِ ترک در پنجرهٔ غلتان — ``None`` اگر پوشش کامل نباشد."""
    _ensure_schema()
    if not _window_covered(chat_id, key, ts):
        return None
    return _count_events(chat_id, "leave", window_start(key, ts), window_end(ts))


#: تعدادِ اعلامیِ سروش تا این مدت معتبر است؛ کهنه‌تر از آن استفاده نمی‌شود.
SERVER_COUNT_TTL_SECONDS = 24 * 3600


def current_members(chat_id):
    """تعداد اعضای فعلی (لحظه‌ای).

    اولویت اول **عددِ خودِ سروش** است (`participants_count`), چون مرجع و
    دقیق است و به کامل بودنِ فهرست اعضا وابسته نیست. اگر عددِ تازه‌ای از
    سروش نبود، شمارشِ دفترِ رخداد استفاده می‌شود و در نهایت ``None``
    (نه صفرِ جعلی).
    """
    total, stamp = server_count(chat_id)
    if total is not None:
        if stamp is None or (now_ts() - float(stamp)) <= SERVER_COUNT_TTL_SECONDS:
            return total
    if has_any_data(chat_id):
        return len(member_ids(chat_id))
    return total


def nets(chat_id, key: str, ts=None):
    """``(joined, left)`` همین پنجره؛ همان چیزی که در گزارش چاپ می‌شود."""
    if not _window_covered(chat_id, key, ts):
        return (None, None)
    start, end = window_start(key, ts), window_end(ts)
    return (_count_events(chat_id, "join", start, end),
            _count_events(chat_id, "leave", start, end))


# ---------------------------------------------------------------------------
# نتیجه‌گیری
# ---------------------------------------------------------------------------

def evaluate(join_counts: dict, leave_counts: dict,
             join_nets: dict, leave_nets: dict) -> str:
    """«نتیجه گیری» بر پایهٔ مقایسهٔ عضو شده‌ها و ریزش‌ها.

    اول با بازه‌های کامل (سقفِ هر بازه = مجموعِ ورودها و ترک‌ها) و اگر
    هیچ بازهٔ کاملی نبود، با داده‌های همان امروز مقایسه می‌کند.
    """
    def _sum(box):
        values = [value for value in box.values() if value is not None]
        return sum(values) if values else None

    total_join, total_leave = _sum(join_counts), _sum(leave_counts)
    if total_join is None and total_leave is None:
        total_join, total_leave = _sum(join_nets), _sum(leave_nets)
    elif total_join is None:
        total_join = _sum(join_nets)
    elif total_leave is None:
        total_leave = _sum(leave_nets)
    if total_join is None or total_leave is None:
        return VERDICT_UNKNOWN
    if total_join > total_leave:
        return VERDICT_GROWTH
    if total_leave > total_join:
        return VERDICT_DECLINE
    return VERDICT_UNKNOWN


# ---------------------------------------------------------------------------
# قالب‌بندی
# ---------------------------------------------------------------------------

#: ارقامِ بولدِ ریاضی (𝟬..𝟵) — همان استایلِ قالبِ خواسته‌شده.
_BOLD_DIGITS = str.maketrans("0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵")
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def bold_digits(value) -> str:
    return str(value).translate(_BOLD_DIGITS)


def to_fa(number) -> str:
    return str(number).translate(_FA_DIGITS)


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _cell(value) -> str:
    return INCOMPLETE if value is None else bold_digits(value)


def build_report(chat_id, ts=None, title=None) -> tuple:
    """گزارشِ کاملِ «نمودار» → ``(text, entities)``.

    ``entities`` فهرستی از ``(offset, length, "bold" | "quote")`` با واحدِ
    UTF-16 است (استانداردِ سروش پلاس/تلگرام).
    """
    state = ensure_tracker(chat_id, ts)
    complete = state["baseline_complete"]

    current = current_members(chat_id)
    join_counts = {key: (joins(chat_id, key, ts) if complete else None)
                   for key in ("day", "week", "month")}
    leave_counts = {key: (leaves(chat_id, key, ts) if complete else None)
                    for key in ("day", "week", "month")}
    join_nets = {key: nets(chat_id, key, ts)[0] for key in ("day", "week", "month")}
    leave_nets = {key: nets(chat_id, key, ts)[1] for key in ("day", "week", "month")}
    verdict = evaluate(join_counts, leave_counts, join_nets, leave_nets)

    #: (متن، بولد؟، داخلِ نقل‌قول؟) — دقیقاً همان قالبِ خواسته‌شده،
    #: بدونِ هیچ سرتیتر یا خطِ اضافه.
    lines = [
        ("⟳ تعداد اعضای فعلی : 『  " + _cell(current) + "  』", True, True),
        ("", False, False),
        ("〄 اعضای عضو شده", True, False),
        (" │امروز : 「 " + _cell(join_counts["day"]) + " 」", False, True),
        (" │این هفته :  「 " + _cell(join_counts["week"]) + " 」", False, True),
        (" │این ماه : 「 " + _cell(join_counts["month"]) + " 」", False, True),
        ("", False, False),
        ("〄  ریزش اعضا", True, False),
        ("│ امروز : 「 " + _cell(leave_counts["day"]) + " 」", False, True),
        ("│این هفته :  「 " + _cell(leave_counts["week"]) + " 」", False, True),
        ("│این ماه : 「 " + _cell(leave_counts["month"]) + " 」", False, True),
        ("", False, False),
        ("« نتیجه گیری : " + verdict + " »", True, True),
    ]

    parts, entities, offset = [], [], 0
    for line, bold, quote in lines:
        chunk = line + "\n"
        parts.append(chunk)
        length = _u16(chunk)
        if quote:
            entities.append((offset, length, QUOTE))
        if bold:
            entities.append((offset, length, BOLD))
        offset += length
    return "".join(parts), entities


def admin_help_text() -> tuple:
    """متن و بازه‌های Bold/نقل‌قولِ راهنمای «لیست ادمینی»."""
    text = ("📊 برای دیدن نمودار رشد گروه یا ریزش گروه دستور\n"
            "« نمودار »\n"
            "را ارسال کنید.")
    quoted = "« نمودار »"
    start = text.index(quoted)
    spans = [
        (0, _u16(text), BOLD),
        (_u16(text[:start]), _u16(quoted), QUOTE),
    ]
    return text, spans


# ---------------------------------------------------------------------------
# نگه‌داری
# ---------------------------------------------------------------------------

def prune(retention_days: int = RETENTION_DAYS) -> int:
    """پاک‌کردنِ رخدادهای کهنه تا دیتابیس سبک بماند."""
    _ensure_schema()
    cutoff = now_ts() - max(1, int(retention_days)) * 86400
    with runtime_db.transaction():
        cursor = runtime_db.execute(
            "DELETE FROM membership_events WHERE event_ts<?", (cutoff,)
        )
        return max(int(getattr(cursor, "rowcount", 0) or 0), 0)


def stats(chat_id) -> dict:
    """خلاصهٔ وضعیتِ پایش (بدونِ هیچ شناسهٔ کاربری)."""
    group = normalize_chat(chat_id)
    _ensure_schema()
    events = runtime_db.query_one(
        "SELECT COUNT(*) FROM membership_events WHERE group_id=?", (group,)
    )
    return {
        "events": int(events[0]) if events else 0,
        "baseline": baseline_count(chat_id),
        "state": get_state(chat_id),
    }
