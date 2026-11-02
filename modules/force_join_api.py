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
#: سرور این متد را پیاده نکرده است (سروش: ``422 NOT_SUPPORTED``) — نه
#: «عضو نیست» و نه «دسترسی نداریم»؛ باید مسیر دیگری امتحان شود.
RESULT_NOT_SUPPORTED = "not_supported"

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


NOT_SUPPORTED_MARKERS = ("not_supported", "not supported", "notsupported",
                         "not_implemented", "notimplemented", "unsupported")


def is_not_supported(error) -> bool:
    """آیا سرور می‌گوید «این متد را ندارم»؟

    سروش پلاس برای بعضی متدهای موجود در TL Schema خطای
    ``RPCError 422: NOT_SUPPORTED`` می‌دهد (مثلاً ``channels.getParticipant``).
    این حالت با «عضو نیست» و «دسترسی نداریم» یکی نیست و نباید آن‌ها را
    جایگزین کند.
    """
    if error is None:
        return False
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "notsupported" in name or "not_supported" in name or \
            "unimplemented" in name:
        return True
    return any(marker in text for marker in NOT_SUPPORTED_MARKERS)


def classify_error(error) -> str:
    """خطای RPC را به یکی از حالت‌های تصمیم‌گیری تبدیل می‌کند.

    تفکیک «عضو نیست» از «دسترسی نداریم» حیاتی است؛ اشتباه در این تفکیک
    باعث می‌شود در قطعی، پیام کاربران واقعی حذف شود.
    """
    if error is None:
        return RESULT_ERROR
    if is_not_supported(error):
        # باید **قبل** از بقیه بررسی شود: متن خطای سروش شامل کلمهٔ NOT_SUPPORTED
        # است و اگر دیرتر بررسی شود، به‌اشتباه «خطای عمومی» یا «دسترسی» می‌شود.
        return RESULT_NOT_SUPPORTED
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


# ---------------------------------------------------------------------------
# مسیر رسمی Bot API: getChatMember (همان روشی که ربات‌های تلگرامی استفاده
# می‌کنند). ربات رسمی باید ادمین همان کانال باشد.
# ---------------------------------------------------------------------------

BOT_API_BASES = ("https://bot.splus.ir/v2/{}", "https://bot.splus.ir/{}")

_BOT_API_MEMBER_STATES = {"member", "administrator", "creator", "restricted"}
_BOT_API_LEFT_STATES = {"left", "kicked"}


def _bot_api_call_sync(token, method, payload, timeout):
    """POST فرم‌محور به Bot API (همگام؛ در thread جدا اجرا می‌شود)."""
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request

    last_error = None
    for base in BOT_API_BASES:
        url = base.format(token) + "/" + method
        data = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return {"ok": True, "payload": _json.loads(response.read().decode())}
        except urllib.error.HTTPError as error:
            try:
                body = error.read().decode()
            except Exception:
                body = ""
            try:
                parsed = _json.loads(body) if body else {}
            except Exception:
                parsed = {}
            return {"ok": False, "http": error.code, "payload": parsed,
                    "text": str(parsed.get("description") or body)[:200]}
        except Exception as error:  # شبکه/تایم‌اوت/دامنه
            last_error = error
            continue
    return {"ok": False, "http": 0, "text": type(last_error).__name__}


async def bot_api_get_chat_member(token, chat_id, user_id, *,
                                  timeout: float = 10.0) -> dict:
    """``getChatMember`` رسمی — یک درخواست HTTP، بدون پیمایش فهرست.

    خروجی: ``{"state": member|not_member|no_access|error|unsupported,
    "status": str|None, "text": str|None}``.
    """
    if not token or chat_id is None or not user_id:
        return {"state": "unsupported", "status": None, "text": "no_token"}
    CALL_COUNTER["bot_api_get_chat_member"] = (
        CALL_COUNTER.get("bot_api_get_chat_member", 0) + 1
    )
    payload = {"chat_id": str(chat_id), "user_id": str(user_id)}
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_bot_api_call_sync, token, "getChatMember",
                              payload, timeout),
            timeout=timeout + 2.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return {"state": "error", "status": None,
                "text": type(error).__name__}
    if not raw.get("ok"):
        text = (raw.get("text") or "").lower()
        if "not_found" in text or "not found" in text or \
                "participant_id_invalid" in text or "user not found" in text:
            return {"state": "not_member", "status": None, "text": text}
        if "not enough rights" in text or "chat_admin_required" in text or \
                "not a member" in text or "forbidden" in text:
            return {"state": "no_access", "status": None, "text": text}
        if any(marker in text for marker in NOT_SUPPORTED_MARKERS) or \
                raw.get("http") == 404:
            return {"state": "unsupported", "status": None, "text": text}
        return {"state": "error", "status": None, "text": text}

    body = raw.get("payload") or {}
    if not body.get("ok"):
        text = str(body.get("description") or "").lower()
        if "not found" in text or "participant_id_invalid" in text:
            return {"state": "not_member", "status": None, "text": text}
        if "rights" in text or "admin" in text or "forbidden" in text:
            return {"state": "no_access", "status": None, "text": text}
        return {"state": "error", "status": None, "text": text}
    member = body.get("result") or {}
    status = str(member.get("status") or "").lower()
    if status in _BOT_API_MEMBER_STATES:
        if status == "restricted" and member.get("is_member") is False:
            return {"state": "not_member", "status": status, "text": None}
        return {"state": "member", "status": status, "text": None}
    if status in _BOT_API_LEFT_STATES:
        return {"state": "not_member", "status": status, "text": None}
    return {"state": "error", "status": status or None,
            "text": "unknown_status"}


async def list_admins(client, channel_input, *, limit: int = 100,
                      timeout: float = 15.0) -> dict:
    """فهرست **ادمین‌های** کانال (``ChannelParticipantsAdmins``).

    برای تشخیص «آیا حساب ما ادمین است؟» بدون ``getParticipant`` — چون آن متد
    روی سروش پشتیبانی نمی‌شود. یک صفحه، حداقل هزینه.
    """
    result = {"state": RESULT_ERROR, "user_ids": [], "error": None}
    if not LIBRARY_AVAILABLE or channel_input is None:
        return result
    CALL_COUNTER["list_admins"] = CALL_COUNTER.get("list_admins", 0) + 1
    try:
        from splusthon.tl.types import ChannelParticipantsAdmins
        page = await asyncio.wait_for(
            client(GetParticipantsRequest(
                channel=channel_input,
                filter=ChannelParticipantsAdmins(),
                offset=0,
                limit=max(1, min(int(limit), 200)),
                hash=0,
            )),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        result["error"] = error
        result["state"] = classify_error(error)
        return result
    result["user_ids"] = [
        int(getattr(user, "id", 0)) for user in (getattr(page, "users", None) or ())
        if getattr(user, "id", None)
    ]
    result["state"] = "ok"
    return result


async def list_probe(client, channel_input, *, timeout: float = 15.0) -> dict:
    """سنجش ارزانِ «آیا این حساب می‌تواند فهرست اعضا را ببیند؟»

    فقط **یک** درخواست با ``limit=1`` (بدون پیمایش)؛ خروجی:

    ``{"state": "ok"|"empty"|"no_access"|"not_supported"|"flood"|"error",
    "count": int, "error": Exception|None, "seconds": float|None}``
    """
    result = {"state": RESULT_ERROR, "count": 0, "error": None, "seconds": None}
    if not LIBRARY_AVAILABLE or channel_input is None:
        return result
    CALL_COUNTER["list_probe"] = CALL_COUNTER.get("list_probe", 0) + 1
    try:
        page = await asyncio.wait_for(
            client(GetParticipantsRequest(
                channel=channel_input,
                filter=ChannelParticipantsRecent(),
                offset=0,
                limit=1,
                hash=0,
            )),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        result["error"] = error
        result["state"] = classify_error(error)
        result["seconds"] = flood_seconds(error, 0.0) or None
        return result
    users = list(getattr(page, "users", None) or ())
    result["count"] = len(users)
    result["state"] = "ok" if users else "empty"
    return result


async def search_participants(client, channel_input, query, target_user_id,
                              *, limit: int = 20, timeout: float = 15.0) -> dict:
    """جست‌وجوی **هدفمند** در فهرست اعضا با ``limit`` کوچک.

    این مسیر فقط برای یک کار استفاده می‌شود: وقتی ``access_hash`` کاربر را
    نداریم و می‌خواهیم آبجکت کامل ``User`` را از همان گروه/کانال بگیریم
    (چون در آن گروه عضو است و پیام داده). هرگز برای «کل فهرست» صدا زده
    نمی‌شود و سقف پیش‌فرض ۲۰ نفر است.

    خروجی: ``{"state": "found"|"absent"|"no_access"|"error"|"flood",
    "user": <User|None>}``.
    """
    result = {"state": "absent", "user": None}
    if not LIBRARY_AVAILABLE or channel_input is None:
        result["state"] = RESULT_ERROR
        return result
    text = str(query or "").strip().lstrip("@")
    if not text:
        return result
    CALL_COUNTER["search_participants"] = CALL_COUNTER.get("search_participants", 0) + 1
    try:
        from splusthon.tl.types import ChannelParticipantsSearch
        page = await asyncio.wait_for(
            client(GetParticipantsRequest(
                channel=channel_input,
                filter=ChannelParticipantsSearch(text),
                offset=0,
                limit=max(1, min(int(limit), 50)),
                hash=0,
            )),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        result["state"] = classify_error(error)
        result["error"] = error
        return result
    users = list(getattr(page, "users", None) or ())
    for user in users:
        if target_user_id and int(getattr(user, "id", 0)) == int(target_user_id):
            result["state"] = "found"
            result["user"] = user
            return result
    result["state"] = "absent"
    result["users_seen"] = len(users)
    return result


async def find_members(client, channel_input, user_ids, *,
                       max_members: int = 5000,
                       budget_seconds: float = 20.0,
                       delay: float = 0.1,
                       page_size: int = 200) -> dict:
    """مثل ``find_member`` ولی برای **چند** کاربر با یک پیمایش.

    چرا: تأیید عضویت چند کاربرِ در انتظار نباید چند بار فهرست را بخواند؛
    یک پاس، همه را جواب می‌دهد (مصرف = همان یک پیمایش).
    """
    wanted = set()
    for value in user_ids or ():
        try:
            wanted.add(int(value))
        except (TypeError, ValueError):
            continue
    summary = {"state": RESULT_ERROR, "found": {}, "scanned": 0, "pages": 0,
               "complete": False, "reason": None}
    if not wanted:
        summary.update(state="absent", complete=True)
        return summary
    if not LIBRARY_AVAILABLE or channel_input is None:
        summary["reason"] = "unavailable"
        return summary

    offset = 0
    started = time.monotonic()
    while True:
        if max_members and offset >= max_members:
            summary["reason"] = "cap"
            return summary
        if time.monotonic() - started > budget_seconds:
            summary["reason"] = "budget"
            return summary
        CALL_COUNTER["get_participants"] += 1
        try:
            page = await asyncio.wait_for(
                client(GetParticipantsRequest(
                    channel=channel_input,
                    filter=ChannelParticipantsRecent(),
                    offset=offset,
                    limit=page_size,
                    hash=0,
                )),
                timeout=20.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            summary["state"] = classify_error(error)
            summary["error"] = error
            summary["reason"] = summary["state"]
            return summary
        summary["pages"] += 1
        users = list(getattr(page, "users", None) or ())
        participants = list(getattr(page, "participants", None) or ())
        for index, user in enumerate(users):
            participant = participants[index] if index < len(participants) else None
            if participant is not None and \
                    participant_state(participant) != RESULT_MEMBER:
                continue
            uid = int(getattr(user, "id", 0) or 0)
            if uid in wanted:
                summary["found"][uid] = user
        step = len(users) if users else len(participants)
        if not step:
            summary.update(state="ok", complete=True, scanned=offset)
            return summary
        offset += step
        total = getattr(page, "count", None)
        if total is not None and offset >= int(total):
            summary.update(state="ok", complete=True, scanned=offset)
            return summary
        if delay:
            await asyncio.sleep(delay)
    return summary


async def find_member(client, channel_input, user_id, *,
                      max_members: int = 5000,
                      budget_seconds: float = 20.0,
                      delay: float = 0.1,
                      page_size: int = 200) -> dict:
    """جست‌وجوی **هدفمند** یک کاربر داخل فهرست اعضا (توقف به‌محض پیدا شدن).

    چرا لازم است: روی سروش ``channels.getParticipant`` وجود ندارد، پس برای
    حکم قطعی روی **یک** کاربر باید فهرست را صفحه‌به‌صفحه دید. این تابع
    برخلاف ``iter_members`` به‌محض پیدا شدن متوقف می‌شود و سقف زمانی/تعدادی
    دارد تا هرگز به پیمایش بی‌پایان تبدیل نشود.

    خروجی: ``{"state": "found"|"absent"|"partial"|"no_access"|"flood"|"error",
    "scanned": int, "pages": int, "complete": bool, "user": User|None}``

    فقط ``absent`` همراه با ``complete=True`` یعنی «عضو نیست»؛ ``partial``
    یعنی «نمی‌دانیم» و هرگز نباید سبب حذف پیام شود.
    """
    summary = {"state": RESULT_ERROR, "scanned": 0, "pages": 0,
               "complete": False, "user": None, "reason": None}
    if not LIBRARY_AVAILABLE or channel_input is None:
        summary["reason"] = "unavailable"
        return summary
    try:
        target = int(user_id)
    except (TypeError, ValueError):
        summary["reason"] = "bad_user_id"
        return summary

    offset = 0
    started = time.monotonic()
    while True:
        if max_members and offset >= max_members:
            summary["reason"] = "cap"
            return summary
        if time.monotonic() - started > budget_seconds:
            summary["reason"] = "budget"
            return summary
        CALL_COUNTER["get_participants"] += 1
        try:
            page = await asyncio.wait_for(
                client(GetParticipantsRequest(
                    channel=channel_input,
                    filter=ChannelParticipantsRecent(),
                    offset=offset,
                    limit=page_size,
                    hash=0,
                )),
                timeout=20.0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            summary["state"] = classify_error(error)
            summary["error"] = error
            summary["reason"] = summary["state"]
            if summary["state"] == RESULT_NO_ACCESS:
                return summary
            return summary
        summary["pages"] += 1
        users = list(getattr(page, "users", None) or ())
        participants = list(getattr(page, "participants", None) or ())
        for index, user in enumerate(users):
            participant = participants[index] if index < len(participants) else None
            if participant is not None and \
                    participant_state(participant) != RESULT_MEMBER:
                continue
            if int(getattr(user, "id", 0) or 0) == target:
                summary.update(state="found", user=user,
                               scanned=offset + len(users), complete=False)
                return summary
        step = len(users) if users else len(participants)
        if not step:
            summary.update(state="absent", complete=True,
                           scanned=offset)
            return summary
        offset += step
        total = getattr(page, "count", None)
        if total is not None and offset >= int(total):
            summary.update(state="absent", complete=True, scanned=offset)
            return summary
        if delay:
            await asyncio.sleep(delay)
    return summary


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
