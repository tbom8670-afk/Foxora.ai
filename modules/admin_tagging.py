# -*- coding: utf-8 -*-
"""Tag the union of this group's native and bot-registered admins, once each.

This is deliberately read-only: admins.json and native administrator rights are
never changed.  Fetch the filtered native-admin list (not the recent-members
list) with a short deadline, then send one real Soroush mention per person.
"""
from __future__ import annotations

import asyncio
import re
import time

from modules.admin_display import entry_fields
from modules.admin_storage import is_admin, load_admins
from modules.group_id import normalize_group_id
from modules.group_storage import get_group_owner, is_active
from modules.manage_features.peers import group_peer
from modules.self_admin_shield import is_self_target

try:
    from splusthon import utils
    from splusthon.tl import functions, types
except ImportError:  # The command fails closed on an incomplete installation.
    utils = functions = types = None


COMMAND = "تگ ادمین ها"
# Each target gets just ONE of the screenshot's five separate greeting lines.
GREETINGS = (
    ("سلام ", " کجای بیا بالا 🎃"),
    ("سلام ", " بیا حرف بزن یکم 😽"),
    ("سلام ", " دلم تنگ شده نمیای ؟ ☹️"),
    ("سلام ", " حیف ادمین که توی 😏"),
    ("سلام ", " خبری ازت نیست 🦊"),
)
READ_TIMEOUT = 6.0
RESOLVE_TIMEOUT = 2.0
SEND_TIMEOUT = 8.0
SEND_BUDGET = 20.0  # Do not keep the admin command lane busy indefinitely.
MAX_NATIVE_PAGES = 2
PAGE_SIZE = 200
MAX_TAGS = 60
SEND_CONCURRENCY = 4
COOLDOWN = 45.0
_USERNAME = re.compile(r"[a-zA-Z0-9_]{3,32}\Z")
_NATIVE_ROLES = frozenset((
    "ChannelParticipantAdmin", "ChannelParticipantCreator",
    "ChatParticipantAdmin", "ChatParticipantCreator",
))
_BUSY = set()
_LAST_SENT = {}


def reset_memory():
    """Clear only the short-lived per-group anti-flood state (also for tests)."""
    _BUSY.clear()
    _LAST_SENT.clear()


def _u16(text):
    return len(text.encode("utf-16-le")) // 2


def _username(value):
    value = str(value or "").strip().lstrip("@")
    return value if _USERNAME.fullmatch(value) else ""


def _name(value):
    return " ".join(str(value or "").split())[:64]


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _authorized(chat_id, actor_id, sender):
    actor_id = _positive_id(actor_id)
    if actor_id is None:
        return False
    try:
        owner = _positive_id(get_group_owner(chat_id))
        return (owner == actor_id
                or is_admin(chat_id, actor_id, getattr(sender, "username", None)))
    except Exception:  # Corrupt/unavailable authority data must not grant access.
        return False


def _remember(rows, seen_ids, seen_names, *, user_id=None, username="",
              name="", entity=None, bot=None):
    """Deduplicate across both lists by stable ID, or legacy @username."""
    user_id = _positive_id(user_id)
    username = _username(username)
    key = username.lower()
    if user_id is None and not username:
        return
    if user_id is not None and is_self_target(user_id, bot=bot):
        if key:
            seen_names.add(key)  # Also discard a legacy @bot-name record.
        return
    # An old username may now belong to another ID: never drop a distinct
    # ID-based record just because its stored @name matches someone else's.
    if user_id is not None and user_id in seen_ids:
        return
    if user_id is None and key in seen_names:
        return
    if user_id is not None:
        seen_ids.add(user_id)
    if key:
        seen_names.add(key)
    rows.append((user_id, username, _name(name), entity))


async def _native_admins(bot, chat_id):
    """Fetch native roles without scanning a supergroup's recent members.

    For a supergroup, hash=0 forces a fresh list, and the role's own user_id
    is matched to page.users (the two arrays need not be in the same order).
    A count larger than our page budget is marked incomplete, not silently
    interpreted as proof that remaining admins do not exist.
    """
    async def read():
        peer = await bot.client.get_input_entity(group_peer(bot, chat_id))
        if getattr(peer, "channel_id", None) is None:
            users = await bot.client.get_participants(
                peer, filter=types.ChannelParticipantsAdmins(), limit=PAGE_SIZE)
            rows = [user for user in users
                    if type(getattr(user, "participant", None)).__name__ in _NATIVE_ROLES]
            return rows, len(users) < PAGE_SIZE

        channel = utils.get_input_channel(peer)
        found, seen = [], set()
        offset, missing_users = 0, False
        for _page in range(MAX_NATIVE_PAGES):
            response = await bot.client(functions.channels.GetParticipantsRequest(
                channel=channel, filter=types.ChannelParticipantsAdmins(),
                offset=offset, limit=PAGE_SIZE, hash=0))
            roles = getattr(response, "participants", None)
            count = getattr(response, "count", None)
            if roles is None or not isinstance(count, int) or count < 0:
                return found, False
            users = {uid: user for user in (getattr(response, "users", ()) or ())
                     if (uid := _positive_id(getattr(user, "id", None))) is not None}
            for role in roles:
                if type(role).__name__ not in _NATIVE_ROLES:
                    missing_users = True  # Unexpected role in an admin-only list.
                    continue
                uid = _positive_id(getattr(role, "user_id", None))
                if uid is None or uid not in users:
                    missing_users = True
                elif uid not in seen:
                    seen.add(uid)
                    found.append(users[uid])
                else:
                    missing_users = True  # A repeated page is not a complete read.
            offset += len(roles)
            if offset >= count:
                return found, not missing_users
            if not roles:  # A premature empty page must not be retried forever.
                return found, False
        return found, False

    return await asyncio.wait_for(read(), timeout=READ_TIMEOUT)


async def _input_user(bot, user_id, entity, username):
    """Resolve a *specific* ID for name mentions; never trust a stale @name."""
    if types is None or utils is None:
        return None
    if isinstance(entity, types.User) and getattr(entity, "access_hash", None):
        input_user = utils.get_input_user(entity)
    else:
        try:
            peer = await asyncio.wait_for(
                bot.client.get_input_entity(entity if entity is not None else user_id),
                timeout=RESOLVE_TIMEOUT)
            input_user = utils.get_input_user(peer)
        except Exception:
            input_user = None
    if input_user is not None and _positive_id(getattr(input_user, "user_id", None)) != user_id:
        input_user = None
    if input_user is None and username:
        # A registered ID absent from the client's cache may still resolve by
        # username. Verify the ID before mentioning it: usernames can change.
        try:
            fresh = await asyncio.wait_for(bot.client.get_entity("@" + username),
                                           timeout=RESOLVE_TIMEOUT)
            if _positive_id(getattr(fresh, "id", None)) == user_id:
                input_user = utils.get_input_user(fresh)
        except Exception:
            pass
    if input_user is None or _positive_id(getattr(input_user, "user_id", None)) != user_id:
        return None
    return input_user


async def _mention(bot, row):
    uid, saved_username, saved_name, entity = row
    username = _username(getattr(entity, "username", None)) if entity else ""
    if entity is not None and username:
        # Username came from the freshly filtered native-admin response.
        return "@" + username, types.MessageEntityMention
    if uid is None:
        # Legacy admins.json records contain only a username, not a user ID.
        return ("@" + saved_username, types.MessageEntityMention) if saved_username else None
    input_user = await _input_user(bot, uid, entity, saved_username)
    if input_user is None:
        return None
    native_name = " ".join(str(part) for part in (
        getattr(entity, "first_name", None), getattr(entity, "last_name", None))
        if part)
    label = "@" + saved_username if saved_username else (
        _name(native_name) or saved_name or "ادمین")
    return label, lambda offset, length: types.InputMessageEntityMentionName(
        offset=offset, length=length, user_id=input_user)


async def handle(bot, event, chat_id, actor_id, sender, text, *, edited=False):
    """Return True only for this command; no network work for other messages."""
    normalized = str(text or "").replace("\u200c", " ").replace("ي", "ی")
    normalized = " ".join(normalized.split())
    if normalized != COMMAND or getattr(event, "is_private", False):
        return False
    if edited:
        return True  # An edit must never fan out another round of mentions.
    if not _authorized(chat_id, actor_id, sender):
        await event.reply("⛔ فقط مالک ثبت‌شده یا ادمین ثبت‌شدهٔ این گروه می‌تواند ادمین‌ها را تگ کند.")
        return True
    if not is_active(chat_id):
        await event.reply("❌ این دستور فقط در گروه فعال قابل استفاده است.")
        return True
    if types is None or functions is None:
        await event.reply("❌ کتابخانهٔ سروش‌پلاس برای تگ‌کردن در دسترس نیست.")
        return True

    key = normalize_group_id(chat_id)
    now = time.monotonic()
    if key in _BUSY:
        await event.reply("⏳ تگ‌کردن ادمین‌های این گروه در حال انجام است.")
        return True
    if now - _LAST_SENT.get(key, -float("inf")) < COOLDOWN:
        await event.reply("⏳ لطفاً کمی بعد دوباره ادمین‌ها را تگ کنید.")
        return True
    _BUSY.add(key)
    try:
        # Registered IDs are stored per group. Native roles are fetched by the
        # admin filter, not by a potentially truncated recent-members list.
        data = load_admins()
        stored = data.get(key, []) if isinstance(data, dict) else []
        entries = list(stored) if isinstance(stored, list) else []
        try:
            natives, native_complete = await _native_admins(bot, chat_id)
        except Exception as exc:
            natives, native_complete = [], False
            try:
                bot.logger.log_error(f"ADMIN TAG native list failed group={key}: {exc!r}")
            except Exception:
                pass
        rows, seen_ids, seen_names = [], set(), set()
        for user in natives:
            _remember(rows, seen_ids, seen_names,
                      user_id=getattr(user, "id", None),
                      username=getattr(user, "username", None),
                      name=getattr(user, "first_name", None),
                      entity=user, bot=bot)
        for entry in entries:
            uid, username, name = entry_fields(entry)
            _remember(rows, seen_ids, seen_names, user_id=uid, username=username,
                      name=name, bot=bot)
        if not rows:
            await event.reply("🦊 ادمینی برای تگ پیدا نشد." if native_complete else
                              "⚠️ فهرست ادمین‌های گروه در دسترس نیست و ادمین ثبت‌شده‌ای پیدا نشد.")
            return True

        omitted = max(0, len(rows) - MAX_TAGS)
        rows = rows[:MAX_TAGS]
        semaphore = asyncio.Semaphore(SEND_CONCURRENCY)

        async def send_one(index, row):
            async with semaphore:
                mention = await _mention(bot, row)
                if mention is None:
                    return False
                label, make_entity = mention
                prefix, suffix = GREETINGS[index % len(GREETINGS)]
                message = prefix + label + suffix
                entity = make_entity(offset=_u16(prefix), length=_u16(label))
                try:
                    await asyncio.wait_for(
                        event.respond(message, formatting_entities=[entity]),
                        timeout=SEND_TIMEOUT)
                    return True
                except Exception as exc:
                    try:
                        bot.logger.log_error(f"ADMIN TAG send failed group={key}: {exc!r}")
                    except Exception:
                        pass
                    return False

        tasks = [asyncio.create_task(send_one(i, row))
                 for i, row in enumerate(rows)]
        try:
            done, pending = await asyncio.wait(tasks, timeout=SEND_BUDGET)
        finally:
            # Cancellation of the parent handler must not leave detached
            # outgoing sends running after the per-group guard is released.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        sent = sum(task.result() for task in done
                   if not task.cancelled() and task.exception() is None)
        if sent:
            _LAST_SENT[key] = time.monotonic()
            if len(_LAST_SENT) > 1024:
                _LAST_SENT.pop(next(iter(_LAST_SENT)))
        if sent < len(rows) or omitted or not native_complete:
            reason = (" دریافت فهرست ادمین‌های گروه کامل نبود." if not native_complete
                      else "")
            await event.reply(
                f"⚠️ {sent} نفر از {len(rows) + omitted} ادمین تگ شدند."
                + (f" سقف هر نوبت {MAX_TAGS} نفر است." if omitted else "")
                + (" زمان ارسال به پایان رسید." if pending else "") + reason)
        return True
    finally:
        _BUSY.discard(key)
