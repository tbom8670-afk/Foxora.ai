"""🗄 ذخیره‌سازی پایدار «عضویت اجباری کانال» روی SQLite (WAL).

داده‌ها **حتماً** باید از ری‌استارت ربات جان سالم به‌در ببرند؛ به همین دلیل
برخلاف چند قابلیت قدیمی‌ترِ این پروژه (که JSON نوشتند) اینجا از همان
زیرساخت SQLite مشترک ``modules.runtime_db`` استفاده می‌شود: WAL،
``synchronous`` بالا، ``busy_timeout`` و یک تراکنش به‌ازای هر تغییر.

دو جدول (طبق طراحی مصوب):

``force_join_groups``
    برای هر گروه یک کانال اجباری: شناسهٔ کانال، ``access_hash`` (کش، برای
    صفر-API شدن مسیر داغ)، لینک، فعال/غیرفعال، زمان آخرین همگام‌سازی،
    ``last_update_cursor`` (JSON محلی) و ``sync_mode``.

``force_join_members``
    وضعیت هر کاربر در هر کانال: ``member`` / ``not_member`` / ``unknown``،
    زمان آخرین تأیید، منبع (``event_join`` / ``event_leave`` / ``rpc`` /
    ``snapshot``) و ``access_hash`` کاربر برای بررسی تک‌کاربره.

نکتهٔ کلیدی: کش ``access_hash`` (هم کانال و هم کاربر) همان چیزی است که
بعد از ری‌استارت اجازه می‌دهد بررسی عضویت با **یک** فراخوانی انجام شود و
هیچ‌گاه لازم نشود فهرست اعضا پیمایش شود.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterable, Optional

from modules import runtime_db
from modules.force_join_ids import canonical_group_key, channel_key

STATUS_MEMBER = "member"
STATUS_NOT_MEMBER = "not_member"
STATUS_UNKNOWN = "unknown"

SOURCE_EVENT_JOIN = "event_join"
SOURCE_EVENT_LEAVE = "event_leave"
SOURCE_RPC = "rpc"
SOURCE_SNAPSHOT = "snapshot"
#: مسیر رسمی Bot API (``getChatMember``) — همان روشی که ربات‌های تلگرامی
#: استفاده می‌کنند؛ دقیق و مستقل از پیمایش فهرست اعضا.
SOURCE_BOT_API = "bot_api"
#: جست‌وجوی هدفمند نام کاربر در فهرست اعضا (``ChannelParticipantsSearch``)
SOURCE_SEARCH = "search"
#: پیمایش هدفمند فهرست اعضا برای همان یک کاربر (حکم قطعی با سقف)
SOURCE_SCAN = "scan"

SYNC_MODE_EVENT = "event"
SYNC_MODE_SNAPSHOT = "snapshot"
SYNC_MODE_PROBE = "probe"
#: بررسی تک‌کاربره از راه رسمی Bot API (getChatMember)
SYNC_MODE_BOT_API = "bot_api"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS force_join_groups (
    group_id            INTEGER PRIMARY KEY,
    channel_id          INTEGER NOT NULL,
    channel_access_hash INTEGER,
    channel_link        TEXT,
    channel_title       TEXT,
    force_join_enabled  INTEGER NOT NULL DEFAULT 1,
    last_sync_time      INTEGER,
    last_update_cursor  TEXT,
    sync_mode           TEXT NOT NULL DEFAULT 'snapshot',
    created_at          INTEGER,
    updated_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_force_join_groups_channel
    ON force_join_groups(channel_id);

CREATE TABLE IF NOT EXISTS force_join_members (
    user_id           INTEGER NOT NULL,
    channel_id        INTEGER NOT NULL,
    membership_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK(membership_status IN ('member', 'not_member', 'unknown')),
    last_verified_at  INTEGER,
    source            TEXT,
    join_date         INTEGER,
    access_hash       INTEGER,
    PRIMARY KEY (user_id, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_force_join_members_channel_status
    ON force_join_members(channel_id, membership_status);
CREATE INDEX IF NOT EXISTS idx_force_join_members_verified
    ON force_join_members(channel_id, last_verified_at);
"""

_local = threading.local()
_override_path: Optional[str] = None
_schema_lock = threading.RLock()
_schema_ready = False


# ---------------------------------------------------------------------------
# اتصال
# ---------------------------------------------------------------------------

def set_db_path(path) -> None:
    """مسیر دیتابیس را برای تست/ابزار override می‌کند.

    ``None`` یعنی بازگشت به دیتابیس مشترک ``modules.runtime_db``.
    """
    global _override_path, _schema_ready
    with _schema_lock:
        _override_path = str(path) if path else None
        _schema_ready = False
        conn = getattr(_local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None


def _connection():
    """اتصال فعال (مشترک یا override) با اسکیمای آماده."""
    if _override_path is None:
        return runtime_db.connection()
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(
        _override_path, timeout=10.0, check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=FULL")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    return conn


def ensure_schema() -> None:
    """ساخت جدول‌ها به‌صورت idempotent (بدون حذف دادهٔ موجود)."""
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        conn = _connection()
        conn.executescript(_SCHEMA)
        _schema_ready = True


@contextmanager
def transaction():
    """تراکنش کوتاه و ایمن (BEGIN IMMEDIATE → COMMIT/ROLLBACK)."""
    ensure_schema()
    conn = _connection()
    with _schema_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _rows(sql, params=()) -> list:
    ensure_schema()
    with _schema_lock:
        return list(_connection().execute(sql, params).fetchall())


def _row(sql, params=()):
    ensure_schema()
    with _schema_lock:
        return _connection().execute(sql, params).fetchone()


def _now() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# گروه‌ها / کانال‌های اجباری
# ---------------------------------------------------------------------------

def _group_row_to_dict(row) -> Optional[dict]:
    if row is None:
        return None
    try:
        cursor = json.loads(row["last_update_cursor"]) if row["last_update_cursor"] else {}
    except Exception:
        cursor = {}
    return {
        "group_id": int(row["group_id"]),
        "channel_id": int(row["channel_id"]),
        "channel_access_hash": (
            int(row["channel_access_hash"])
            if row["channel_access_hash"] is not None else None
        ),
        "channel_link": row["channel_link"],
        "channel_title": row["channel_title"],
        "force_join_enabled": bool(row["force_join_enabled"]),
        "last_sync_time": (
            int(row["last_sync_time"]) if row["last_sync_time"] is not None else None
        ),
        "last_update_cursor": cursor,
        "sync_mode": row["sync_mode"] or SYNC_MODE_SNAPSHOT,
    }


def get_group(group_id) -> Optional[dict]:
    """تنظیمات کانال اجباری یک گروه (یا ``None``)."""
    key = canonical_group_key(group_id)
    if not key:
        return None
    return _group_row_to_dict(
        _row("SELECT * FROM force_join_groups WHERE group_id=?", (key,))
    )


def list_groups(*, enabled_only: bool = False) -> list:
    """همهٔ گروه‌های دارای کانال اجباری."""
    if enabled_only:
        rows = _rows(
            "SELECT * FROM force_join_groups WHERE force_join_enabled=1 "
            "ORDER BY group_id"
        )
    else:
        rows = _rows("SELECT * FROM force_join_groups ORDER BY group_id")
    return [record for record in (_group_row_to_dict(r) for r in rows) if record]


def groups_for_channel(channel_id) -> list:
    """گروه‌هایی که این کانال، کانال اجباری‌شان است (برای رویدادهای کانال)."""
    cid = channel_key(channel_id)
    if not cid:
        return []
    rows = _rows(
        "SELECT * FROM force_join_groups WHERE channel_id=? ORDER BY group_id",
        (cid,),
    )
    return [record for record in (_group_row_to_dict(r) for r in rows) if record]


def save_group(
    group_id,
    *,
    channel_id,
    channel_access_hash=None,
    channel_link=None,
    channel_title=None,
    enabled=True,
    sync_mode=SYNC_MODE_SNAPSHOT,
) -> dict:
    """ثبت یا به‌روزرسانی کانال اجباری یک گروه.

    ``access_hash`` قبلی حفظ می‌شود مگر مقدار تازه‌تری داده شود؛ همین کش است
    که بعد از ری‌استارت مانع فراخوانی resolve می‌شود.
    """
    key = canonical_group_key(group_id)
    cid = channel_key(channel_id)
    if not key or not cid:
        raise ValueError("group_id/channel_id نامعتبر است")
    now = _now()
    with transaction() as conn:
        existing = conn.execute(
            "SELECT channel_id, channel_access_hash, channel_link, channel_title "
            "FROM force_join_groups WHERE group_id=?",
            (key,),
        ).fetchone()
        access_hash = channel_access_hash
        if access_hash is None and existing is not None and \
                int(existing["channel_id"]) == cid:
            access_hash = existing["channel_access_hash"]
        link = channel_link
        if link is None and existing is not None:
            link = existing["channel_link"]
        title = channel_title
        if title is None and existing is not None:
            title = existing["channel_title"]
        conn.execute(
            "INSERT INTO force_join_groups "
            "(group_id, channel_id, channel_access_hash, channel_link, "
            " channel_title, force_join_enabled, sync_mode, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET "
            "  channel_id=excluded.channel_id, "
            "  channel_access_hash=COALESCE(excluded.channel_access_hash, "
            "                                force_join_groups.channel_access_hash), "
            "  channel_link=COALESCE(excluded.channel_link, force_join_groups.channel_link), "
            "  channel_title=COALESCE(excluded.channel_title, force_join_groups.channel_title), "
            "  force_join_enabled=excluded.force_join_enabled, "
            "  sync_mode=excluded.sync_mode, "
            "  updated_at=excluded.updated_at",
            (
                key, cid, access_hash, link, title, 1 if enabled else 0,
                sync_mode or SYNC_MODE_SNAPSHOT, now, now,
            ),
        )
        if existing is not None and int(existing["channel_id"]) != cid:
            # کانال عوض شده است: وضعیت اعضای کانال قبلی دیگر بی‌معناست.
            conn.execute(
                "DELETE FROM force_join_members WHERE channel_id=?",
                (int(existing["channel_id"]),),
            )
    return get_group(key)


def set_enabled(group_id, enabled: bool) -> Optional[dict]:
    key = canonical_group_key(group_id)
    if not key:
        return None
    with transaction() as conn:
        conn.execute(
            "UPDATE force_join_groups SET force_join_enabled=?, updated_at=? "
            "WHERE group_id=?",
            (1 if enabled else 0, _now(), key),
        )
    return get_group(key)


def set_channel_access_hash(channel_id, access_hash) -> None:
    cid = channel_key(channel_id)
    if not cid or access_hash is None:
        return
    with transaction() as conn:
        conn.execute(
            "UPDATE force_join_groups SET channel_access_hash=?, updated_at=? "
            "WHERE channel_id=?",
            (int(access_hash), _now(), cid),
        )


def set_sync_state(group_id, *, last_sync_time=None, cursor=None,
                   sync_mode=None) -> None:
    """ذخیرهٔ زمان/کرسر همگام‌سازی (کرسر JSON محلی است، نه توکن سرور)."""
    key = canonical_group_key(group_id)
    if not key:
        return
    payload = json.dumps(cursor, ensure_ascii=False) if cursor is not None else None
    with transaction() as conn:
        if payload is None:
            conn.execute(
                "UPDATE force_join_groups SET last_sync_time=COALESCE(?, last_sync_time), "
                "sync_mode=COALESCE(?, sync_mode), updated_at=? WHERE group_id=?",
                (last_sync_time, sync_mode, _now(), key),
            )
        else:
            conn.execute(
                "UPDATE force_join_groups SET last_sync_time=COALESCE(?, last_sync_time), "
                "last_update_cursor=?, sync_mode=COALESCE(?, sync_mode), updated_at=? "
                "WHERE group_id=?",
                (last_sync_time, payload, sync_mode, _now(), key),
            )


def delete_group(group_id) -> bool:
    """حذف کامل کانال اجباری و وضعیت اعضای آن."""
    key = canonical_group_key(group_id)
    if not key:
        return False
    with transaction() as conn:
        row = conn.execute(
            "SELECT channel_id FROM force_join_groups WHERE group_id=?", (key,)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "DELETE FROM force_join_members WHERE channel_id=?",
            (int(row["channel_id"]),),
        )
        conn.execute("DELETE FROM force_join_groups WHERE group_id=?", (key,))
    return True


# ---------------------------------------------------------------------------
# وضعیت اعضا
# ---------------------------------------------------------------------------

def get_member(channel_id, user_id) -> Optional[dict]:
    cid = channel_key(channel_id)
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    if not cid or not uid:
        return None
    row = _row(
        "SELECT * FROM force_join_members WHERE user_id=? AND channel_id=?",
        (uid, cid),
    )
    if row is None:
        return None
    return {
        "user_id": int(row["user_id"]),
        "channel_id": int(row["channel_id"]),
        "membership_status": row["membership_status"],
        "last_verified_at": (
            int(row["last_verified_at"]) if row["last_verified_at"] is not None else None
        ),
        "source": row["source"],
        "join_date": (
            int(row["join_date"]) if row["join_date"] is not None else None
        ),
        "access_hash": (
            int(row["access_hash"]) if row["access_hash"] is not None else None
        ),
    }


def set_member(
    channel_id,
    user_id,
    status: str,
    *,
    source: Optional[str] = None,
    access_hash=None,
    join_date=None,
    verified_at=None,
) -> Optional[dict]:
    """ثبت وضعیت یک کاربر؛ ``access_hash`` قبلی حفظ می‌شود."""
    cid = channel_key(channel_id)
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    if not cid or not uid:
        return None
    if status not in (STATUS_MEMBER, STATUS_NOT_MEMBER, STATUS_UNKNOWN):
        status = STATUS_UNKNOWN
    stamp = int(verified_at if verified_at is not None else _now())
    with transaction() as conn:
        conn.execute(
            "INSERT INTO force_join_members "
            "(user_id, channel_id, membership_status, last_verified_at, source, "
            " join_date, access_hash) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, channel_id) DO UPDATE SET "
            "  membership_status=excluded.membership_status, "
            "  last_verified_at=excluded.last_verified_at, "
            "  source=COALESCE(excluded.source, force_join_members.source), "
            "  join_date=COALESCE(excluded.join_date, force_join_members.join_date), "
            "  access_hash=COALESCE(excluded.access_hash, force_join_members.access_hash)",
            (uid, cid, status, stamp, source, join_date, access_hash),
        )
    return get_member(cid, uid)


def set_members_bulk(channel_id, rows: Iterable, *, source=SOURCE_SNAPSHOT,
                     status=STATUS_MEMBER, verified_at=None) -> int:
    """درج دسته‌ای اعضای اسنپ‌شات (کارآمد و بدون یک تراکنش به‌ازای نفر)."""
    cid = channel_key(channel_id)
    if not cid:
        return 0
    stamp = int(verified_at if verified_at is not None else _now())
    payload = []
    for item in rows or ():
        if isinstance(item, dict):
            uid = item.get("user_id") or item.get("id")
            access_hash = item.get("access_hash")
        else:
            uid = getattr(item, "id", item)
            access_hash = getattr(item, "access_hash", None)
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        if not uid:
            continue
        payload.append((uid, cid, status, stamp, source, access_hash))
    if not payload:
        return 0
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO force_join_members "
            "(user_id, channel_id, membership_status, last_verified_at, source, "
            " access_hash) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id, channel_id) DO UPDATE SET "
            "  membership_status=excluded.membership_status, "
            "  last_verified_at=excluded.last_verified_at, "
            "  source=excluded.source, "
            "  access_hash=COALESCE(excluded.access_hash, force_join_members.access_hash)",
            payload,
        )
    return len(payload)


def set_member_access_hash(channel_id, user_id, access_hash) -> bool:
    """فقط ``access_hash`` کاربر را ذخیره می‌کند؛ وضعیت عضویت دست‌نخورده می‌ماند.

    این همان چیزی است که «بررسی بعدی = یک فراخوانی» را تضمین می‌کند، حتی
    وقتی رویداد/پیام ``access_hash`` نداشته باشد.
    """
    cid = channel_key(channel_id)
    try:
        uid = int(user_id)
        access = int(access_hash)
    except (TypeError, ValueError):
        return False
    if not cid or not uid or not access:
        return False
    with transaction() as conn:
        conn.execute(
            "INSERT INTO force_join_members "
            "(user_id, channel_id, membership_status, last_verified_at, source, "
            " access_hash) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id, channel_id) DO UPDATE SET "
            "  access_hash=excluded.access_hash",
            (uid, cid, STATUS_UNKNOWN, None, None, access),
        )
    return True


def members_with_status(channel_id, status: str = STATUS_MEMBER) -> list:
    cid = channel_key(channel_id)
    if not cid:
        return []
    rows = _rows(
        "SELECT user_id, last_verified_at, access_hash FROM force_join_members "
        "WHERE channel_id=? AND membership_status=?",
        (cid, status),
    )
    return [
        {
            "user_id": int(row["user_id"]),
            "last_verified_at": (
                int(row["last_verified_at"])
                if row["last_verified_at"] is not None else None
            ),
            "access_hash": (
                int(row["access_hash"]) if row["access_hash"] is not None else None
            ),
        }
        for row in rows
    ]


def member_count(channel_id, status: Optional[str] = None) -> int:
    cid = channel_key(channel_id)
    if not cid:
        return 0
    if status is None:
        row = _row(
            "SELECT COUNT(*) AS n FROM force_join_members WHERE channel_id=?",
            (cid,),
        )
    else:
        row = _row(
            "SELECT COUNT(*) AS n FROM force_join_members "
            "WHERE channel_id=? AND membership_status=?",
            (cid, status),
        )
    return int(row["n"]) if row is not None else 0


def mark_missing_as_not_member(channel_id, user_ids: Iterable, *,
                               before_ts: int, source=SOURCE_SNAPSHOT) -> int:
    """کاربرانی که در اسنپ‌شات **کامل** نبودند را «خارج‌شده» ثبت می‌کند.

    شرط ``last_verified_at <= before_ts`` از این جلوگیری می‌کند که رویداد
    ورودِ تازه (که بعد از شروع اسنپ‌شات رسیده) با دادهٔ کهنهٔ اسنپ‌شات
    باطل شود.
    """
    cid = channel_key(channel_id)
    if not cid:
        return 0
    ids = []
    for value in user_ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number:
            ids.append(number)
    if not ids:
        return 0
    stamp = _now()
    changed = 0
    with transaction() as conn:
        for chunk_start in range(0, len(ids), 400):
            chunk = ids[chunk_start:chunk_start + 400]
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                "UPDATE force_join_members SET membership_status=?, "
                "last_verified_at=?, source=? "
                f"WHERE channel_id=? AND user_id IN ({placeholders}) "
                "AND membership_status='member' "
                "AND COALESCE(last_verified_at, 0) <= ?",
                # ترتیب پارامترها: ابتدا مقادیر SET، سپس شرط‌های WHERE
                (STATUS_NOT_MEMBER, stamp, source, cid, *chunk, int(before_ts)),
            )
            changed += cursor.rowcount or 0
    return changed


def purge_channel_members(channel_id) -> int:
    cid = channel_key(channel_id)
    if not cid:
        return 0
    with transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM force_join_members WHERE channel_id=?", (cid,)
        )
        return cursor.rowcount or 0


def stats(group_id) -> dict:
    """آمار خلاصه برای فرمان «وضعیت کانال»."""
    record = get_group(group_id)
    if record is None:
        return {"configured": False}
    cid = record["channel_id"]
    now = _now()
    return {
        "configured": True,
        "enabled": record["force_join_enabled"],
        "channel_id": cid,
        "channel_link": record["channel_link"],
        "channel_title": record["channel_title"],
        "sync_mode": record["sync_mode"],
        "last_sync_time": record["last_sync_time"],
        "last_sync_age_seconds": (
            max(0, now - int(record["last_sync_time"]))
            if record["last_sync_time"] else None
        ),
        "cursor": record["last_update_cursor"],
        "members": member_count(cid, STATUS_MEMBER),
        "not_members": member_count(cid, STATUS_NOT_MEMBER),
        "unknown": member_count(cid, STATUS_UNKNOWN),
        "tracked": member_count(cid),
    }


def reset_caches() -> None:
    """بازنشانی وضعیت ماژول (فقط تست)."""
    global _schema_ready
    with _schema_lock:
        _schema_ready = False


def close() -> None:
    """بستن اتصال override (فقط تست/ابزار)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
