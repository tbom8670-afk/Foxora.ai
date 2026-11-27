"""Owner-configured photo attached to the first response of a group command.

The original command and its authorization stay untouched. A configured
photo replaces only the FIRST event.reply/respond with one media+caption
message. The caption is the handler's original response, with its entities.
"""
from __future__ import annotations

import time
from modules import runtime_db
from modules.group_id import normalize_group_id
from modules.owner_check import is_global_owner
from handlers import background_handler as _media_ref

NAMESPACE = "command_photo_v1"
SETUP = "تنظیم عکس "
RESET = "لغو عکس "
CANCEL = "لغو تنظیم عکس"
_SESSION_TTL = 900
_sessions = {}  # (normalized group, global owner) -> (command, monotonic)


def normalize(value):
    return " ".join(str(value or "").replace("\u200c", " ").replace("ي", "ی")
                    .replace("ك", "ک").split())


def _key(chat_id, user_id):
    return str(normalize_group_id(chat_id)), str(user_id)


def _pending(chat_id, user_id):
    key = _key(chat_id, user_id)
    item = _sessions.get(key)
    if item and time.monotonic() - item[1] > _SESSION_TTL:
        _sessions.pop(key, None)
        return None
    return item


def is_awaiting(chat_id, user_id):
    return (chat_id is not None and user_id is not None
            and is_global_owner(user_id) and _pending(chat_id, user_id) is not None)


def photo_for(command):
    return runtime_db.kv_get(NAMESPACE, normalize(command))


async def handle_setup(bot, event, chat_id, user_id, text):
    if chat_id is None or getattr(event, "is_private", False):
        return False
    if (getattr(event, "is_edit", False)
            or getattr(event, "_is_edited_message", False)
            or getattr(getattr(event, "message", None), "edit_date", None)):
        return False
    value = normalize(text)
    if user_id is None:
        sender = await event.get_sender()
        user_id = getattr(sender, "id", None)
    if user_id is None or not is_global_owner(user_id):
        return False
    key = _key(chat_id, user_id)
    if value.startswith(SETUP):
        command = value[len(SETUP):].strip()
        if len(command) < 2 or len(command) > 64 or command.startswith((SETUP, RESET)):
            await event.reply("❌ نام دستور معتبر نیست؛ مثال: تنظیم عکس فعال سازی")
            return True
        _sessions[key] = (command, time.monotonic())
        await event.reply(f"🖼 عکسِ دستور «{command}» را در همین گروه ارسال کنید؛ کپشنِ عکس نادیده گرفته می‌شود.")
        return True
    if value.startswith(RESET):
        command = value[len(RESET):].strip()
        if command:
            runtime_db.kv_delete(NAMESPACE, command)
            await event.reply(f"✅ عکس دستور «{command}» حذف شد.")
            return True
    if value == CANCEL and _pending(chat_id, user_id):
        _sessions.pop(key, None)
        await event.reply("✅ تنظیم عکس لغو شد.")
        return True
    item = _pending(chat_id, user_id)
    if item is None:
        return False
    if not _media_ref._photo(getattr(event, "message", None)):
        await event.reply("❌ لطفاً عکس را ارسال کنید، یا «لغو تنظیم عکس» بنویسید.")
        return True
    command = item[0]
    try:
        ref, archived = await _media_ref._archive_photo(bot, event, "")
        runtime_db.kv_set(NAMESPACE, command, {"source": ref})
    except Exception as error:
        bot.logger.log_error(f"COMMAND PHOTO SAVE FAILED command={command!r} error={error!r}")
        await event.reply("❌ عکس ذخیره نشد؛ دوباره ارسال کنید.")
        return True
    _sessions.pop(key, None)
    await event.reply(
        f"✅ عکس دستور «{command}» تنظیم شد."
        + ("" if archived else " ⚠️ پیام اصلی عکس را حذف نکنید."))
    return True


def _u16(value):
    return len(str(value or "").encode("utf-16-le")) // 2


class FirstPhotoReply:
    def __init__(self, bot, event, command, entry):
        self.bot, self.event, self.command, self.entry = bot, event, command, entry
        self.used = False

    async def send(self, original, args, kwargs, *, reply):
        if self.used or not args or not isinstance(args[0], str):
            return await original(*args, **kwargs)
        self.used = True
        text = args[0]
        try:
            media = await _media_ref._source_media(self.bot, self.entry.get("source"))
            if media is None:
                raise ValueError("configured photo is unavailable")
            send_kwargs = {"caption": text,
                           "formatting_entities": kwargs.get("formatting_entities") or None}
            for option in ("buttons", "silent", "parse_mode", "schedule"):
                if option in kwargs:
                    send_kwargs[option] = kwargs[option]
            if reply:
                send_kwargs["reply_to"] = getattr(getattr(self.event, "message", None), "id", None)
            return await self.bot.client.send_file(self.event.chat_id, media, **send_kwargs)
        except Exception as error:
            self.bot.logger.log_error(
                f"COMMAND PHOTO SEND FAILED command={self.command!r} error={error!r}")
            return await original(*args, **kwargs)


def install_on_event(bot, event, command):
    """Wrap just this event; no global client/handler monkeypatch or access change."""
    if getattr(event, "is_private", False) or getattr(event, "_command_photo_reply", None):
        return None
    entry = photo_for(command)
    if not isinstance(entry, dict) or not entry.get("source"):
        return None
    overlay = FirstPhotoReply(bot, event, normalize(command), entry)
    try:
        for attr in ("reply", "respond"):
            original = getattr(event, attr, None)
            if not callable(original):
                continue
            async def wrapped(*args, _original=original, _reply=(attr == "reply"), **kwargs):
                return await overlay.send(_original, args, kwargs, reply=_reply)
            setattr(event, attr, wrapped)
        event._command_photo_reply = overlay
    except (AttributeError, TypeError):
        return None
    return overlay


async def send_activation(bot, event, text, entities):
    """Fast owner activation bypasses handle_new_message, so route it here."""
    entry = photo_for("فعال سازی")
    if not isinstance(entry, dict) or not entry.get("source"):
        return await event.respond(text, formatting_entities=entities)
    overlay = FirstPhotoReply(bot, event, "فعال سازی", entry)
    return await overlay.send(event.respond, (text,), {"formatting_entities": entities}, reply=False)
