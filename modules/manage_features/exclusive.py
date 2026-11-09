# -*- coding: utf-8 -*-
"""🔐 «اختصاصی» — اجازهٔ ارسال پیام به یک کاربر در گروهِ قفل‌شده.

یافتهٔ واقعیِ API (splusthon 1.1.4 — بررسی‌شده، نه حدس):

* ``channels.EditBannedRequest(channel, participant, banned_rights)`` با یک
  ``ChatBannedRights`` که **همهٔ** فلگ‌هایش ``False`` است، محدودیتِ آن کاربر را
  برمی‌دارد و فقط برای همان کاربر مجوز ارسال می‌دهد — گروه همچنان برای بقیه
  قفل می‌ماند (این «استثنای per-user» است، نه بازکردن گروه).
* هیچ ارتقای ادمینی لازم نیست: نه ``EditAdminRequest`` و نه هیچ مجوز مدیریتی.
  به همین دلیل ریسکِ «ادمین کامل شدن کاربر» صفر است.
* مجوز با همان RPC و ``ChatBannedRights(send_messages=True, send_plain=True)``
  لغو می‌شود (``send_plain`` هم همیشه همراه ``send_messages`` نوشته می‌شود،
  مثل همان الگویی که در ``modules/group_actions.py`` مستند شده).
* خواندنِ وضعیت واقعی مجوز: ``channels.GetParticipantRequest`` ⇒
  ``ChannelParticipantBanned.banned_rights``.

وضعیت گروه **از سرور** خوانده می‌شود (نه از حافظه) و بعد از هر تغییر دوباره
خوانده و تأیید می‌شود؛ «قفل است» به معنیِ ``send_messages`` باندشده برای
پیش‌فرضِ گروه است.
"""
from __future__ import annotations

from modules.group_actions import (
    GroupActions,
    clone_banned_rights,
    text_is_banned,
)
from modules.manage_features import storage
from modules.manage_features.rpc import call, log_info

try:
    from splusthon.tl import functions, types
except ImportError:                                  # pragma: no cover
    functions = types = None

PENDING_FLAG = "pending_revoke"


# ---------------------------------------------------------------------------
#  دسترسی به گروه (فقط خواندن؛ هیچ تغییری در گروه_اکشنز موجود نمی‌دهیم)
# ---------------------------------------------------------------------------
def _actions(bot):
    return GroupActions(bot.client, bot.logger)


async def peer_and_channel(bot, chat_id):
    """``(peer, input_channel)`` با همان منطقِ موجودِ پروژه."""
    actions = _actions(bot)
    peer = await actions._group_peer(chat_id)
    if not (hasattr(peer, "channel_id") and hasattr(peer, "access_hash")):
        raise ValueError("chat is not a channel/supergroup")
    return peer, types.InputChannel(peer.channel_id, peer.access_hash)


class ExcludedUser(RuntimeError):
    """کاربر با دستور «آزاد» از ربات ban شده است؛ نباید پیام بفرستد.

    این استثنا عمداً وجود دارد: قابلیت «اختصاصی» هیچ‌گاه یک banِ ذخیره‌شده
    را دور نمی‌زند (بدون تغییر در لایهٔ حفاظتیِ موجود).
    """


async def user_peer(bot, chat_id, user_id):
    """«آزاد نشده» بودن کاربر را چک می‌کند و InputPeer او را می‌دهد."""
    from modules import banned_storage

    try:
        if banned_storage.is_banned(chat_id, user_id):
            raise ExcludedUser(
                f"user {user_id} is still banned by the bot in {chat_id}")
    except ExcludedUser:
        raise
    except Exception:                                # noqa: BLE001
        # اگر خودِ بررسی خطا داد، اجازه نمی‌دهیم (fail-closed).
        raise ExcludedUser("ban check failed; refusing to grant rights")

    peer, _channel = await peer_and_channel(bot, chat_id)
    try:
        return await bot.client.get_input_entity(int(user_id))
    except Exception:                                # noqa: BLE001
        # آخرین تلاش: شناسهٔ عددی خام (SPlusthon خودش resolve می‌کند)
        return int(user_id)


# ---------------------------------------------------------------------------
#  وضعیت واقعیِ گروه و مجوز
# ---------------------------------------------------------------------------
async def is_group_locked(bot, chat_id) -> bool:
    """آیا گروه الان برای اعضای عادی قفل است؟ (پاسخ از سرور)"""
    peer, _channel = await peer_and_channel(bot, chat_id)
    rights, _source = await _actions(bot)._fetch_default_banned_rights(
        chat_id, peer)
    return text_is_banned(rights)


async def grant_participant_rights(bot, chat_id, user_id) -> bool:
    """تنها مجوز ارسال پیام را برای همان کاربر باز می‌کند (بدون ادمین‌شدن)."""
    _peer, channel = await peer_and_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id)
    rights = clone_banned_rights(None)               # همهٔ فلگ‌ها False
    await call(
        bot.client(functions.channels.EditBannedRequest(
            channel=channel, participant=participant, banned_rights=rights)),
        logger=bot.logger,
        what=f"EXCLUSIVE GRANT chat={chat_id} user={user_id}",
    )
    log_info(bot.logger,
             f"EXCLUSIVE GRANT RPC OK chat_id={chat_id} user_id={user_id}")
    return True


async def revoke_participant_rights(bot, chat_id, user_id) -> bool:
    """مجوز ارسالِ همان کاربر را برمی‌گرداند (مثل پیش‌فرض قفلِ گروه)."""
    _peer, channel = await peer_and_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id)
    rights = clone_banned_rights(None, send_messages=True, send_plain=True)
    await call(
        bot.client(functions.channels.EditBannedRequest(
            channel=channel, participant=participant, banned_rights=rights)),
        logger=bot.logger,
        what=f"EXCLUSIVE REVOKE chat={chat_id} user={user_id}",
    )
    log_info(bot.logger,
             f"EXCLUSIVE REVOKE RPC OK chat_id={chat_id} user_id={user_id}")
    return True


async def read_participant_rights(bot, chat_id, user_id):
    """``ChatBannedRights`` واقعیِ کاربر از سرور (تأییدِ نهایی)."""
    _peer, channel = await peer_and_channel(bot, chat_id)
    participant = await user_peer(bot, chat_id, user_id)
    result = await call(
        bot.client(functions.channels.GetParticipantRequest(
            channel=channel, participant=participant)),
        logger=bot.logger,
        what=f"EXCLUSIVE READ chat={chat_id} user={user_id}",
    )
    # GetParticipantRequest خروجی‌اش خودِ participant است (نه wrapper)؛
    # هر دو شکل را تحمل می‌کنیم تا با تغییر نسخهٔ کتابخانه نشکند.
    participant_obj = getattr(result, "participant", result)
    return getattr(participant_obj, "banned_rights", None)


async def rights_confirmed(bot, chat_id, user_id, allowed) -> bool:
    """آیا API واقعاً همان چیزی را می‌گوید که ما ادعا می‌کنیم؟"""
    rights = await read_participant_rights(bot, chat_id, user_id)
    banned = text_is_banned(rights)
    return (not banned) if allowed else banned


# ---------------------------------------------------------------------------
#  فهرست «اختصاصی» — ذخیره‌سازی
# ---------------------------------------------------------------------------
def _group_record(data, chat_id, create=False):
    groups = data.setdefault("groups", {})
    key = storage.group_key(chat_id)
    if create:
        return groups.setdefault(key, {"users": {}, "pending": []})
    return groups.get(key) or {"users": {}, "pending": []}


def list_entries(chat_id, *, states=("active",)):
    """رکوردهای این گروه که در یکی از ``states`` هستند (پیش‌فرض: فعال).

    منبعِ حقیقت ``state`` است؛ کلید ``active`` تنها برای خواناییِ فایل نگه داشته
    می‌شود و هرگز جای ``state`` را نمی‌گیرد.
    """
    wanted = set(states)
    with storage.EXCLUSIVE_STORAGE._lock:
        record = _group_record(storage.EXCLUSIVE_STORAGE._data(), chat_id)
        return [dict(entry) for entry in record.get("users", {}).values()
                if entry.get("state") in wanted]


def is_exclusive(chat_id, user_id) -> bool:
    wanted = storage.user_key(user_id)
    return any(storage.user_key(item.get("user_id")) == wanted
               for item in list_entries(chat_id))


def get_entry(chat_id, user_id):
    wanted = storage.user_key(user_id)
    for entry in list_entries(chat_id):
        if storage.user_key(entry.get("user_id")) == wanted:
            return entry
    return None


def _save_entry(chat_id, entry):
    def mutate(data):
        record = _group_record(data, chat_id, create=True)
        users = record.setdefault("users", {})
        users[storage.user_key(entry["user_id"])] = entry
        # فهرست در انتظارِ لغو (برای بازیابی پس از قطعی/ری‌استارت)
        pending = [item for item in record.get("pending", [])
                   if storage.user_key(item) != storage.user_key(entry["user_id"])]
        entry["active"] = entry.get("state") == "active"
        if entry.get("state") in {"active", PENDING_FLAG}:
            pending.append(storage.user_key(entry["user_id"]))
        record["pending"] = pending
        return entry

    return storage.EXCLUSIVE_STORAGE.update(mutate)


def _set_state(chat_id, entry, state, extra=None):
    entry = dict(entry)
    entry["state"] = state
    entry["updated_at"] = storage.now()
    if extra:
        entry.update(extra)
    return _save_entry(chat_id, entry)


# ---------------------------------------------------------------------------
#  عملیات «اختصاصی» / «لغو اختصاصی»
# ---------------------------------------------------------------------------
class NotLocked(RuntimeError):
    """گروه قفل نیست ⇒ استثنا معنا ندارد."""


class AlreadyExclusive(RuntimeError):
    pass


class NotExclusive(RuntimeError):
    pass


async def grant(bot, chat_id, user_id, *, actor_id=None, name="", username=""):
    """ثبت کاربر در فهرست اختصاصی + اعطای مجوز ارسالِ واقعی."""
    if not await is_group_locked(bot, chat_id):
        raise NotLocked(f"group {chat_id} is not locked")

    existing = get_entry(chat_id, user_id)
    if existing and existing.get("state") == "active":
        raise AlreadyExclusive(f"user {user_id} already exclusive")

    await grant_participant_rights(bot, chat_id, user_id)
    saved = _save_entry(chat_id, {
        "user_id": storage.user_key(user_id),
        "chat_id": storage.group_key(chat_id),
        "name": name or "",
        "username": (username or "").lstrip("@"),
        "actor_id": storage.user_key(actor_id) if actor_id else None,
        "created_at": storage.now(),
        "created_at_tehran": storage.iso_tehran(),
        "updated_at": storage.now(),
        "state": "active",
        "granted": True,
        "confirmed": False,
        "last_error": "",
    })
    # تأیید واقعی با API؛ اگر تأیید نشد، rکورد فعال نمی‌ماند.
    try:
        confirmed = await rights_confirmed(bot, chat_id, user_id, True)
    except Exception as error:                       # noqa: BLE001
        _set_state(chat_id, saved, PENDING_FLAG, {"last_error": repr(error)})
        raise
    saved = _set_state(chat_id, saved,
                       "active" if confirmed else PENDING_FLAG,
                       {"confirmed": bool(confirmed)})
    log_info(bot.logger,
             f"EXCLUSIVE GRANTED chat_id={chat_id} user_id={user_id} "
             f"confirmed={confirmed}")
    return saved


async def revoke(bot, chat_id, user_id, *, reason="manual"):
    """لغو مجوز + غیرفعال کردن رکورد. اجرای چندباره ممنوع."""
    entry = get_entry(chat_id, user_id)
    if not entry:
        raise NotExclusive(f"user {user_id} is not exclusive")
    if entry.get("state") == "revoked":
        raise NotExclusive(f"user {user_id} already revoked")

    try:
        await revoke_participant_rights(bot, chat_id, user_id)
    except Exception as error:                       # noqa: BLE001
        # قطعی/خطای گذرا: رکورد در «در انتظار لغو» می‌ماند تا ریکاوریِ
        # استارتاپ دوباره تلاش کند؛ پرونده هرگز گم نمی‌شود.
        _set_state(chat_id, entry, PENDING_FLAG, {"last_error": repr(error)})
        raise
    try:
        confirmed = await rights_confirmed(bot, chat_id, user_id, False)
    except Exception as error:                       # noqa: BLE001
        _set_state(chat_id, entry, PENDING_FLAG, {"last_error": repr(error)})
        raise
    entry = _set_state(chat_id, entry,
                       "revoked" if confirmed else PENDING_FLAG,
                       {"confirmed": bool(confirmed), "revoked_at": storage.now(),
                        "revoked_at_tehran": storage.iso_tehran(),
                        "revoke_reason": reason})
    log_info(bot.logger,
             f"EXCLUSIVE REVOKED chat_id={chat_id} user_id={user_id} "
             f"reason={reason} confirmed={confirmed}")
    return entry


async def revoke_all(bot, chat_id, *, reason="group_unlocked", logger=None):
    """لغو همهٔ مجوزهای موقتِ این گروه (وقتی گروه باز شد).

    خروجی: ``(revoked, failed_entries)``. خطای هر کاربر بقیه را متوقف نمی‌کند و
    در فهرستِ بازیابی می‌نشیند.
    """
    revoked, failed = [], []
    for entry in list_entries(chat_id):
        try:
            await revoke(bot, chat_id, entry["user_id"], reason=reason)
            revoked.append(entry["user_id"])
        except Exception as error:                   # noqa: BLE001
            try:
                _set_state(chat_id, entry, PENDING_FLAG,
                           {"last_error": repr(error)})
            except Exception:                        # noqa: BLE001
                pass
            failed.append(dict(entry, last_error=repr(error)))
            if logger is not None:
                try:
                    logger.log_error(
                        "EXCLUSIVE REVOKE ALL FAILED "
                        f"chat_id={chat_id} user_id={entry.get('user_id')} "
                        f"error={error!r}")
                except Exception:                    # noqa: BLE001
                    pass
    return revoked, failed


def pending_jobs(chat_id=None):
    """کارهای در انتظارِ بازیابی (برای استارتاپ/صف اختصاصی)."""
    data = storage.EXCLUSIVE_STORAGE.load()
    jobs = []
    groups = data.get("groups", {})
    keys = [storage.group_key(chat_id)] if chat_id else list(groups.keys())
    for key in keys:
        record = groups.get(key) or {}
        for entry in record.get("users", {}).values():
            if entry.get("state") in {"active", PENDING_FLAG} and \
                    entry.get("state") != "revoked":
                jobs.append(dict(entry, group_key=key))
    return jobs


def forget_group(chat_id):
    """پاک‌سازی رکوردهای یک گروه (تست/ری‌ست)."""
    def mutate(data):
        data.setdefault("groups", {}).pop(storage.group_key(chat_id), None)

    storage.EXCLUSIVE_STORAGE.update(mutate)
