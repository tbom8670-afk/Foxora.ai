"""Persistent public background gallery; completely independent of Plus500.

Only the global owner can register a numbered photo in a group. Every user
receives each number at most once, across all groups. A photo is downloaded to
the bot's private runtime directory, so source-message deletion and restarts
do not break the gallery. Captions are always sent WITH the photo, never alone.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import tempfile
import time

from modules import runtime_db
from modules.owner_check import is_global_owner
from modules.runtime_paths import DATA_DIR

COMMAND = "پس زمینه"
SETUP = "تنظیم پس زمینه"
CANCEL = "لغو پس زمینه"
PHOTOS = "background_gallery_v1"
SEEN = "background_seen_v1"
TIMEOUT = 900
MAX_PHOTO_BYTES = 20 * 1024 * 1024
_GALLERY_DIR = DATA_DIR / "background_gallery"
_sessions = {}  # (chat_id, owner_id) -> monotonic timestamp
_user_locks = {}
_catalog_lock = None
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CAPTION_NUMBER = re.compile(r"^\s*عکس\s*شماره\s*([0-9۰-۹٠-٩]+)(?=\s|$)")


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


def _path(number):
    return _GALLERY_DIR / f"{number}.jpg"


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


async def _download_photo(bot, message, number):
    _GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{number}-", suffix=".jpg", dir=_GALLERY_DIR)
    os.close(fd)
    temp = Path(temp_name)
    try:
        result = await bot.client.download_media(message, file=str(temp))
        if isinstance(result, bytes):
            temp.write_bytes(result)
        elif isinstance(result, (str, os.PathLike)) and Path(result) != temp:
            source = Path(result)
            if not source.is_file():
                raise ValueError("download returned a missing photo")
            temp.write_bytes(source.read_bytes())
        size = temp.stat().st_size
        if size == 0 or size > MAX_PHOTO_BYTES:
            raise ValueError("photo is empty or exceeds 20 MB")
        os.replace(temp, _path(number))
    finally:
        temp.unlink(missing_ok=True)


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
                await _download_photo(bot, message, number)
                runtime_db.kv_set(PHOTOS, str(number), {"caption": caption})
            except Exception as error:
                _path(number).unlink(missing_ok=True)
                bot.logger.log_error(f"BACKGROUND SAVE FAILED number={number} error={error!r}")
                await event.reply("❌ عکس ذخیره نشد؛ دوباره ارسال کنید.")
                return True
        _sessions.pop(_session_key(chat_id, user_id), None)
        await event.reply(f"✅ عکس شماره {number} با کپشن در مجموعهٔ پس‌زمینه ثبت شد.")
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
        photo_file = _path(number)
        if not photo_file.is_file():
            bot.logger.log_error(f"BACKGROUND PHOTO MISSING number={number}")
            await event.reply("❌ فایل پس‌زمینه در دسترس نیست؛ به مالک ربات اطلاع دهید.")
            return True
        # Claim durably BEFORE the network RPC: if the process crashes after
        # delivery, this user must not receive the same number again.
        runtime_db.kv_set(SEEN, str(user_id), sorted(seen | {number}))
        try:
            await bot.client.send_file(
                chat_id, str(photo_file), caption=entry.get("caption", ""),
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
