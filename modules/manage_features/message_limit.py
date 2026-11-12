# -*- coding: utf-8 -*-
"""قابلیت «محدودیت پیام» — شمارش دقیق، میوت خودکار و محافظت از باز شدن ناخواسته.

معماری و اصول طراحی:

* **ذخیره‌سازی پایدار (SQLite اتمیک + ایندکس حافظه‌ای O(1))**:
  جدول ``message_limits`` در ``modules.runtime_db`` (با ``WAL`` و تراکنش‌های
  ``BEGIN IMMEDIATE``) منبعِ حقیقتِ پایدار است و دیکشنریِ همگام‌شده با
  ``threading.RLock`` مسیرِ گرمِ بررسی پیام‌های عادی را بدون هیچ سربار دیسک یا
  RPC در زمان ثابت نگه می‌دارد.
* **صف مستقل (PerGroupQueue)**:
  نمونهٔ اختصاصی ``QUEUE = PerGroupQueue("message_limit", ...)`` با کلیدِ
  ``group:user`` همهٔ عملیاتِ ثبت، میوت خودکار و لغوِ هر کاربر را سریالی
  می‌کند بدون آن‌که با صف‌های میوت، ضداسپم، حذف پیام یا سایر قابلیت‌ها تداخل
  داشته باشد.
* **استفاده از سیستم میوت موجود**:
  هیچ RPC موازی برای میوت ساخته نمی‌شود؛ محدودسازی و رفع محدودیت مستقیماً از
  ``AdminActions.mute_user`` و ``AdminActions.unmute_user`` و
  ``PermissionCircuitBreaker`` موجود استفاده می‌کند.
* **معافیت قطعی مالکین در هر سه مرحله**:
  مالک اصلی ثبت‌شدهٔ ربات، مالک ثبت‌شدهٔ گروه، سازندهٔ بومی گروه در سروش‌پلاس
  (``ChannelParticipantCreator`` / ``ChatParticipantCreator``) و خودِ اکانت
  ربات در هر سه مرحلهٔ ثبت محدودیت، شمارش پیام و اجرای میوت خودکار معاف‌اند.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from modules import admin_tools, banned_storage, manual_mute_storage, runtime_db
from modules.admin_actions import AdminActions
from modules.user_display import format_user

from . import access, peers, storage, texts
from .queue import PerGroupQueue, QueueFull, reset_queue
from .rpc import call, flood_seconds

try:  # pragma: no cover
    from splusthon.tl import functions, types
except ImportError:  # pragma: no cover
    functions = None
    types = None


# ---------------------------------------------------------------------------
#  ثابت‌ها و خطاهای دامنه
# ---------------------------------------------------------------------------
STATUS_COUNTING = "counting"
STATUS_PENDING_MUTE = "pending_mute"
STATUS_MUTED = "muted"
STATUS_MUTE_FAILED = "mute_failed"

ROLE_CACHE_TTL = 180.0
READ_RPC_TIMEOUT = 12.0
MUTE_WAIT_SECONDS = 4.0
MAX_SEEN_MESSAGE_IDS = 32
MAX_FLOOD_WAIT_SLEEP = 30.0

QUEUE = PerGroupQueue(
    "message_limit",
    max_pending_per_key=16,
    max_pending_total=512,
    max_keys=256,
    task_timeout=45.0,
)


class MessageLimitError(Exception):
    """پایهٔ خطاهای دامنهٔ محدودیت پیام."""


class InvalidLimitError(MessageLimitError):
    """عدد سقف مجاز معتبر نیست (باید عدد صحیح مثبت بزرگ‌تر از صفر باشد)."""


class ProtectedTargetError(MessageLimitError):
    """کاربر هدف مالک ربات، مالک گروه یا خود ربات است و قابل محدودسازی نیست."""

    def __init__(self, reason: str = "protected"):
        super().__init__(reason)
        self.reason = reason


class UnrestrictableTargetError(MessageLimitError):
    """ادمین/کاربر هدف از نظر دسترسی API قابل میوت شدن توسط ربات نیست."""

    def __init__(self, reason: str = "unrestrictable"):
        super().__init__(reason)
        self.reason = reason


class NoActiveLimitError(MessageLimitError):
    """برای این کاربر در این گروه محدودیت فعالی ثبت نشده است."""


class UnmuteFailedError(MessageLimitError):
    """رفع سکوت از طریق API ناموفق بود؛ وضعیت برای تلاش مجدد حفظ شد."""


# ---------------------------------------------------------------------------
#  لایهٔ پایدار SQLite + ایندکس حافظه‌ای همگام
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_limits (
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    allowed_count INTEGER NOT NULL CHECK(allowed_count > 0),
    sent_count INTEGER NOT NULL DEFAULT 0 CHECK(sent_count >= 0),
    status TEXT NOT NULL DEFAULT 'counting',
    muted_by_limit INTEGER NOT NULL DEFAULT 0,
    epoch INTEGER NOT NULL DEFAULT 1,
    username TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    set_by TEXT,
    set_by_username TEXT NOT NULL DEFAULT '',
    pre_banned_rights_json TEXT,
    seen_msg_ids_json TEXT NOT NULL DEFAULT '[]',
    trigger_message_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    muted_at REAL,
    last_error TEXT,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_message_limits_group_status
    ON message_limits(group_id, status);
"""

_STATE_LOCK = threading.RLock()
_READY_CONN_ID: Optional[int] = None
# {group_key: {user_key: dict_record}}
_ACTIVE_INDEX: Dict[str, Dict[str, dict]] = {}

# کش نقش‌های مدیریتی/سازندهٔ گروه برای پرهیز از RPC تکراری
# {group_key: (monotonic_ts, info_dict)}
_ROLE_CACHE: Dict[str, Tuple[float, dict]] = {}
_ROLE_LOCKS: Dict[str, asyncio.Lock] = {}

_RIGHTS_BOOL_FIELDS = (
    "view_messages",
    "send_messages",
    "send_media",
    "send_stickers",
    "send_gifs",
    "send_games",
    "send_inline",
    "embed_links",
    "send_polls",
    "change_info",
    "invite_users",
    "pin_messages",
)


def _row_to_record(row) -> dict:
    try:
        pre_rights = (
            json.loads(row["pre_banned_rights_json"])
            if row["pre_banned_rights_json"]
            else None
        )
    except Exception:  # noqa: BLE001
        pre_rights = None
    try:
        seen_ids = (
            json.loads(row["seen_msg_ids_json"])
            if row["seen_msg_ids_json"]
            else []
        )
        if not isinstance(seen_ids, list):
            seen_ids = []
    except Exception:  # noqa: BLE001
        seen_ids = []
    return {
        "group_id": str(row["group_id"]),
        "user_id": str(row["user_id"]),
        "allowed_count": int(row["allowed_count"]),
        "sent_count": int(row["sent_count"]),
        "status": str(row["status"]),
        "muted_by_limit": bool(int(row["muted_by_limit"] or 0)),
        "epoch": int(row["epoch"] or 1),
        "username": str(row["username"] or ""),
        "display_name": str(row["display_name"] or ""),
        "set_by": str(row["set_by"]) if row["set_by"] is not None else None,
        "set_by_username": str(row["set_by_username"] or ""),
        "pre_banned_rights": pre_rights if isinstance(pre_rights, dict) else None,
        "seen_msg_ids": [int(x) for x in seen_ids if isinstance(x, int) or str(x).lstrip("-").isdigit()],
        "trigger_message_id": (
            int(row["trigger_message_id"])
            if row["trigger_message_id"] is not None
            else None
        ),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "muted_at": float(row["muted_at"]) if row["muted_at"] is not None else None,
        "last_error": str(row["last_error"]) if row["last_error"] else None,
    }


def _ensure_loaded(force: bool = False) -> None:
    """ایجاد جدول در صورت نیاز و همگام‌سازی ایندکس حافظه‌ای با اتصال فعال DB."""
    global _READY_CONN_ID
    conn = runtime_db.connection()
    conn_id = id(conn)
    if not force and _READY_CONN_ID == conn_id:
        return
    with _STATE_LOCK:
        conn = runtime_db.connection()
        conn_id = id(conn)
        if not force and _READY_CONN_ID == conn_id:
            return
        with runtime_db._LOCK:
            conn.executescript(_SCHEMA)
            conn.commit()
            rows = conn.execute("SELECT * FROM message_limits").fetchall()
        fresh: Dict[str, Dict[str, dict]] = {}
        for row in rows:
            rec = _row_to_record(row)
            fresh.setdefault(rec["group_id"], {})[rec["user_id"]] = rec
        _ACTIVE_INDEX.clear()
        _ACTIVE_INDEX.update(fresh)
        _READY_CONN_ID = conn_id


def _index_put(rec: dict) -> None:
    _ACTIVE_INDEX.setdefault(rec["group_id"], {})[rec["user_id"]] = dict(rec)


def _index_pop(gkey: str, ukey: str) -> Optional[dict]:
    group = _ACTIVE_INDEX.get(gkey)
    if not group:
        return None
    popped = group.pop(ukey, None)
    if not group:
        _ACTIVE_INDEX.pop(gkey, None)
    return popped


def _queue_key(chat_id, user_id) -> str:
    return f"{storage.group_key(chat_id)}:{peers.user_key(user_id)}"


_USER_ENTITY_CACHE: Dict[int, Any] = {}


def remember_user_entity(bot, entity) -> None:
    if entity is None:
        return
    uid = peers.user_identifier(entity)
    if uid is None:
        return
    _USER_ENTITY_CACHE[int(uid)] = entity
    cache = getattr(bot, "reply_input_peer_cache", None) if bot is not None else None
    if isinstance(cache, dict):
        try:
            inp = peers.as_input_user(entity)
            if inp is not None:
                cache[int(uid)] = inp
        except Exception:  # noqa: BLE001
            pass


def cached_user_entity(bot, user_id):
    uid = peers.user_identifier(user_id)
    if uid is None:
        return None
    ent = _USER_ENTITY_CACHE.get(int(uid))
    if ent is not None:
        return ent
    cache = getattr(bot, "reply_input_peer_cache", None) if bot is not None else None
    if isinstance(cache, dict):
        return cache.get(int(uid))
    return None


def remember_group_entity(bot, chat_id, chat) -> None:
    if bot is None or chat_id is None or chat is None:
        return
    try:
        peers.remember_channel_peer(bot, chat_id, chat)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  پرس‌وجوهای عمومی وضعیت (O(1) در مسیر گرم)
# ---------------------------------------------------------------------------
def get_record(chat_id, user_id) -> Optional[dict]:
    """کپی رکورد فعال کاربر در گروه یا ``None``."""
    if chat_id is None or user_id is None:
        return None
    _ensure_loaded()
    gkey = storage.group_key(chat_id)
    ukey = peers.user_key(user_id)
    if not ukey:
        return None
    with _STATE_LOCK:
        rec = (_ACTIVE_INDEX.get(gkey) or {}).get(ukey)
        return dict(rec) if rec is not None else None


def list_group_limits(chat_id) -> List[dict]:
    """همهٔ رکوردهای محدودیت پیام در یک گروه."""
    if chat_id is None:
        return []
    _ensure_loaded()
    gkey = storage.group_key(chat_id)
    with _STATE_LOCK:
        group = _ACTIVE_INDEX.get(gkey) or {}
        return [dict(rec) for rec in group.values()]


def has_active_counter(chat_id, user_id) -> bool:
    """آیا شمارش پیام برای این کاربر در این گروه فعال است؟"""
    if chat_id is None or user_id is None:
        return False
    _ensure_loaded()
    gkey = storage.group_key(chat_id)
    ukey = peers.user_key(user_id)
    if not ukey:
        return False
    with _STATE_LOCK:
        rec = (_ACTIVE_INDEX.get(gkey) or {}).get(ukey)
        return bool(rec and rec.get("status") == STATUS_COUNTING)


def is_muted_by_limit(chat_id, user_id) -> bool:
    """آیا کاربر هم‌اکنون به دلیل رسیدن به سقف «محدودیت پیام» میوت شده است؟"""
    if chat_id is None or user_id is None:
        return False
    _ensure_loaded()
    gkey = storage.group_key(chat_id)
    ukey = peers.user_key(user_id)
    if not ukey:
        return False
    with _STATE_LOCK:
        rec = (_ACTIVE_INDEX.get(gkey) or {}).get(ukey)
        return bool(
            rec
            and rec.get("muted_by_limit")
            and rec.get("status") == STATUS_MUTED
        )


def _delete_record(chat_id, user_id) -> Optional[dict]:
    """حذف اتمیک رکورد و شمارنده از SQLite و ایندکس حافظه‌ای."""
    _ensure_loaded()
    gkey = storage.group_key(chat_id)
    ukey = peers.user_key(user_id)
    if not ukey:
        return None
    with _STATE_LOCK:
        with runtime_db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM message_limits WHERE group_id = ? AND user_id = ?",
                (gkey, ukey),
            ).fetchone()
            conn.execute(
                "DELETE FROM message_limits WHERE group_id = ? AND user_id = ?",
                (gkey, ukey),
            )
        popped = _index_pop(gkey, ukey)
        if row is not None:
            return _row_to_record(row)
        return popped


# ---------------------------------------------------------------------------
#  معافیت مالک اصلی ربات، مالک گروه و خود اکانت ربات
# ---------------------------------------------------------------------------
def _bot_id(bot) -> Optional[int]:
    if bot is None:
        return None
    for candidate in (
        getattr(bot, "bot_account_id", None),
        getattr(getattr(bot, "client", None), "bot_id", None),
        getattr(getattr(bot, "me", None), "id", None),
    ):
        identifier = peers.user_identifier(candidate)
        if identifier is not None:
            return identifier
    return None


def is_creator_participant(participant) -> bool:
    if participant is None:
        return False
    cls_name = type(participant).__name__
    if "Creator" in cls_name:
        return True
    return bool(getattr(participant, "is_creator", False))


def is_admin_participant(participant) -> bool:
    if participant is None:
        return False
    if is_creator_participant(participant):
        return False
    cls_name = type(participant).__name__
    return "Admin" in cls_name or bool(getattr(participant, "admin_rights", None))


def _cached_role_snapshot(chat_id) -> Optional[dict]:
    entry = _ROLE_CACHE.get(storage.group_key(chat_id))
    if entry is None:
        return None
    stamp, info = entry
    if storage.monotonic() - stamp > ROLE_CACHE_TTL:
        return None
    return info


def invalidate_role_cache(chat_id=None) -> None:
    if chat_id is None:
        _ROLE_CACHE.clear()
    else:
        _ROLE_CACHE.pop(storage.group_key(chat_id), None)


def check_exempt_sync(chat_id, user_id, *, bot=None) -> Optional[str]:
    """بررسی همگام (بدون RPC) برای معافیت قطعی مالک ربات / مالک گروه / خود ربات.

    خروجی: رشتهٔ علت در صورت معاف بودن، یا ``None`` اگر معاف نبود.
    """
    identifier = peers.user_identifier(user_id)
    if identifier is None:
        return "unknown_identity"
    bot_identifier = _bot_id(bot)
    if bot_identifier is not None and int(identifier) == int(bot_identifier):
        return "bot_self"
    try:
        if admin_tools.is_global_owner(identifier):
            return "global_owner"
    except Exception:  # noqa: BLE001
        pass
    try:
        if access.is_group_owner(chat_id, identifier):
            return "group_owner"
    except Exception:  # noqa: BLE001
        pass
    snapshot = _cached_role_snapshot(chat_id)
    if snapshot is not None:
        creator_id = snapshot.get("creator_id")
        if creator_id is not None and int(creator_id) == int(identifier):
            return "group_creator"
    return None


def _serialize_banned_rights(rights_obj) -> Optional[dict]:
    """ذخیرهٔ پرچم‌های قبلی ``ChatBannedRights`` (فقط وقتی محدودیت جزئی غیرمیوت وجود دارد)."""
    if rights_obj is None:
        return None
    flags = {}
    any_true = False
    for field in _RIGHTS_BOOL_FIELDS:
        val = bool(getattr(rights_obj, field, False))
        flags[field] = val
        if val:
            any_true = True
    if not any_true:
        return None
    until_date = getattr(rights_obj, "until_date", None)
    if hasattr(until_date, "timestamp"):
        try:
            until_ts = float(until_date.timestamp())
        except Exception:  # noqa: BLE001
            until_ts = None
    elif isinstance(until_date, (int, float)) and until_date > 0:
        until_ts = float(until_date)
    else:
        until_ts = None
    return {"flags": flags, "until_ts": until_ts}


def _build_rights_from_snapshot(snapshot: dict):
    if types is None or not hasattr(types, "ChatBannedRights"):
        return None
    flags = dict((snapshot or {}).get("flags") or {})
    until_ts = (snapshot or {}).get("until_ts")
    until_date = None
    if isinstance(until_ts, (int, float)) and until_ts > time.time():
        import datetime as _dt
        until_date = _dt.datetime.fromtimestamp(until_ts, tz=_dt.timezone.utc)
    kwargs = {field: bool(flags.get(field, False)) for field in _RIGHTS_BOOL_FIELDS}
    kwargs["until_date"] = until_date
    return types.ChatBannedRights(**kwargs)


async def _fetch_role_snapshot(bot, chat_id, *, chat=None, logger=None, force: bool = False) -> dict:
    """خواندن امن نقش‌های سازنده و ادمین‌های بومی گروه از API رسمی (با کش TTL).

    اگر کلاینت تست متد ``get_participants`` نداشته باشد یا سروش‌پلاس برای متدی
    ``NOT_SUPPORTED`` برگرداند، بدون خطا به مسیرهای پشتیبان می‌رود و هرگز باعث
    کرش نمی‌شود.
    """
    if not force:
        cached = _cached_role_snapshot(chat_id)
        if cached is not None:
            return cached

    gkey = storage.group_key(chat_id)
    lock = _ROLE_LOCKS.setdefault(gkey, asyncio.Lock())
    async with lock:
        if not force:
            cached = _cached_role_snapshot(chat_id)
            if cached is not None:
                return cached

        info: dict = {
            "creator_id": None,
            "admins": {},  # {int_user_id: {"can_edit": Optional[bool], "promoted_by": Optional[int], "admin_rights": dict}}
            "inspected": False,
        }
        client = getattr(bot, "client", None)
        if client is None:
            return info

        peer = chat if chat is not None else peers.group_peer(bot, chat_id)

        # مسیر ۱: get_participants با فیلتر ادمین‌ها (سروش‌پلاس از این مسیر پشتیبانی می‌کند)
        if (
            hasattr(client, "get_participants")
            and types is not None
            and hasattr(types, "ChannelParticipantsAdmins")
        ):
            try:
                rows = await call(
                    lambda: client.get_participants(
                        peer,
                        filter=types.ChannelParticipantsAdmins(),
                        limit=200,
                    ),
                    logger=logger,
                    timeout=READ_RPC_TIMEOUT,
                    max_retry=1,
                    what=f"MSG_LIMIT ADMINS chat={chat_id}",
                )
                info["inspected"] = True
                for row in list(rows or []):
                    participant = getattr(row, "participant", None)
                    uid = peers.user_identifier(row)
                    if uid is None and participant is not None:
                        uid = peers.user_identifier(getattr(participant, "user_id", None))
                    if uid is None:
                        continue
                    if is_creator_participant(participant) or is_creator_participant(row):
                        info["creator_id"] = int(uid)
                        continue
                    p_obj = participant if participant is not None else row
                    if is_admin_participant(p_obj):
                        rights_obj = getattr(p_obj, "admin_rights", None)
                        info["admins"][int(uid)] = {
                            "can_edit": getattr(p_obj, "can_edit", None),
                            "promoted_by": peers.user_identifier(
                                getattr(p_obj, "promoted_by", None)
                            ),
                            "ban_users": (
                                bool(getattr(rights_obj, "ban_users", False))
                                if rights_obj is not None
                                else None
                            ),
                        }
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

        # مسیر ۲ (پشتیبان برای گپ‌های معمولی Chat): GetFullChatRequest
        if (
            not info["inspected"]
            and functions is not None
            and hasattr(getattr(functions, "messages", None), "GetFullChatRequest")
            and isinstance(chat_id, int)
            and -1_000_000_000_000 < chat_id < 0
        ):
            try:
                raw_chat_id = abs(int(chat_id))
                full = await call(
                    lambda: client(functions.messages.GetFullChatRequest(chat_id=raw_chat_id)),
                    logger=logger,
                    timeout=READ_RPC_TIMEOUT,
                    max_retry=1,
                    what=f"MSG_LIMIT FULLCHAT chat={chat_id}",
                )
                participants = getattr(
                    getattr(full, "full_chat", None), "participants", None
                )
                part_rows = getattr(participants, "participants", None) or []
                info["inspected"] = True
                for p_obj in part_rows:
                    uid = peers.user_identifier(getattr(p_obj, "user_id", None))
                    if uid is None:
                        continue
                    if is_creator_participant(p_obj):
                        info["creator_id"] = int(uid)
                    elif is_admin_participant(p_obj):
                        info["admins"][int(uid)] = {
                            "can_edit": None,
                            "promoted_by": peers.user_identifier(
                                getattr(p_obj, "inviter_id", None)
                            ),
                            "ban_users": True,
                        }
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass

        if info["inspected"]:
            _ROLE_CACHE[gkey] = (storage.monotonic(), info)
        return info


async def _fetch_single_participant(bot, chat_id, target_id, *, entity=None, chat=None, logger=None):
    """تلاش اختیاری برای خواندن وضعیت دقیق یک عضو (اگر کلاینت از GetParticipant پشتیبانی کند)."""
    client = getattr(bot, "client", None)
    if (
        client is None
        or functions is None
        or not hasattr(getattr(functions, "channels", None), "GetParticipantRequest")
    ):
        return None
    # اگر کلاینت فقط یک callable سفارشی تست است که فقط EditBannedRequest را می‌پذیرد،
    # تنها زمانی GetParticipantRequest را صدا بزن که کلاینت صریحاً پشتیبانی کند یا متد استاندارد باشد.
    if not hasattr(client, "get_participants") and not getattr(
        client, "supports_get_participant", False
    ):
        return None
    peer = chat if chat is not None else peers.group_peer(bot, chat_id)
    target_peer = entity if entity is not None else cached_user_entity(bot, target_id)
    if target_peer is None:
        target_peer = int(target_id)
    try:
        res = await call(
            lambda: client(
                functions.channels.GetParticipantRequest(
                    channel=peer,
                    participant=target_peer,
                )
            ),
            logger=logger,
            timeout=READ_RPC_TIMEOUT,
            max_retry=1,
            what=f"MSG_LIMIT GET_PARTICIPANT chat={chat_id} user={target_id}",
        )
        return getattr(res, "participant", res)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return None


async def check_exempt_async(
    bot,
    chat_id,
    user_id,
    *,
    entity=None,
    chat=None,
    logger=None,
    force_refresh: bool = False,
) -> Optional[str]:
    """بررسی کامل معافیت (همگام + استعلام سازندهٔ بومی گروه در سروش‌پلاس)."""
    sync_reason = check_exempt_sync(chat_id, user_id, bot=bot)
    if sync_reason is not None:
        return sync_reason
    identifier = peers.user_identifier(user_id)
    if identifier is None:
        return "unknown_identity"
    snapshot = await _fetch_role_snapshot(
        bot, chat_id, chat=chat, logger=logger, force=force_refresh
    )
    creator_id = snapshot.get("creator_id")
    if creator_id is not None and int(creator_id) == int(identifier):
        return "group_creator"
    participant = await _fetch_single_participant(
        bot, chat_id, identifier, entity=entity, chat=chat, logger=logger
    )
    if is_creator_participant(participant):
        if snapshot.get("inspected"):
            snapshot["creator_id"] = int(identifier)
        return "group_creator"
    return None


# ---------------------------------------------------------------------------
#  اتصال به سیستم میوت موجود (AdminActions) و بررسی امکان محدودسازی
# ---------------------------------------------------------------------------
def _resolve_admin_actions(bot) -> AdminActions:
    existing = getattr(bot, "admin_actions", None)
    if existing is not None:
        return existing
    actions = AdminActions(
        getattr(bot, "client", None),
        getattr(bot, "logger", None),
        getattr(bot, "config_manager", {}),
        peer_cache=getattr(bot, "reply_input_peer_cache", None),
        bot_account_id=_bot_id(bot),
    )
    try:
        setattr(bot, "admin_actions", actions)
    except Exception:  # noqa: BLE001
        pass
    return actions


async def _assess_target_restrictability(
    bot,
    chat_id,
    target_id: int,
    *,
    entity=None,
    chat=None,
    logger=None,
) -> Tuple[bool, str, Optional[dict]]:
    """بررسی می‌کند آیا هدف (کاربر عادی، ادمین گروه یا ادمین ثبت‌شده) واقعاً قابل میوت است یا نه.

    خروجی: ``(can_restrict, reason, pre_banned_rights_snapshot)``
    """
    exempt_reason = await check_exempt_async(
        bot, chat_id, target_id, entity=entity, chat=chat, logger=logger
    )
    if exempt_reason is not None:
        raise ProtectedTargetError(exempt_reason)

    admin_actions = _resolve_admin_actions(bot)
    breaker = getattr(admin_actions, "circuit_breaker", None)
    if breaker is not None and not breaker.can_execute(chat_id, "mute"):
        return False, "circuit_breaker_open", None

    snapshot = await _fetch_role_snapshot(bot, chat_id, chat=chat, logger=logger)
    bot_uid = _bot_id(bot)
    creator_id = snapshot.get("creator_id")
    admins_map: dict = snapshot.get("admins") or {}

    bot_is_creator = bool(
        (bot_uid is not None and creator_id is not None and int(bot_uid) == int(creator_id))
        or getattr(chat, "creator", False)
    )

    # بررسی حق ban_users ربات در صورتی که از شیء چت یا لیست ادمین‌ها صریحاً مشخص باشد
    if not bot_is_creator:
        chat_admin_rights = getattr(chat, "admin_rights", None) if chat is not None else None
        if chat_admin_rights is not None and hasattr(chat_admin_rights, "ban_users"):
            if not bool(getattr(chat_admin_rights, "ban_users", False)):
                return False, "bot_lacks_ban_users", None
        elif (
            chat is not None
            and hasattr(chat, "creator")
            and hasattr(chat, "admin_rights")
            and getattr(chat, "creator", None) is False
            and getattr(chat, "admin_rights", None) is None
        ):
            return False, "bot_not_group_admin", None

        if snapshot.get("inspected") and bot_uid is not None:
            bot_admin_info = admins_map.get(int(bot_uid))
            if bot_admin_info is not None and bot_admin_info.get("ban_users") is False:
                return False, "bot_lacks_ban_users", None

    # بررسی وضعیت هدف در صورتی که ادمین بومی گروه باشد
    target_admin_info = admins_map.get(int(target_id))
    if target_admin_info is not None and not bot_is_creator:
        can_edit = target_admin_info.get("can_edit")
        promoted_by = target_admin_info.get("promoted_by")
        if can_edit is False:
            return False, "admin_not_editable", None
        if (
            can_edit is None
            and promoted_by is not None
            and bot_uid is not None
            and int(promoted_by) != int(bot_uid)
        ):
            return False, "admin_promoted_by_other", None

    pre_rights_snapshot = None
    participant = await _fetch_single_participant(
        bot, chat_id, target_id, entity=entity, chat=chat, logger=logger
    )
    if participant is not None:
        if is_creator_participant(participant):
            raise ProtectedTargetError("group_creator")
        if is_admin_participant(participant) and not bot_is_creator:
            p_can_edit = getattr(participant, "can_edit", None)
            p_promoted_by = peers.user_identifier(
                getattr(participant, "promoted_by", None)
            )
            if p_can_edit is False:
                return False, "admin_not_editable", None
            if (
                p_can_edit is None
                and p_promoted_by is not None
                and bot_uid is not None
                and int(p_promoted_by) != int(bot_uid)
            ):
                return False, "admin_promoted_by_other", None
        banned_rights = getattr(participant, "banned_rights", None)
        if banned_rights is not None:
            pre_rights_snapshot = _serialize_banned_rights(banned_rights)

    return True, "ok", pre_rights_snapshot


async def _mute_via_existing_system(
    bot,
    chat_id,
    target_id: int,
    *,
    user=None,
    chat=None,
    logger=None,
) -> bool:
    """فراخوانی ``AdminActions.mute_user`` موجود با پوشش کوتاه FloodWait."""
    admin_actions = _resolve_admin_actions(bot)
    resolved_chat = chat if chat is not None else peers.group_peer(bot, chat_id)
    resolved_user = user if user is not None else cached_user_entity(bot, target_id)
    for attempt in range(2):
        try:
            ok = await admin_actions.mute_user(
                chat_id,
                int(target_id),
                duration_seconds=None,
                user=resolved_user,
                chat=resolved_chat,
            )
            return bool(ok)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            flood = flood_seconds(exc)
            if flood and attempt == 0 and flood <= MAX_FLOOD_WAIT_SLEEP:
                await asyncio.sleep(float(flood) + 0.5)
                continue
            if logger and hasattr(logger, "log_error"):
                logger.log_error(
                    f"[MSG_LIMIT] خطا در اجرای mute_user chat={chat_id} user={target_id}: {exc}"
                )
            return False
    return False


def _has_independent_restriction(bot, chat_id, user_id: int, username: str = "") -> Optional[str]:
    """آیا کاربر علاوه بر محدودیت پیام، میوت دستی، بن دائمی یا جریمهٔ خودکار ربات دارد؟"""
    try:
        if manual_mute_storage.is_manual_muted(chat_id, user_id):
            return "manual_mute"
    except Exception:  # noqa: BLE001
        pass
    try:
        if banned_storage.is_banned(chat_id, user_id, username or None):
            return "permanent_ban"
    except Exception:  # noqa: BLE001
        pass
    if bot is not None:
        uid_int = int(user_id)
        gkey = storage.group_key(chat_id)
        candidate_keys = (
            f"{chat_id}:{uid_int}",
            f"{gkey}:{uid_int}",
            (chat_id, uid_int),
        )
        tracker = getattr(bot, "tracker", None)
        if tracker is not None:
            try:
                muted_map = getattr(tracker, "muted_users", None) or {}
                for k in candidate_keys:
                    until_ts = muted_map.get(k)
                    if until_ts is not None:
                        if until_ts is True or (
                            isinstance(until_ts, (int, float)) and float(until_ts) > time.time()
                        ):
                            return "bot_auto_mute"
            except Exception:  # noqa: BLE001
                pass
            try:
                banned_col = getattr(tracker, "banned_users", None) or ()
                for k in candidate_keys:
                    if k in banned_col:
                        return "bot_auto_ban"
            except Exception:  # noqa: BLE001
                pass
        try:
            punished = getattr(bot, "punished_users", None)
            if isinstance(punished, dict):
                for k in candidate_keys:
                    entry = punished.get(k)
                    if isinstance(entry, dict):
                        until = entry.get("until")
                        if until is None or float(until) > time.time():
                            return f"bot_punishment:{entry.get('action', 'mute')}"
                    elif entry:
                        return "bot_punishment"
            elif isinstance(punished, (set, list, tuple, frozenset)):
                for k in candidate_keys:
                    if k in punished:
                        return "bot_punishment"
        except Exception:  # noqa: BLE001
            pass
    return None


async def _restore_or_unmute_via_existing_system(
    bot,
    chat_id,
    target_id: int,
    *,
    pre_banned_rights: Optional[dict] = None,
    user=None,
    chat=None,
    logger=None,
) -> bool:
    """بازگردانی وضعیت محدودیت قبلی یا اجرای ``AdminActions.unmute_user`` موجود."""
    admin_actions = _resolve_admin_actions(bot)
    resolved_chat = chat if chat is not None else peers.group_peer(bot, chat_id)
    resolved_user = user if user is not None else cached_user_entity(bot, target_id)

    # اگر کاربر پیش از محدودیت پیام، محدودیت جزئیِ غیرمیوت داشت (مثلاً فقط قفل لینک/گیف)،
    # همان حقوق قبلی را بازمی‌گردانیم تا محدودیت قبلی از بین نرود؛ اگر از قبل send_messages
    # بسته بود، اصلاً نباید آن میوتِ مستقل باز شود!
    if isinstance(pre_banned_rights, dict):
        flags = pre_banned_rights.get("flags") or {}
        until_ts = pre_banned_rights.get("until_ts")
        still_valid = until_ts is None or float(until_ts) > time.time()
        if still_valid and flags.get("send_messages"):
            # کاربر پیش از اعمال محدودیت پیام نیز send_messages بسته داشته؛ بازش نکن.
            return True
        if (
            still_valid
            and any(bool(v) for v in flags.values())
            and functions is not None
            and hasattr(getattr(functions, "channels", None), "EditBannedRequest")
        ):
            rights_tl = _build_rights_from_snapshot(pre_banned_rights)
            if rights_tl is not None:
                try:
                    channel_entity = await admin_actions._input_entity(
                        resolved_chat if resolved_chat is not None else chat_id
                    )
                    user_entity = await admin_actions._input_entity(
                        resolved_user if resolved_user is not None else int(target_id)
                    )
                    if channel_entity is not None and user_entity is not None:
                        await bot.client(
                            functions.channels.EditBannedRequest(
                                channel=channel_entity,
                                participant=user_entity,
                                banned_rights=rights_tl,
                            )
                        )
                        return True
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    pass

    for attempt in range(2):
        try:
            ok = await admin_actions.unmute_user(
                chat_id,
                int(target_id),
                user=resolved_user,
                chat=resolved_chat,
            )
            return bool(ok)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            flood = flood_seconds(exc)
            if flood and attempt == 0 and flood <= MAX_FLOOD_WAIT_SLEEP:
                await asyncio.sleep(float(flood) + 0.5)
                continue
            if logger and hasattr(logger, "log_error"):
                logger.log_error(
                    f"[MSG_LIMIT] خطا در اجرای unmute_user chat={chat_id} user={target_id}: {exc}"
                )
            return False
    return False


# ---------------------------------------------------------------------------
#  عملیات اصلی: ثبت محدودیت، شمارش پیام، میوت خودکار و لغو محدودیت
# ---------------------------------------------------------------------------
def _user_label(entity, fallback_username: str = "", fallback_name: str = "") -> str:
    if entity is not None:
        label = format_user(entity, fallback="")
        if label:
            return label
    uname = str(fallback_username or "").strip().lstrip("@")
    if uname:
        return f"@{uname}"
    dname = str(fallback_name or "").strip()
    if dname:
        return dname
    return "کاربر"


async def set_limit(
    bot,
    chat_id,
    target_id,
    allowed_count: int,
    *,
    actor_id=None,
    actor_username: str = "",
    entity=None,
    chat=None,
    logger=None,
) -> dict:
    """ثبت یا به‌روزرسانی محدودیت پیام برای کاربر هدف در گروه (سریالی‌شده در صف مستقل)."""
    try:
        limit_val = int(allowed_count)
    except (TypeError, ValueError) as exc:
        raise InvalidLimitError("non_integer") from exc
    if limit_val <= 0:
        raise InvalidLimitError("non_positive")

    uid = peers.user_identifier(target_id)
    if uid is None:
        raise MessageLimitError("invalid_target")

    # مرحله ۱ معافیت مالکین + بررسی واقعی امکان میوت کاربر/ادمین هدف
    can_restrict, reason, fresh_pre_rights = await _assess_target_restrictability(
        bot, chat_id, uid, entity=entity, chat=chat, logger=logger
    )
    if not can_restrict:
        raise UnrestrictableTargetError(reason)

    if entity is not None:
        remember_user_entity(bot, entity)
    if chat is not None:
        remember_group_entity(bot, chat_id, chat)

    gkey = storage.group_key(chat_id)
    ukey = peers.user_key(uid)
    qkey = _queue_key(chat_id, uid)

    async def _job():
        # بررسی مجدد معافیت داخل صف برای جلوگیری از هرگونه تغییر همزمان مالکیت
        exempt_reason = await check_exempt_async(
            bot, chat_id, uid, entity=entity, chat=chat, logger=logger
        )
        if exempt_reason is not None:
            raise ProtectedTargetError(exempt_reason)

        _ensure_loaded()
        existing = get_record(chat_id, uid)
        was_muted_by_limit = bool(existing and existing.get("muted_by_limit"))

        # اگر کاربر قبلاً با همین سیستم محدودیت پیام میوت شده بود و مدیر محدودیت جدیدی
        # برایش تعیین کرده، ابتدا قفل میوتِ محدودیت قبلی را باز می‌کنیم تا بتواند به اندازهٔ
        # سقف جدید پیام بفرستد (مگر آن‌که محدودیت مستقل دیگری مثل میوت دستی/بن داشته باشد).
        if was_muted_by_limit:
            other_lock = _has_independent_restriction(
                bot, chat_id, uid, existing.get("username", "")
            )
            if other_lock is None:
                await _restore_or_unmute_via_existing_system(
                    bot,
                    chat_id,
                    uid,
                    pre_banned_rights=existing.get("pre_banned_rights"),
                    user=entity,
                    chat=chat,
                    logger=logger,
                )

        now_ts = time.time()
        raw_username = (
            str(getattr(entity, "username", "") or "").strip().lstrip("@")
            if entity is not None
            else ""
        ) or (existing.get("username", "") if existing else "")
        display_label = _user_label(
            entity,
            fallback_username=raw_username,
            fallback_name=existing.get("display_name", "") if existing else "",
        )
        preserved_pre_rights = (
            existing.get("pre_banned_rights")
            if (existing and was_muted_by_limit)
            else fresh_pre_rights
        )
        next_epoch = (int(existing["epoch"]) + 1) if existing else 1
        actor_key = peers.user_key(actor_id) if actor_id is not None else None
        actor_uname = str(actor_username or "").strip().lstrip("@")

        with _STATE_LOCK:
            with runtime_db.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO message_limits (
                        group_id, user_id, allowed_count, sent_count, status,
                        muted_by_limit, epoch, username, display_name,
                        set_by, set_by_username, pre_banned_rights_json,
                        seen_msg_ids_json, trigger_message_id,
                        created_at, updated_at, muted_at, last_error
                    ) VALUES (?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, ?, '[]', NULL, ?, ?, NULL, NULL)
                    ON CONFLICT(group_id, user_id) DO UPDATE SET
                        allowed_count = excluded.allowed_count,
                        sent_count = 0,
                        status = excluded.status,
                        muted_by_limit = 0,
                        epoch = excluded.epoch,
                        username = excluded.username,
                        display_name = excluded.display_name,
                        set_by = excluded.set_by,
                        set_by_username = excluded.set_by_username,
                        pre_banned_rights_json = excluded.pre_banned_rights_json,
                        seen_msg_ids_json = '[]',
                        trigger_message_id = NULL,
                        updated_at = excluded.updated_at,
                        muted_at = NULL,
                        last_error = NULL
                    """,
                    (
                        gkey,
                        ukey,
                        limit_val,
                        STATUS_COUNTING,
                        next_epoch,
                        raw_username,
                        display_label,
                        actor_key,
                        actor_uname,
                        json.dumps(preserved_pre_rights, ensure_ascii=False)
                        if preserved_pre_rights
                        else None,
                        now_ts,
                        now_ts,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM message_limits WHERE group_id = ? AND user_id = ?",
                    (gkey, ukey),
                ).fetchone()
            saved = _row_to_record(row)
            _index_put(saved)

        try:
            admin_tools.log_action(
                chat_id,
                actor_id or 0,
                "set_message_limit",
                int(uid),
                {"allowed_count": limit_val},
            )
        except Exception:  # noqa: BLE001
            pass
        if logger and hasattr(logger, "log_info"):
            logger.log_info(
                f"[MSG_LIMIT] ثبت محدودیت پیام chat={chat_id} user={uid} limit={limit_val} by={actor_id}"
            )
        return saved

    return await QUEUE.run(qkey, _job)


async def _send_limit_reached_notice(
    bot,
    chat_id,
    record: dict,
    *,
    event=None,
    sender=None,
    message_id: Optional[int] = None,
    logger=None,
) -> None:
    label = _user_label(
        sender,
        fallback_username=record.get("username", ""),
        fallback_name=record.get("display_name", ""),
    )
    text, entities = texts.message_limit_reached(
        username=label,
        limit=record["allowed_count"],
    )
    try:
        if event is not None and hasattr(event, "reply"):
            await call(
                lambda: event.reply(text, formatting_entities=entities),
                logger=logger,
                timeout=15.0,
                max_retry=2,
                what=f"MSG_LIMIT REACHED_REPLY chat={chat_id}",
            )
            return
        client = getattr(bot, "client", None)
        if client is not None and hasattr(client, "send_message"):
            peer = peers.group_peer(bot, chat_id)
            reply_to = message_id or record.get("trigger_message_id")
            await call(
                lambda: client.send_message(
                    peer,
                    text,
                    reply_to=reply_to,
                    formatting_entities=entities,
                ),
                logger=logger,
                timeout=15.0,
                max_retry=2,
                what=f"MSG_LIMIT REACHED_SEND chat={chat_id}",
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if logger and hasattr(logger, "log_error"):
            logger.log_error(
                f"[MSG_LIMIT] خطا در ارسال اعلان رسیدن به سقف chat={chat_id}: {exc}"
            )


async def _execute_auto_mute(
    bot,
    chat_id,
    user_id: int,
    expected_epoch: int,
    *,
    event=None,
    sender=None,
    chat=None,
    message_id: Optional[int] = None,
    logger=None,
) -> bool:
    """اجرای تک‌بارهٔ میوت خودکار داخل صف مستقل پس از رسیدن شمارنده به سقف مجاز."""
    _ensure_loaded()
    gkey = storage.group_key(chat_id)
    ukey = peers.user_key(user_id)

    current = get_record(chat_id, user_id)
    if current is None:
        return False
    if int(current.get("epoch", 0)) != int(expected_epoch):
        return False
    if current.get("status") == STATUS_MUTED and current.get("muted_by_limit"):
        return False

    # مرحله ۳ معافیت قطعی مالکین (قبل از اجرای واقعی میوت در API)
    exempt_reason = await check_exempt_async(
        bot, chat_id, user_id, entity=sender, chat=chat, logger=logger
    )
    if exempt_reason is not None:
        _delete_record(chat_id, user_id)
        if logger and hasattr(logger, "log_info"):
            logger.log_info(
                f"[MSG_LIMIT] لغو خودکار به دلیل معافیت مالک ({exempt_reason}) chat={chat_id} user={user_id}"
            )
        return False

    ok = await _mute_via_existing_system(
        bot,
        chat_id,
        int(user_id),
        user=sender,
        chat=chat,
        logger=logger,
    )
    now_ts = time.time()

    with _STATE_LOCK:
        with runtime_db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM message_limits WHERE group_id = ? AND user_id = ?",
                (gkey, ukey),
            ).fetchone()
            if row is None or int(row["epoch"]) != int(expected_epoch):
                return False
            if ok:
                conn.execute(
                    """
                    UPDATE message_limits
                    SET status = ?, muted_by_limit = 1, muted_at = ?, updated_at = ?, last_error = NULL
                    WHERE group_id = ? AND user_id = ? AND epoch = ?
                    """,
                    (STATUS_MUTED, now_ts, now_ts, gkey, ukey, int(expected_epoch)),
                )
            else:
                conn.execute(
                    """
                    UPDATE message_limits
                    SET status = ?, muted_by_limit = 0, updated_at = ?, last_error = ?
                    WHERE group_id = ? AND user_id = ? AND epoch = ?
                    """,
                    (
                        STATUS_MUTE_FAILED,
                        now_ts,
                        "mute_user_failed",
                        gkey,
                        ukey,
                        int(expected_epoch),
                    ),
                )
            updated_row = conn.execute(
                "SELECT * FROM message_limits WHERE group_id = ? AND user_id = ?",
                (gkey, ukey),
            ).fetchone()
        saved = _row_to_record(updated_row)
        _index_put(saved)

    if not ok:
        if logger and hasattr(logger, "log_error"):
            logger.log_error(
                f"[MSG_LIMIT] میوت خودکار توسط API انجام نشد chat={chat_id} user={user_id}"
            )
        return False

    try:
        admin_tools.log_action(
            chat_id,
            _bot_id(bot) or 0,
            "auto_mute_message_limit",
            int(user_id),
            {
                "allowed_count": saved["allowed_count"],
                "sent_count": saved["sent_count"],
            },
        )
    except Exception:  # noqa: BLE001
        pass
    if logger and hasattr(logger, "log_info"):
        logger.log_info(
            f"[MSG_LIMIT] سقف پیام تکمیل شد و کاربر میوت شد chat={chat_id} user={user_id} "
            f"({saved['sent_count']}/{saved['allowed_count']})"
        )

    await _send_limit_reached_notice(
        bot,
        chat_id,
        saved,
        event=event,
        sender=sender,
        message_id=message_id,
        logger=logger,
    )
    return True


def _is_service_event(event) -> bool:
    msg = getattr(event, "message", None)
    if msg is None:
        return True
    if getattr(msg, "action", None) is not None:
        return True
    cls_name = type(msg).__name__
    if "Service" in cls_name:
        return True
    return not hasattr(msg, "message")


async def observe_message(
    bot,
    event,
    chat_id,
    user_id,
    *,
    sender=None,
    chat=None,
    is_edit: bool = False,
    logger=None,
) -> bool:
    """بررسی و شمارش هر پیام جدید ارسالی در گروه برای کاربر دارای محدودیت فعال.

    * پیام‌های سایر اعضا، پیام‌های ویرایش‌شده، پیام‌های خصوصی و پیام‌های سرویس
      نادیده گرفته می‌شوند.
    * انواع پیام (متن، عکس، ویدیو، فایل، ویس، استیکر، گیف، فوروارد و ...) شمرده می‌شوند.
    * به محض رسیدن به ``N``امین پیام مجاز، وضعیت به صورت اتمیک قفل شده و کارِ
      میوت در صف مستقل اجرا می‌شود (بدون امکان اجرای تکراری در همزمانی بالا).
    """
    if is_edit or chat_id is None or user_id is None:
        return False
    if getattr(event, "is_private", False) or _is_service_event(event):
        return False

    uid = peers.user_identifier(user_id)
    if uid is None:
        return False

    # مسیر گرم O(1): اگر کاربر در این گروه محدودیت شمارشی فعالی ندارد، بلافاصله برگرد.
    _ensure_loaded()
    gkey = storage.group_key(chat_id)
    ukey = peers.user_key(uid)
    with _STATE_LOCK:
        quick = (_ACTIVE_INDEX.get(gkey) or {}).get(ukey)
        if quick is None or quick.get("status") != STATUS_COUNTING:
            return False

    # مرحله ۲ معافیت قطعی مالکین هنگام شمارش پیام
    exempt_reason = await check_exempt_async(
        bot, chat_id, uid, entity=sender, chat=chat, logger=logger
    )
    if exempt_reason is not None:
        _delete_record(chat_id, uid)
        if logger and hasattr(logger, "log_info"):
            logger.log_info(
                f"[MSG_LIMIT] حذف محدودیت هنگام شمارش به علت معافیت ({exempt_reason}) chat={chat_id} user={uid}"
            )
        return False

    if sender is not None:
        remember_user_entity(bot, sender)
    if chat is not None:
        remember_group_entity(bot, chat_id, chat)

    msg_obj = getattr(event, "message", None)
    raw_msg_id = getattr(msg_obj, "id", None)
    msg_id = int(raw_msg_id) if isinstance(raw_msg_id, int) else None

    should_mute = False
    trigger_epoch = 0
    now_ts = time.time()

    with _STATE_LOCK:
        with runtime_db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM message_limits WHERE group_id = ? AND user_id = ?",
                (gkey, ukey),
            ).fetchone()
            if row is None:
                _index_pop(gkey, ukey)
                return False
            rec = _row_to_record(row)
            if rec["status"] != STATUS_COUNTING:
                _index_put(rec)
                return False

            seen_ids = list(rec["seen_msg_ids"])
            if msg_id is not None:
                if msg_id in seen_ids:
                    return False
                seen_ids.append(msg_id)
                if len(seen_ids) > MAX_SEEN_MESSAGE_IDS:
                    seen_ids = seen_ids[-MAX_SEEN_MESSAGE_IDS:]

            new_sent = int(rec["sent_count"]) + 1
            allowed = int(rec["allowed_count"])
            fresh_uname = (
                str(getattr(sender, "username", "") or "").strip().lstrip("@")
                if sender is not None
                else ""
            ) or rec["username"]
            fresh_dname = _user_label(
                sender,
                fallback_username=fresh_uname,
                fallback_name=rec["display_name"],
            )

            if new_sent >= allowed:
                next_status = STATUS_PENDING_MUTE
                should_mute = True
                trigger_epoch = int(rec["epoch"])
            else:
                next_status = STATUS_COUNTING

            conn.execute(
                """
                UPDATE message_limits
                SET sent_count = ?,
                    status = ?,
                    username = ?,
                    display_name = ?,
                    seen_msg_ids_json = ?,
                    trigger_message_id = COALESCE(?, trigger_message_id),
                    updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (
                    new_sent,
                    next_status,
                    fresh_uname,
                    fresh_dname,
                    json.dumps(seen_ids),
                    msg_id if should_mute else None,
                    now_ts,
                    gkey,
                    ukey,
                ),
            )
            updated_row = conn.execute(
                "SELECT * FROM message_limits WHERE group_id = ? AND user_id = ?",
                (gkey, ukey),
            ).fetchone()
            saved = _row_to_record(updated_row)
            _index_put(saved)

    if not should_mute:
        return False

    qkey = _queue_key(chat_id, uid)
    dedupe_key = f"auto_mute:{trigger_epoch}"
    try:
        fut = await QUEUE.submit(
            qkey,
            lambda: _execute_auto_mute(
                bot,
                chat_id,
                uid,
                trigger_epoch,
                event=event,
                sender=sender,
                chat=chat,
                message_id=msg_id,
                logger=logger,
            ),
            dedupe_key=dedupe_key,
        )
    except QueueFull as q_exc:
        if "duplicate" in str(q_exc).lower():
            return False
        if logger and hasattr(logger, "log_error"):
            logger.log_error(
                f"[MSG_LIMIT] صف پر است؛ اجرای مستقیم میوت برای chat={chat_id} user={uid}: {q_exc}"
            )
        return await _execute_auto_mute(
            bot,
            chat_id,
            uid,
            trigger_epoch,
            event=event,
            sender=sender,
            chat=chat,
            message_id=msg_id,
            logger=logger,
        )

    try:
        return bool(await asyncio.wait_for(asyncio.shield(fut), timeout=MUTE_WAIT_SECONDS))
    except asyncio.TimeoutError:
        # کار در صف مستقل ادامه می‌یابد و کامل خواهد شد.
        return True


async def cancel_limit(
    bot,
    chat_id,
    target_id,
    *,
    actor_id=None,
    entity=None,
    chat=None,
    logger=None,
) -> dict:
    """لغو کامل محدودیت پیام، حذف شمارنده و رفع سکوتِ ناشی از این قابلیت در همان گروه."""
    uid = peers.user_identifier(target_id)
    if uid is None:
        raise MessageLimitError("invalid_target")

    if entity is not None:
        remember_user_entity(bot, entity)
    if chat is not None:
        remember_group_entity(bot, chat_id, chat)

    qkey = _queue_key(chat_id, uid)

    async def _job():
        existing = get_record(chat_id, uid)
        if existing is None:
            raise NoActiveLimitError("no_active_limit")

        unmuted_api = False
        kept_other_restriction = None
        if existing.get("muted_by_limit"):
            kept_other_restriction = _has_independent_restriction(
                bot, chat_id, uid, existing.get("username", "")
            )
            if kept_other_restriction is None:
                ok = await _restore_or_unmute_via_existing_system(
                    bot,
                    chat_id,
                    uid,
                    pre_banned_rights=existing.get("pre_banned_rights"),
                    user=entity,
                    chat=chat,
                    logger=logger,
                )
                if not ok:
                    raise UnmuteFailedError("unmute_failed")
                unmuted_api = True

        removed = _delete_record(chat_id, uid)
        try:
            admin_tools.log_action(
                chat_id,
                actor_id or 0,
                "cancel_message_limit",
                int(uid),
                {
                    "was_muted_by_limit": bool(existing.get("muted_by_limit")),
                    "unmuted_api": unmuted_api,
                    "kept_other_restriction": kept_other_restriction,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        if logger and hasattr(logger, "log_info"):
            logger.log_info(
                f"[MSG_LIMIT] لغو محدودیت پیام chat={chat_id} user={uid} by={actor_id} "
                f"unmuted={unmuted_api} other_lock={kept_other_restriction}"
            )
        return removed or existing

    return await QUEUE.run(qkey, _job)


# ---------------------------------------------------------------------------
#  ریکاوری پس از ری‌استارت ربات
# ---------------------------------------------------------------------------
async def recover_pending(bot, logger=None) -> int:
    """تکمیل میوت‌های ناتمامِ ``pending_mute`` داخل صف مستقل پس از ری‌استارت."""
    _ensure_loaded()
    pending_rows: List[dict] = []
    with _STATE_LOCK:
        for group_records in _ACTIVE_INDEX.values():
            for rec in group_records.values():
                if rec.get("status") == STATUS_PENDING_MUTE:
                    pending_rows.append(dict(rec))

    resumed = 0
    futures = []
    for rec in pending_rows:
        try:
            raw_gid = str(rec["group_id"])
            if raw_gid.lstrip("-").isdigit():
                ival = int(raw_gid)
                chat_id = -(1_000_000_000_000 + ival) if ival > 0 else ival
            else:
                chat_id = raw_gid
            uid = int(rec["user_id"])
            qkey = _queue_key(chat_id, uid)
            epoch = int(rec["epoch"])
            fut = await QUEUE.submit(
                qkey,
                lambda cid=chat_id, u=uid, ep=epoch, mid=rec.get("trigger_message_id"): _execute_auto_mute(
                    bot,
                    cid,
                    u,
                    ep,
                    message_id=mid,
                    logger=logger,
                ),
                dedupe_key=f"auto_mute:{epoch}",
            )
            futures.append(fut)
            resumed += 1
        except QueueFull as q_exc:
            if "duplicate" not in str(q_exc).lower() and logger and hasattr(logger, "log_error"):
                logger.log_error(f"[MSG_LIMIT] خطا در بازیابی ردیف pending_mute: {q_exc}")
        except Exception as exc:  # noqa: BLE001
            if logger and hasattr(logger, "log_error"):
                logger.log_error(f"[MSG_LIMIT] خطا در بازیابی ردیف pending_mute: {exc}")

    if futures:
        await asyncio.gather(*futures, return_exceptions=True)
    await QUEUE.drain()
    return resumed


def recover(bot, logger=None) -> dict:
    """بارگذاری مجدد وضعیت محدودیت‌ها از SQLite و زمان‌بندی تکمیل میوت‌های ناتمام."""
    _ensure_loaded(force=True)
    counting_total = 0
    muted_total = 0
    pending_total = 0

    with _STATE_LOCK:
        for group_records in _ACTIVE_INDEX.values():
            for rec in group_records.values():
                st = rec.get("status")
                if st == STATUS_COUNTING:
                    counting_total += 1
                elif st == STATUS_MUTED and rec.get("muted_by_limit"):
                    muted_total += 1
                elif st == STATUS_PENDING_MUTE:
                    pending_total += 1

    if pending_total > 0:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(recover_pending(bot, logger=logger))
        except RuntimeError:
            pass

    if logger and hasattr(logger, "log_info") and (counting_total or muted_total or pending_total):
        logger.log_info(
            f"[MSG_LIMIT] بازیابی پس از ری‌استارت: counting={counting_total}, "
            f"muted={muted_total}, pending={pending_total}"
        )
    return {
        "counting": counting_total,
        "muted": muted_total,
        "pending": pending_total,
    }


def reset_runtime_caches() -> None:
    """پاک‌سازی کش‌های حافظه‌ای (برای شبیه‌سازی ری‌استارت واقعی در تست‌ها)."""
    global _READY_CONN_ID
    with _STATE_LOCK:
        _ACTIVE_INDEX.clear()
        _READY_CONN_ID = None
        _ROLE_CACHE.clear()
        _ROLE_LOCKS.clear()
    reset_queue(QUEUE)


_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_cmd(text: str) -> str:
    """نرمال‌سازی متن دستور (حذف نیم‌فاصله و کاراکترهای جهت‌دهی، یکسان‌سازی ی/ک و ارقام)."""
    value = str(text or "")
    for ch in ("\u200c", "\u200f", "\u200e", "\ufeff", "\u202a", "\u202b", "\u202c"):
        value = value.replace(ch, " ")
    value = value.replace("ي", "ی").replace("ك", "ک").translate(_DIGIT_MAP)
    return " ".join(value.split())


def parse_set_command(text: str) -> Tuple[bool, Optional[int]]:
    """تشخیص دستور ``محدودیت پیام <عدد>``.

    خروجی:
    * ``(False, None)`` اگر متن اصلاً دستور «محدودیت پیام» نیست.
    * ``(True, N)`` اگر عدد صحیح داده‌شده ``N > 0`` باشد.
    * ``(True, None)`` اگر دستور «محدودیت پیام» زده شده ولی عدد نامعتبر/صفر/منفی/خالی است.
    """
    norm = normalize_cmd(text)
    if not norm:
        return False, None
    if norm == "محدودیت پیام":
        return True, None
    match = re.match(r"^محدودیت پیام\s+([+-]?\d+)$", norm)
    if match:
        val = int(match.group(1))
        return True, (val if val > 0 else None)
    if norm.startswith("محدودیت پیام "):
        return True, None
    return False, None


def is_cancel_command(text: str) -> bool:
    """تشخیص دستور ``لغو محدودیت پیام`` (و فرم خلاصهٔ راهنما ``لغو محدودیت``)."""
    norm = normalize_cmd(text)
    return norm in ("لغو محدودیت پیام", "لغو محدودیت")
