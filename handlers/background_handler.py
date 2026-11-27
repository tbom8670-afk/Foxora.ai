"""Persistent public background gallery; completely independent of Plus500.

Only the global owner can register a numbered photo in a group. Every user
receives each number at most once, across all groups. A photo is downloaded to
the bot's Saved Messages (or the original group message as a fallback), so restarts
do not break the gallery. Captions are always sent WITH the photo, never alone.
"""
from __future__ import annotations

import asyncio
import re
import time

from modules import runtime_db
from modules.owner_check import is_global_owner

COMMAND = "پس زمینه"
SETUP = "تنظیم پس زمینه"
CANCEL = "لغو پس زمینه"
PHOTOS = "background_gallery_v1"
SEEN = "background_seen_v1"
TIMEOUT = 900
_sessions = {}  # (chat_id, owner_id) -> monotonic timestamp
_user_locks = {}
_catalog_lock = None
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CAPTION_NUMBER = re.compile(r"^\s*(?:عکس\s*)?شماره\s*([0-9۰-۹٠-٩]+)(?=\s|$)")


def _session_key(chat_id, user_id):
    from modules.group_id import normalize_group_id
    return (str(normalize_group_id(chat_id)), str(user_id))


def _awaiting(chat_id, user_id):
    key = _session_key(chat_id, user_id)
    started = _sessions.get(key)
    if started is not None and time.monotonic() - started > TIMEOUT:
        _sessions.pop(key, None)
        return False
    return started is not None


def is_awaiting(chat_id, user_id):
    """For command-lane scheduling; unauthorized users cannot claim a session."""
    return (chat_id is not None and user_id is not None
            and is_global_owner(user_id) and _awaiting(chat_id, user_id))


def _photo(message):
    media = getattr(message, "media", None)
    return bool(getattr(message, "photo", None) is not None
                or type(media).__name__ == "MessageMediaPhoto")


def _number(caption):
    match = _CAPTION_NUMBER.match(caption or "")
    if not match:
        return None
    value = int(match.group(1).translate(_DIGITS))
    return value if 1 <= value <= 10000 else None


def _catalog_numbers():
    rows = runtime_db.query_all(
        "SELECT item_key FROM runtime_kv WHERE namespace=?", (PHOTOS,))
    return sorted(int(row[0]) for row in rows if str(row[0]).isdigit())


def _lock_for(user_id):
    # Async tasks run in a single loop; creating the lock has no await/race.
    lock = _user_locks.get(str(user_id))
    if lock is None:
        lock = _user_locks[str(user_id)] = asyncio.Lock()
    return lock


def _media(message):
    return getattr(message, "media", None) or getattr(message, "photo", None)


async def _archive_photo(bot, event, caption):
    """Reuse server-side media; no GetFile/download or file upload required.

    First try a private copy in the account's Saved Messages, so deletion of
    the owner's group post does not break a background. If that chat is not
    available, retain the original group message as the source.
    """
    source = {"chat_id": getattr(event, "chat_id", None),
              "message_id": getattr(event.message, "id", None)}
    if source["chat_id"] is None or source["message_id"] is None:
        raise ValueError("source message has no chat/message id")
    archived = False
    try:
        copy = await bot.client.send_file("me", _media(event.message), caption=caption)
        copied_id = getattr(copy, "id", None)
        if copied_id is not None:
            source = {"chat_id": "me", "message_id": copied_id}
            archived = True
    except Exception as error:
        bot.logger.log_error(f"BACKGROUND SAVED-MESSAGES COPY FAILED: {error!r}")
    return source, archived


async def _source_media(bot, ref):
    if not isinstance(ref, dict):
        return None
    chat_id, message_id = ref.get("chat_id"), ref.get("message_id")
    if chat_id is None or message_id is None:
        return None
    message = await bot.client.get_messages(chat_id, ids=message_id)
    return _media(message) if message is not None and _photo(message) else None


async def handle(bot, event, chat_id, user_id, text):
    """Return True only for this gallery's group commands / owner's setup."""
    if chat_id is None or getattr(event, "is_private", False):
        return False
    if (getattr(event, "is_edit", False)
            or getattr(event, "_is_edited_message", False)
            or getattr(getattr(event, "message", None), "edit_date", None)):
        return False
    value = " ".join(str(text or "").replace("\u200c", " ").split())
    if user_id is None:
        sender = await event.get_sender()
        user_id = getattr(sender, "id", None)
    owner = user_id is not None and is_global_owner(user_id)
    if owner and value == SETUP:
        _sessions[_session_key(chat_id, user_id)] = time.monotonic()
        await event.reply("🖼 عکس را با کپشنِ شماره‌دار، مثلاً «عکس شماره 3»، در همین گروه ارسال کنید.")
        return True
    if owner and value == CANCEL and _awaiting(chat_id, user_id):
        _sessions.pop(_session_key(chat_id, user_id), None)
        await event.reply("✅ ثبت پس‌زمینه لغو شد.")
        return True
    if owner and _awaiting(chat_id, user_id):
        message = getattr(event, "message", None)
        caption = getattr(message, "message", None) or getattr(message, "caption", None) or ""
        if not _photo(message):
            await event.reply("❌ لطفاً یک عکس همراه کپشن «عکس شماره 3» بفرستید.")
            return True
        number = _number(caption)
        if number is None:
            await event.reply("❌ شماره را در ابتدای کپشن بنویسید؛ نمونه: عکس شماره 3")
            return True
        global _catalog_lock
        if _catalog_lock is None:
            _catalog_lock = asyncio.Lock()
        async with _catalog_lock:
            if runtime_db.kv_get(PHOTOS, str(number)) is not None:
                await event.reply(f"❌ عکس شماره {number} قبلاً ثبت شده؛ شمارهٔ دیگری انتخاب کنید.")
                return True
            try:
                ref, archived = await _archive_photo(bot, event, caption)
                runtime_db.kv_set(PHOTOS, str(number),
                                  {"caption": caption, "source": ref})
            except Exception as error:
                bot.logger.log_error(f"BACKGROUND SAVE FAILED number={number} error={error!r}")
                await event.reply("❌ عکس ثبت نشد؛ دوباره ارسال کنید.")
                return True
        _sessions.pop(_session_key(chat_id, user_id), None)
        await event.reply(
            f"✅ عکس شماره {number} همراه کپشن ثبت شد."
            + ("" if archived else " ⚠️ پیام اصلی عکس را حذف نکنید."))
        return True
    if value != COMMAND:
        return False
    if user_id is None:
        await event.reply("❌ کاربر شناسایی نشد؛ دوباره تلاش کنید.")
        return True
    async with _lock_for(user_id):
        numbers = _catalog_numbers()
        if not numbers:
            await event.reply("🖼 هنوز پس‌زمینه‌ای ثبت نشده است.")
            return True
        seen = runtime_db.kv_get(SEEN, str(user_id), [])
        seen = set(seen) if isinstance(seen, list) else set()
        remaining = [n for n in numbers if n not in seen]
        if not remaining:
            await event.reply("✅ همهٔ پس‌زمینه‌ها را قبلاً دریافت کرده‌اید.")
            return True
        number = remaining[0]
        entry = runtime_db.kv_get(PHOTOS, str(number)) or {}
        try:
            media = await _source_media(bot, entry.get("source"))
        except Exception as error:
            bot.logger.log_error(f"BACKGROUND PHOTO FETCH FAILED number={number} error={error!r}")
            media = None
        if media is None:
            await event.reply("❌ عکس پس‌زمینه در دسترس نیست؛ به مالک ربات اطلاع دهید.")
            return True
        # Claim durably BEFORE the network RPC: if the process crashes after
        # delivery, this user must not receive the same number again.
        runtime_db.kv_set(SEEN, str(user_id), sorted(seen | {number}))
        try:
            await bot.client.send_file(
                chat_id, media, caption=entry.get("caption", ""),
                reply_to=getattr(getattr(event, "message", None), "id", None),
            )
        except Exception as error:
            # A known failure did not deliver; allow a retry. On a crash or
            # ambiguous network outcome the durable claim remains at-most-once.
            runtime_db.kv_set(SEEN, str(user_id), sorted(seen))
            bot.logger.log_error(f"BACKGROUND SEND FAILED number={number} error={error!r}")
            await event.reply("❌ ارسال پس‌زمینه انجام نشد؛ دوباره تلاش کنید.")
            return True
        return True
