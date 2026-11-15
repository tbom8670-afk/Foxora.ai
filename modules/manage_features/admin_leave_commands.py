# -*- coding: utf-8 -*-
"""🎒 هندلرِ دستورهای «مرخصی ادمین».

* ``مرخصی``                  → راهنمای نوشتن
* ``مرخصی 30 دقیقه``         → ثبتِ مرخصیِ خودِ همان ادمین
* ``لغو مرخصی`` (ریپلای)     → فقط مالکِ ثبت‌شدهٔ گروه / مالک اصلی ربات
* ``لغو مرخصی`` (بدون ریپلای) → ادمین، مرخصیِ خودش را لغو می‌کند

مجوزها فقط با **شناسهٔ عددی** سنجیده می‌شوند و در همین تابع (نه فقط در
تشخیصِ دستور) اعمال می‌شوند.
"""
from __future__ import annotations

import asyncio

from modules import admin_tools
from modules.manage_features import access, admin_leave, peers, texts
from modules.user_display import format_user

COMMAND = "مرخصی"
CANCEL_COMMAND = "لغو مرخصی"


def _log(logger, message, error=False):
    admin_leave._log(logger, message, error)


def parse(text):
    """``(kind, argument)`` — kind ∈ {ask, set, cancel} یا ``(None, "")``."""
    value = admin_leave.normalize(text)
    if value == COMMAND:
        return "ask", ""
    if value == CANCEL_COMMAND:
        return "cancel", ""
    if value.startswith(COMMAND + " "):
        return "set", value[len(COMMAND) + 1:].strip()
    return None, ""


def is_command(text) -> bool:
    # مسیرِ گرم: اول یک بررسیِ ارزان روی متنِ خام.
    if not text or COMMAND not in str(text)[:24]:
        return False
    return parse(text)[0] is not None


# ---------------------------------------------------------------------------
#  مجوز
# ---------------------------------------------------------------------------
def is_any_owner(chat_id, user_id) -> bool:
    """مالک اصلی ربات یا مالکِ ثبت‌شدهٔ همین گروه (عددی)."""
    if user_id is None:
        return False
    return bool(admin_tools.is_global_owner(user_id)
                or access.is_group_owner(chat_id, user_id))


async def can_take_leave(chat_id, user_id, username=None, native_admin=None):
    """ادمینِ ثبت‌شدهٔ روباه یا ادمینِ واقعیِ گروه؛ مالک‌ها هرگز."""
    if user_id is None or is_any_owner(chat_id, user_id):
        return False
    try:
        if admin_tools.is_admin(chat_id, user_id, username):
            return True
    except Exception:                                # noqa: BLE001
        pass
    if native_admin is None:
        return False
    try:
        return bool(await native_admin())
    except Exception:                                # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
#  ارسال
# ---------------------------------------------------------------------------
async def _reply(event, payload, logger=None):
    text, entities = payload
    try:
        return await event.reply(text, formatting_entities=entities)
    except Exception as error:                       # noqa: BLE001
        _log(logger, f"ADMIN LEAVE REPLY FAILED error={error!r}", error=True)
        return None


def _peer_for(bot, chat_id, stored):
    if isinstance(stored, dict):
        try:
            from splusthon.tl import types
            return types.InputPeerChannel(int(stored["channel_id"]),
                                          int(stored["access_hash"]))
        except Exception:                            # noqa: BLE001
            pass
    return peers.group_peer(bot, chat_id)


async def _send(bot, chat_id, payload, stored_peer, logger=None):
    text, entities = payload
    try:
        return await asyncio.wait_for(
            bot.client.send_message(_peer_for(bot, chat_id, stored_peer),
                                    text, formatting_entities=entities),
            timeout=admin_leave.RPC_TIMEOUT)
    except Exception as error:                       # noqa: BLE001
        _log(logger, f"ADMIN LEAVE SEND FAILED chat_id={chat_id} "
                     f"error={error!r}", error=True)
        return None


def mention(leave) -> str:
    username = str((leave or {}).get("username") or "").lstrip("@")
    if username:
        return "@" + username
    return str((leave or {}).get("display") or "").strip() or "ادمین"


async def _bot_username(bot):
    cached = getattr(bot, "_leave_bot_username", None)
    if cached is not None:
        return cached
    value = ""
    try:
        me = getattr(bot, "me", None) or await bot.client.get_me()
        username = getattr(me, "username", "") or ""
        value = ("@" + username) if username else ""
    except Exception:                                # noqa: BLE001
        return ""
    try:
        bot._leave_bot_username = value
    except Exception:                                # noqa: BLE001
        pass
    return value


def _serialize_peer(peer):
    channel_id = getattr(peer, "channel_id", None)
    access_hash = getattr(peer, "access_hash", None)
    if channel_id is None or access_hash is None:
        return None
    return {"channel_id": int(channel_id), "access_hash": int(access_hash)}


# ---------------------------------------------------------------------------
#  زمان‌بند (پیام‌های خودکار)
# ---------------------------------------------------------------------------
def _callbacks(bot, logger):
    async def on_reminder(chat_id, leave, peer):
        await _send(bot, chat_id, texts.leave_reminder(mention(leave)),
                    peer, logger)

    async def on_end(chat_id, leave, peer, last):
        await _send(bot, chat_id, texts.leave_ended(mention(leave)),
                    peer, logger)

    return on_reminder, on_end


def ensure_scheduler(bot, logger=None):
    on_reminder, on_end = _callbacks(bot, logger)
    return admin_leave.ensure_watcher(on_reminder, on_end, logger)


def recover(bot, logger=None):
    """بعد از ری‌استارت: مرخصی‌های فعال از دیسک؛ گذشته‌ها در اولین دور پاک."""
    admin_leave._rebuild_index()
    data = admin_leave.STORAGE.load()
    groups = data.get("groups") or {}
    leaves = sum(len(group.get("leaves") or {}) for group in groups.values())
    if leaves:
        ensure_scheduler(bot, logger)
    return {"groups": len(groups), "leaves": leaves}


# ---------------------------------------------------------------------------
#  دستورها
# ---------------------------------------------------------------------------
async def handle(bot, event, chat_id, user_id, sender, text, logger=None,
                 chat=None, native_admin=None):
    """``True`` = پیام مصرف شد."""
    kind, argument = parse(text)
    if kind is None or user_id is None:
        return False
    username = getattr(sender, "username", None)

    if kind == "cancel":
        return await _handle_cancel(bot, event, chat_id, user_id, logger)

    if not await can_take_leave(chat_id, user_id, username, native_admin):
        _log(logger, f"ADMIN LEAVE DENIED chat_id={chat_id} user_id={user_id} "
                     f"owner={is_any_owner(chat_id, user_id)}")
        # مالک/کاربر عادی: دستور بی‌اثر است (قالبِ جدیدی ساخته نمی‌شود).
        return True

    seconds = admin_leave.parse_duration(argument) if kind == "set" else None
    if seconds is None:
        await _reply(event, texts.leave_ask(), logger)
        return True

    peer = None
    try:
        cached = getattr(bot, "reply_input_peer_cache", None) or {}
        peer = _serialize_peer(cached.get(chat_id))
        if peer is None:
            # همان روشِ «پیام زماندار»: peerِ گروه وقتی همه‌چیز گرم است گرفته
            # و ذخیره می‌شود تا پیام‌های خودکار بعد از ری‌استارت هم برسند.
            peer = _serialize_peer(
                await bot.client.get_input_entity(chat_id))
    except Exception:                                # noqa: BLE001
        peer = None
    display = format_user(sender, fallback="ادمین")
    leave, first = admin_leave.start(
        chat_id, user_id, seconds, username=username or "",
        display=display, peer=peer)
    _log(logger, f"ADMIN LEAVE SET chat_id={chat_id} admin_id={user_id} "
                 f"seconds={seconds} first={first} end_ts={leave['end_ts']}")
    ensure_scheduler(bot, logger)
    await _reply(event, texts.leave_set(
        duration=admin_leave.duration_label(seconds),
        admin=mention(leave),
        substitute=await _bot_username(bot)), logger)
    return True


async def _handle_cancel(bot, event, chat_id, user_id, logger):
    replied = getattr(event, "reply_to", None) is not None or \
        getattr(event, "is_reply", False)
    owner = is_any_owner(chat_id, user_id)
    target_id = None
    if replied:
        if not owner:
            # ادمین فقط مرخصیِ خودش را (بدون ریپلای) لغو می‌کند.
            own = admin_leave.get_leave(chat_id, user_id)
            if not own:
                return True
            target = await _reply_target(bot, event, chat_id)
            if target is None or str(target) != str(user_id):
                _log(logger, f"ADMIN LEAVE CANCEL DENIED chat_id={chat_id} "
                             f"user_id={user_id}")
                return True
            target_id = user_id
        else:
            target_id = await _reply_target(bot, event, chat_id)
    elif not owner:
        target_id = user_id
    if target_id is None:
        return True
    leave, last, restored = admin_leave.end(chat_id, target_id)
    if not leave:
        return True
    _log(logger, f"ADMIN LEAVE CANCELLED chat_id={chat_id} "
                 f"admin_id={target_id} by={user_id} last={last} "
                 f"restored={restored}")
    await _reply(event, texts.leave_cancelled(), logger)
    return True


async def _reply_target(bot, event, chat_id):
    """شناسهٔ عددیِ فرستندهٔ پیامِ ریپلای‌شده (یا ``None``)."""
    try:
        from modules.manage_features import violator_commands
        found = await violator_commands._reply_target(bot, event, chat_id)
    except Exception:                                # noqa: BLE001
        return None
    if not found:
        return None
    identifier = found[1]
    try:
        return int(identifier)
    except (TypeError, ValueError):
        return None


def reset_all():
    admin_leave.reset_all()
