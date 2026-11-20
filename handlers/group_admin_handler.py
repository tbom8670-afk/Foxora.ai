# -*- coding: utf-8 -*-
"""Reply-only group-native-admin commands (no bot-admin storage).

The only bridge to the large existing message pipeline is ``handle``.  The
short-lived workflow lives here, scoped by (group, registered owner); every
step rechecks groups.json.  A title and a fully explicit six-right choice are
required before any native EditAdminRequest.  Interactive text is isolated in
modules/group_admin_texts.py for the remaining reference screenshots.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import time
from weakref import WeakValueDictionary

from modules import group_admin_manager as native, group_admin_texts as texts
from modules.group_id import normalize_group_id
from modules.self_admin_shield import is_self_target
from modules.user_display import format_user


PROMOTE_COMMAND = "ادمین گروه"
DEMOTE_COMMAND = "لغو ادمین گروه"
CONFIRM = "تایید ادمین گروه"
CANCEL = "انصراف ادمین گروه"
FLOW_TTL = 300.0
MAX_FLOWS = 256


@dataclass
class Flow:
    target: object
    target_id: int
    label: str
    phase: str = "rank"
    rank: str = ""
    choices: dict = field(default_factory=lambda: dict(native.DEFAULT_CHOICES))
    updated: float = field(default_factory=time.monotonic)


_FLOWS = {}
_TARGET_LOCKS = WeakValueDictionary()
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_SELECTION = re.compile(r"[1-6](?:(?:\s*(?:و|,|،|/|-|\s)\s*)[1-6])*")


def normalize(text):
    value = str(text or "").replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\ufeff", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return " ".join(value.split())


def _key(chat_id, user_id):
    return normalize_group_id(chat_id), str(user_id)


def _flow(chat_id, user_id):
    key = _key(chat_id, user_id)
    value = _FLOWS.get(key)
    if value is not None and value.phase != "busy" \
            and time.monotonic() - value.updated > FLOW_TTL:
        _FLOWS.pop(key, None)
        return None
    return value


def is_command(text):
    return normalize(text) in {PROMOTE_COMMAND, DEMOTE_COMMAND}


def is_awaiting(chat_id, user_id):
    """Cheap, no-RPC dispatch interest for the registered owner's input."""
    # Also route the FIRST message after ownership is revoked so the handler
    # can clear the stale flow and explain why it is no longer authorized.
    return chat_id is not None and user_id is not None \
        and _flow(chat_id, user_id) is not None


def _lock(chat_id, target_id):
    key = (normalize_group_id(chat_id), int(target_id))
    lock = _TARGET_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _TARGET_LOCKS[key] = lock
    return lock


def _reply_id(event):
    message = getattr(event, "message", None)
    for reply in (getattr(message, "reply_to", None), getattr(event, "reply_to", None)):
        mid = getattr(reply, "reply_to_msg_id", None)
        if mid is not None:
            return mid
    return (getattr(message, "reply_to_msg_id", None)
            or getattr(event, "reply_to_msg_id", None))


async def _reply(event, payload):
    text, entities = payload
    return await event.reply(text, formatting_entities=list(entities))


def _log(bot, message):
    logger = getattr(bot, "logger", None)
    if logger is not None:
        try:
            logger.log_error(message)
        except Exception:
            pass


async def _target(bot, event, chat_id):
    mid = _reply_id(event)
    if not mid:
        raise native.GroupAdminError("❌ روی پیام کاربر موردنظر ریپلای کنید.")
    try:
        # Shared group-ID resolver: a positive short channel ID must NEVER be
        # passed straight to SPlusthon (it would be resolved as a user).
        from modules.manage_features.peers import group_peer
        reply = await bot.client.get_messages(group_peer(bot, chat_id), ids=mid)
        if isinstance(reply, (list, tuple)):
            reply = reply[0] if reply else None
        target = await reply.get_sender() if reply is not None else None
    except Exception as exc:
        raise native.GroupAdminError("❌ پیام ریپلای‌شده یا فرستندهٔ آن پیدا نشد.") from exc
    if target is None:
        raise native.GroupAdminError("❌ فرستندهٔ پیام ریپلای‌شده قابل شناسایی نیست.")
    try:
        target_id = int(getattr(target, "id", getattr(target, "user_id", None)))
    except (ValueError, TypeError) as exc:
        raise native.GroupAdminError("❌ روی پیام یک کاربر واقعی ریپلای کنید.") from exc
    if target_id <= 0 or is_self_target(target, bot=bot):
        raise native.GroupAdminError("❌ نمی‌توان ادمینی حساب ربات را تغییر داد.")
    return target, target_id, format_user(target, fallback="کاربر بدون نام")


def _numbers(text):
    translated = normalize(text).translate(_DIGITS)
    enabled = False
    if translated.startswith("روشن "):
        enabled, translated = True, translated[5:].strip()
    elif translated.startswith("خاموش "):
        translated = translated[6:].strip()
    if not _SELECTION.fullmatch(translated):
        return None
    numbers = [int(item) for item in re.findall(r"[0-9]+", translated)]
    return sorted(set(numbers)), enabled


def _other_project_command(cleaned):
    """A pending title/selection must not steal an existing bot command."""
    if cleaned in {CONFIRM, CANCEL, "هیچکدام", "هیچ کدام"}:
        return False
    from modules import group_dispatch
    return (cleaned in group_dispatch._ADMIN_EXACT
            or cleaned in group_dispatch._COMMAND_EXACT
            or cleaned.startswith(group_dispatch._ADMIN_PREFIXES)
            or cleaned.startswith(group_dispatch._COMMAND_PREFIXES_MANAGE))


async def _start(bot, event, chat_id, actor_id):
    native.require_owner(chat_id, actor_id)
    native.build_rights(dict(native.DEFAULT_CHOICES))  # fail closed if API absent
    current = _flow(chat_id, actor_id)
    if current is not None and current.phase == "busy":
        raise native.GroupAdminError("⏳ عملیات قبلی هنوز در حال انجام است؛ کمی صبر کنید.")
    target, target_id, label = await _target(bot, event, chat_id)
    key = _key(chat_id, actor_id)
    state = Flow(target=target, target_id=target_id, label=label)
    _FLOWS[key] = state
    if len(_FLOWS) > MAX_FLOWS:
        for old_key, old_state in list(_FLOWS.items()):
            if (old_state is not state and old_state.phase != "busy"
                    and time.monotonic() - old_state.updated > FLOW_TTL):
                _FLOWS.pop(old_key, None)
        while len(_FLOWS) > MAX_FLOWS:
            removable = next(
                (old_key for old_key, old_state in _FLOWS.items()
                 if old_state is not state and old_state.phase != "busy"),
                None,
            )
            if removable is None:
                # In-flight edits must never be discarded to make room for a
                # new prompt. Refuse this workflow rather than leaving a
                # prompt with no pending state or exceeding the memory cap.
                _FLOWS.pop(key, None)
                raise native.GroupAdminError(
                    "⏳ درخواست‌های ادمین گروه هنوز در حال انجام‌اند؛ "
                    "کمی بعد دوباره تلاش کنید."
                )
            _FLOWS.pop(removable, None)
    try:
        await _reply(event, texts.title_prompt())
    except BaseException:
        if _FLOWS.get(key) is state:
            _FLOWS.pop(key, None)
        raise


async def _demote(bot, event, chat_id, actor_id):
    native.require_owner(chat_id, actor_id)
    current = _flow(chat_id, actor_id)
    if current is not None and current.phase == "busy":
        raise native.GroupAdminError("⏳ عملیات قبلی هنوز در حال انجام است؛ کمی صبر کنید.")
    target, target_id, label = await _target(bot, event, chat_id)
    if current is not None:
        _FLOWS.pop(_key(chat_id, actor_id), None)
    async with _lock(chat_id, target_id):
        await native.demote(bot, chat_id, actor_id, target)
    await _reply(event, texts.demoted(label))


async def _continue(bot, event, chat_id, actor_id, raw, state):
    cleaned = normalize(raw)
    if state.phase == "busy":
        await _reply(event, texts.bold("⏳ ثبت ادمینی در حال بررسی است؛ کمی صبر کنید."))
        return
    if cleaned == CANCEL:
        _FLOWS.pop(_key(chat_id, actor_id), None)
        await _reply(event, texts.bold("✅ درخواست ادمین گروه لغو شد؛ تغییری اعمال نشد."))
        return
    if state.phase == "rank":
        try:
            state.rank = native.validate_rank(raw)
        except native.GroupAdminError as exc:
            await _reply(event, texts.bold(str(exc)))
            return
        state.phase = "rights"
        state.updated = time.monotonic()
        try:
            await _reply(event, texts.rights_board(state.label, state.choices))
        except BaseException:
            if _FLOWS.get(_key(chat_id, actor_id)) is state:
                _FLOWS.pop(_key(chat_id, actor_id), None)
            raise
        return
    if cleaned == CONFIRM:
        if not any(state.choices.values()):
            await _reply(event, texts.bold(
                "❌ برای ادمین شدن حداقل یکی از شش دسترسی باید روشن باشد."))
            return
        state.phase = "busy"
        try:
            # This locks only this group/target; never admins.json, other
            # groups, or the existing bot administration paths.
            async with _lock(chat_id, state.target_id):
                await native.promote(bot, chat_id, actor_id, state.target,
                                     state.rank, dict(state.choices))
        finally:
            if _FLOWS.get(_key(chat_id, actor_id)) is state:
                _FLOWS.pop(_key(chat_id, actor_id), None)
        await _reply(event, texts.promoted(state.label, state.rank))
        return
    if cleaned in {"هیچکدام", "هیچ کدام"}:
        state.updated = time.monotonic()
        await _reply(event, texts.rights_board(state.label, state.choices))
        return
    selection = _numbers(cleaned)
    if selection is None:
        await _reply(event, texts.bold(
            "❌ شماره‌های ۱ تا ۶ را بنویسید؛ برای روشن‌کردن «روشن ۶»، "
            "و برای ثبت «تایید ادمین گروه» را بفرستید."))
        return
    numbers, enabled = selection
    for number in numbers:
        state.choices[number] = enabled
    state.updated = time.monotonic()
    try:
        await _reply(event, texts.rights_changed(numbers, enabled))
        await _reply(event, texts.rights_board(state.label, state.choices))
    except BaseException:
        if _FLOWS.get(_key(chat_id, actor_id)) is state:
            _FLOWS.pop(_key(chat_id, actor_id), None)
        raise


async def handle(bot, event, chat_id, actor_id, sender, raw, logger=None):
    """True if this independent command/workflow has consumed the message."""
    command = normalize(raw)
    state = _flow(chat_id, actor_id) if chat_id is not None and actor_id is not None else None
    if not is_command(command) and state is None:
        return False
    # Prevent a MessageEdited event from turning old text into a new grant or
    # from confirming a pending choice twice.
    if (getattr(event, "is_edit", False)
            or getattr(event, "_is_edited_message", False)
            or getattr(getattr(event, "message", None), "edit_date", None) is not None):
        return bool(is_command(command) or state is not None)
    if getattr(event, "is_private", False) or getattr(event, "is_group", True) is False:
        if is_command(command):
            await _reply(event, texts.bold("❌ این دستور فقط در گروه قابل استفاده است."))
            return True
        return False
    if not native.is_registered_group_owner(chat_id, actor_id):
        if state is not None:
            _FLOWS.pop(_key(chat_id, actor_id), None)
        await _reply(event, texts.bold(
            "⛔ فقط مالک ثبت‌شدهٔ همین گروه می‌تواند این دستور را اجرا کند."))
        return True
    if not is_command(command) and _other_project_command(command):
        return False
    try:
        if command == PROMOTE_COMMAND:
            await _start(bot, event, chat_id, actor_id)
        elif command == DEMOTE_COMMAND:
            await _demote(bot, event, chat_id, actor_id)
        else:
            await _continue(bot, event, chat_id, actor_id, raw, state)
    except native.GroupAdminError as exc:
        _log(bot, f"NATIVE GROUP ADMIN FAILED group={chat_id} actor={actor_id} "
                  f"command={command[:40]!r} cause={exc.__cause__!r} error={exc!r}")
        await _reply(event, texts.bold(str(exc)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log(bot, f"NATIVE GROUP ADMIN CRASH group={chat_id} actor={actor_id} "
                  f"command={command[:40]!r} error={exc!r}")
        await _reply(event, texts.bold(
            "❌ خطای نامنتظره در ارتباط با سروش‌پلاس؛ وضعیت ادمین‌های همین گروه را بررسی کنید."))
    return True


def reset_memory():
    """Tests only; no native-admin or bot-admin records are stored here."""
    _FLOWS.clear()
    _TARGET_LOCKS.clear()
