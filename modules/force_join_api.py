"""🔌 آداپتر SPlusthon برای «عضویت اجباری کانال» — تنها فایلی که به کتابخانه
دست می‌زند.

هدف: اگر روزی کتابخانه عوض شد (مثلاً NSplusthon)، فقط همین فایل تغییر کند.
هیچ ماژول دیگری از ``splusthon`` ایمپورت نمی‌کند.

روش‌های تأییدشده روی نسخهٔ نصب‌شده (SPlusthon 1.1.4 — با ``hasattr`` و
مطالعهٔ سورس ``tl/functions/channels.py`` و ``client/chats.py`` بررسی شده):

``channels.getParticipant#a0ab6cc7``
    ``GetParticipantRequest(channel: InputChannel, participant: InputPeer)``
    پاسخ: ``channels.ChannelParticipant`` با فیلد ``participant`` از نوع
    ``ChannelParticipant*``. نیازمند ``access_hash`` کاربر است
    (``splusthon.utils.get_input_user(user)`` آن را بدون RPC می‌دهد).
    خطای ``UserNotParticipantError`` = «عضو نیست».

``channels.getParticipants#77ced9d0``
    ``GetParticipantsRequest(channel, filter, offset, limit, hash)``
    سقف صفحه در خود کتابخانه: ``_MAX_PARTICIPANTS_CHUNK_SIZE = 200``
    (``client/chats.py``). پاسخ ``channels.ChannelParticipants`` یا
    ``channels.ChannelParticipantsNotModified`` (فقط وقتی ``hash`` غیر صفر
    باشد؛ نوع آن زیر ``splusthon.tl.types.channels`` است، نه
    ``splusthon.types``).

``channels.getFullChannel#08736a95``
    ``GetFullChannelRequest(channel)`` → ``ChannelFull`` با فیلدهای
    ``can_view_participants``، ``participants_hidden``،
    ``participants_count``، ``admins_count``، ``kicked_count``.

``channels.getAdminLog`` **وجود ندارد** (نه در این نسخه و نه در
NSplusthon 1.8.2 / SplusPy 2.2.12)؛ پس ``client.iter_admin_log`` عملاً با
``AttributeError`` می‌شکند. بنابراین Adapter هرگز از آن استفاده نمی‌کند.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

try:  # pragma: no cover - در محیط تست همیشه نصب است
    from splusthon import utils as _splus_utils
    from splusthon.tl.functions.channels import (
        GetFullChannelRequest,
        GetParticipantRequest,
        GetParticipantsRequest,
    )
    from splusthon.tl.types import (
        ChannelParticipant,
        ChannelParticipantAdmin,
        ChannelParticipantBanned,
        ChannelParticipantCreator,
        ChannelParticipantLeft,
        ChannelParticipantSelf,
        ChannelParticipantsRecent,
        InputChannel,
        InputPeerChannel,
        InputPeerUser,
    )
    try:
        from splusthon.errors import (
            ChannelInvalidError,
            ChannelPrivateError,
            ChatAdminRequiredError,
            UserNotParticipantError,
        )
    except Exception:  # pragma: no cover - نسخه‌های قدیمی‌تر
        from splusthon.errors import rpcerrorlist as _rpcerrors
        ChatAdminRequiredError = _rpcerrors.ChatAdminRequiredError
        UserNotParticipantError = _rpcerrors.UserNotParticipantError
        ChannelPrivateError = _rpcerrors.ChannelPrivateError
        ChannelInvalidError = _rpcerrors.ChannelInvalidError
    LIBRARY_AVAILABLE = True
    LIBRARY_ERROR = None
except Exception as error:  # pragma: no cover - محیط بدون کتابخانه
    LIBRARY_AVAILABLE = False
    LIBRARY_ERROR = error
    GetFullChannelRequest = GetParticipantRequest = GetParticipantsRequest = None
    ChannelParticipant = ChannelParticipantAdmin = ChannelParticipantBanned = None
    ChannelParticipantCreator = ChannelParticipantLeft = ChannelParticipantSelf = None
    ChannelParticipantsRecent = InputChannel = InputPeerChannel = InputPeerUser = None

    class ChatAdminRequiredError(Exception):
        pass

    class UserNotParticipantError(Exception):
        pass

    class ChannelPrivateError(Exception):
        pass

    class ChannelInvalidError(Exception):
        pass


# ---------------------------------------------------------------------------
# وضعیت‌ها و خطاها
# ---------------------------------------------------------------------------

RESULT_MEMBER = "member"
RESULT_NOT_MEMBER = "not_member"
RESULT_NO_ACCESS = "no_access"
RESULT_FLOOD = "flood_wait"
RESULT_ERROR = "error"

#: شرکت‌کننده‌هایی که «عضو بودن» را ثابت می‌کنند.
_MEMBER_TYPES = tuple(
    item for item in (
        ChannelParticipant, ChannelParticipantAdmin,
        ChannelParticipantCreator, ChannelParticipantSelf,
    ) if item is not None
)
#: شرکت‌کننده‌هایی که یعنی «عضو نیست» (خروج/اخراج).
_LEFT_TYPES = tuple(
    item for item in (ChannelParticipantLeft, ChannelParticipantBanned)
    if item is not None
)

#: شمارندهٔ فراخوانی‌های API — برای بنچمارک و تست مسیر گرم.
CALL_COUNTER = {"resolve": 0, "get_participant": 0, "get_full": 0,
                "get_participants": 0}


def reset_counters() -> None:
    for key in CALL_COUNTER:
        CALL_COUNTER[key] = 0


def total_calls() -> int:
    return sum(CALL_COUNTER.values())


def call_stats() -> dict:
    return dict(CALL_COUNTER)


def classify_error(error) -> str:
    """خطای RPC را به یکی از حالت‌های تصمیم‌گیری تبدیل می‌کند.

    تفکیک «عضو نیست» از «دسترسی نداریم» حیاتی است؛ اشتباه در این تفکیک
    باعث می‌شود در قطعی، پیام کاربران واقعی حذف شود.
    """
    if error is None:
        return RESULT_ERROR
    if isinstance(error, UserNotParticipantError):
        return RESULT_NOT_MEMBER
    if isinstance(error, (ChatAdminRequiredError, ChannelPrivateError,
                         ChannelInvalidError)):
        return RESULT_NO_ACCESS
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "usernotparticipant" in name:
        return RESULT_NOT_MEMBER
    if "flood" in name or "flood_wait" in text or "too many requests" in text \
            or "slowmode" in name:
        return RESULT_FLOOD
    if "chatadminrequired" in name or "channelprivate" in name or \
            "channelinvalid" in name or "not enough rights" in text or \
            "admin required" in text:
        return RESULT_NO_ACCESS
    return RESULT_ERROR


def flood_seconds(error, default: float = 60.0) -> float:
    """ثانیهٔ انتظارِ FloodWait (بدون ایمپورت سنگین)."""
    for attribute in ("seconds", "value", "wait_seconds"):
        value = getattr(error, attribute, None)
        if isinstance(value, (int, float)):
            return float(value)
    import re
    match = re.search(r"(\d+(?:\.\d+)?)", str(error))
    return float(match.group(1)) if match else default


# ---------------------------------------------------------------------------
# ورودی‌ها (بدون RPC)
# ---------------------------------------------------------------------------

def input_channel(channel_id, access_hash):
    """ساخت ``InputChannel`` از کش دیتابیس — صفر فراخوانی API."""
    if not LIBRARY_AVAILABLE or access_hash is None:
        return None
    try:
        return InputChannel(int(channel_id), int(access_hash))
    except Exception:
        return None


def input_user(entity):
    """``InputPeerUser`` از یک آبجکت ``User``/``InputPeer`` — بدون RPC.

    اگر ``access_hash`` در دست نباشد ``None`` برمی‌گردد تا فراخوان صریحاً
    یک resolve انجام دهد (و همان را کش کند).
    """
    if entity is None:
        return None
    if InputPeerChannel is not None and isinstance(entity, (InputPeerChannel,)):
        return None
    if InputPeerUser is not None and isinstance(entity, InputPeerUser):
        return entity
    if LIBRARY_AVAILABLE and _splus_utils is not None:
        try:
            return _splus_utils.get_input_user(entity)
        except Exception:
            pass
    try:
        user_id = int(getattr(entity, "id", entity))
        access_hash = getattr(entity, "access_hash", None)
    except (TypeError, ValueError):
        return None
    if not user_id or access_hash is None or InputPeerUser is None:
        return None
    try:
        return InputPeerUser(user_id, int(access_hash))
    except Exception:
        return None


def input_user_parts(user_id, access_hash):
    """``InputPeerUser`` از شناسه و ``access_hash`` ذخیره‌شده — بدون RPC."""
    if not LIBRARY_AVAILABLE or InputPeerUser is None or access_hash is None:
        return None
    try:
        return InputPeerUser(int(user_id), int(access_hash))
    except Exception:
        return None


def entity_is_channel(entity) -> bool:
    """آیا این آبجکت یک کانال است (پست بی‌نام/کانالی)؟"""
    name = type(entity).__name__.lower()
    return "channel" in name and "participant" not in name


def channel_access_hash(entity) -> Optional[int]:
    value = getattr(entity, "access_hash", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def channel_link(entity) -> Optional[str]:
    """لینک عمومی کانال از entity (بدون RPC)."""
    username = getattr(entity, "username", None)
    if username:
        return "https://splus.ir/" + str(username).lstrip("@")
    for attribute in ("usernames",):
        for item in getattr(entity, attribute, None) or ():
            value = getattr(item, "username", None)
            if value:
                return "https://splus.ir/" + str(value).lstrip("@")
    return None


# ---------------------------------------------------------------------------
# فراخوانی‌های شبکه
# ---------------------------------------------------------------------------

async def resolve_channel(client, target, *, timeout: float = 20.0) -> dict:
    """resolve یک کانال با لینک/@username/آیدی (یک فراخوانی حداکثر).

    خروجی: ``{"ok": bool, "reason": str|None, "channel_id", "access_hash",
    "title", "link", "entity"}``.
    """
    if not LIBRARY_AVAILABLE:
        return {"ok": False, "reason": "library_missing"}
    CALL_COUNTER["resolve"] += 1
    entity = None
    error = None
    for candidate in _resolve_candidates(target):
        try:
            entity = await asyncio.wait_for(
                client.get_entity(candidate), timeout=timeout
            )
            if entity is None:
                continue
            break
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            error = failure
            entity = None
    if entity is None:
        return {
            "ok": False,
            "reason": classify_error(error) if error else RESULT_ERROR,
            "error": error,
        }
    if not entity_is_channel(entity):
        return {"ok": False, "reason": "not_a_channel", "entity": entity}
    try:
        channel_id = int(entity.id)
    except Exception:
        return {"ok": False, "reason": RESULT_ERROR}
    return {
        "ok": True,
        "reason": None,
        "channel_id": channel_id,
        "access_hash": channel_access_hash(entity),
        "title": getattr(entity, "title", None),
        "link": channel_link(entity),
        "entity": entity,
    }


def _resolve_candidates(target):
    """شکل‌های ممکن ورودی کاربر: لینک، ``@username``، آیدی علامت‌دار/ساده."""
    if target is None:
        return ()
    if not isinstance(target, str):
        return (target,)
    value = str(target).strip()
    if not value:
        return ()
    lowered = value.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            lowered = value.lower()
            break
    for host in ("splus.ir/", "sapp.ir/", "soroush.ir/", "t.me/",
                 "telegram.me/", "www."):
        if lowered.startswith(host):
            value = value[len(host):]
            lowered = value.lower()
            break
    value = value.strip().strip("/")
    if value.startswith("joinchat/") or value.startswith("+"):
        # لینک دعوت خصوصی: resolve با get_entity ممکن نیست.
        return tuple()
    candidates = []
    if value.startswith("@"):
        candidates.append(value)
        candidates.append(value[1:])
    elif value and not value.lstrip("-").isdigit():
        candidates.append(value)
        candidates.append("@" + value)
    else:
        number = value.lstrip("-")
        if number.isdigit():
            from modules.force_join_ids import canonical_group_key
            candidates.append(canonical_group_key(int(value)))
            candidates.append(int(number))
    return tuple(candidates)


async def get_full_channel(client, channel_input, *, timeout: float = 20.0) -> dict:
    """خواندن ``ChannelFull``: تعیین می‌کند فهرست اعضا خواندنی است یا نه."""
    if not LIBRARY_AVAILABLE or channel_input is None:
        return {"ok": False, "reason": "unavailable"}
    CALL_COUNTER["get_full"] += 1
    try:
        full = await asyncio.wait_for(
            client(GetFullChannelRequest(channel_input)), timeout=timeout
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return {"ok": False, "reason": classify_error(error), "error": error}
    chat = getattr(full, "full_chat", None)
    if chat is None:
        return {"ok": False, "reason": RESULT_ERROR}
    return {
        "ok": True,
        "can_view_participants": getattr(chat, "can_view_participants", None),
        "participants_hidden": getattr(chat, "participants_hidden", None),
        "participants_count": getattr(chat, "participants_count", None),
        "admins_count": getattr(chat, "admins_count", None),
        "about": getattr(chat, "about", None),
        "chat": chat,
    }


def participant_state(participant) -> str:
    """نوع ``ChannelParticipant*`` را به ``member``/``not_member`` تبدیل می‌کند."""
    if participant is None:
        return RESULT_NOT_MEMBER
    if _LEFT_TYPES and isinstance(participant, _LEFT_TYPES):
        return RESULT_NOT_MEMBER
    if _MEMBER_TYPES and isinstance(participant, _MEMBER_TYPES):
        return RESULT_MEMBER
    name = type(participant).__name__.lower()
    if "left" in name or "banned" in name or "kicked" in name:
        return RESULT_NOT_MEMBER
    if "participant" in name:
        return RESULT_MEMBER
    return RESULT_NOT_MEMBER


async def get_participant(client, channel_input, user_input, *,
                          timeout: float = 12.0) -> dict:
    """بررسی عضویت **یک کاربر** — همان «یک فراخوانی» مسیر گرم.

    خروجی: ``{"state": member|not_member|no_access|flood_wait|error,
    "participant": ..., "join_date": ..., "seconds": ...}``.
    """
    if not LIBRARY_AVAILABLE or channel_input is None or user_input is None:
        return {"state": RESULT_ERROR, "reason": "unavailable"}
    CALL_COUNTER["get_participant"] += 1
    try:
        result = await asyncio.wait_for(
            client(GetParticipantRequest(channel=channel_input,
                                         participant=user_input)),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        state = classify_error(error)
        return {
            "state": state,
            "error": error,
            "seconds": flood_seconds(error) if state == RESULT_FLOOD else None,
        }
    participant = getattr(result, "participant", None)
    state = participant_state(participant)
    join_date = getattr(participant, "date", None)
    try:
        join_date = int(join_date) if join_date is not None else None
    except (TypeError, ValueError):
        join_date = None
    return {"state": state, "participant": participant, "join_date": join_date}


async def iter_members(client, channel_input, *, limit: Optional[int] = None,
                       budget_seconds: float = 60.0,
                       delay: float = 0.35, page_size: int = 200) -> dict:
    """پیمایش اعضای کانال با صفحه‌بندی صریح، بودجهٔ زمانی و فاصلهٔ RPC.

    خروجی: ``{"users": [...], "complete": bool, "reason": str|None,
    "pages": int}``. ``complete=False`` یعنی اسنپ‌شات ناقص است و **نباید**
    مبنای «خارج‌شده» قرار گیرد.
    """
    summary = {"users": [], "complete": False, "reason": None, "pages": 0}
    if not LIBRARY_AVAILABLE or channel_input is None:
        summary["reason"] = "unavailable"
        return summary
    offset = 0
    started = time.monotonic()
    cap = limit if limit else 0
    while True:
        if cap and offset >= cap:
            summary["complete"] = True
            break
        if time.monotonic() - started > budget_seconds:
            summary["reason"] = "budget"
            break
        page_limit = page_size if not cap else min(page_size, cap - offset)
        CALL_COUNTER["get_participants"] += 1
        try:
            page = await asyncio.wait_for(
                client(GetParticipantsRequest(
                    channel=channel_input,
                    filter=ChannelParticipantsRecent(),
                    offset=offset,
                    limit=page_limit,
                    hash=0,
                )),
                timeout=30.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            summary["reason"] = classify_error(error)
            summary["error"] = error
            return summary
        summary["pages"] += 1
        users = list(getattr(page, "users", None) or ())
        participants = list(getattr(page, "participants", None) or ())
        if not users and not participants:
            summary["complete"] = True
            break
        for index, user in enumerate(users):
            participant = participants[index] if index < len(participants) else None
            if participant is not None and participant_state(participant) != RESULT_MEMBER:
                continue
            summary["users"].append(user)
        offset += len(users) if users else len(participants)
        total = getattr(page, "count", None)
        if users and total is not None and offset >= int(total):
            summary["complete"] = True
            break
        if delay:
            await asyncio.sleep(delay)
    return summary
