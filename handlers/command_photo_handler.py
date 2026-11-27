"""Owner-configured photo attached to the first response of a group command.

The original command and its authorization stay untouched. A configured
photo replaces only the FIRST event.reply/respond with one media+caption
message. The caption is the handler's original response, with its entities.
"""
from __future__ import annotations

import time
import json
import contextvars
import functools
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
_configured = None  # In-memory index: NEVER query SQLite on each incoming command.
_config_file = None
_media_cache = {}  # command -> server-side media, refreshed on send failure
_active_photo = contextvars.ContextVar("command_photo_current", default=None)


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
    return bool(_sessions and chat_id is not None and user_id is not None
                and _pending(chat_id, user_id) is not None
                and is_global_owner(user_id))


def warm_config():
    """Load all configured photos once at startup, not once per message."""
    global _configured, _config_file
    db_file = str(runtime_db.DB_FILE)
    if _configured is not None and _config_file == db_file:
        return _configured
    try:
        rows = runtime_db.query_all(
            "SELECT item_key,payload FROM runtime_kv WHERE namespace=?", (NAMESPACE,))
        loaded = {}
        for command, raw in rows:
            try:
                entry = json.loads(raw)
                if isinstance(entry, dict) and entry.get("source"):
                    loaded[command] = entry
            except (ValueError, TypeError):
                continue
    except Exception:
        # Retry later if the database is temporarily unavailable.
        return {}
    _configured, _config_file = loaded, db_file
    _media_cache.clear()
    return loaded


def photo_for(command):
    return warm_config().get(normalize(command))


async def handle_setup(bot, event, chat_id, user_id, text):
    if chat_id is None or getattr(event, "is_private", False):
        return False
    if (getattr(event, "is_edit", False)
            or getattr(event, "_is_edited_message", False)
            or getattr(getattr(event, "message", None), "edit_date", None)):
        return False
    value = normalize(text)
    # Every ordinary message hits this handler; avoid owner-file lookups,
    # database access and RPCs unless it is a setup command or an active flow.
    setup = value.startswith(SETUP)
    reset = value.startswith(RESET)
    cancel = value == CANCEL
    if not (setup or reset or cancel or (user_id is not None and
                                         _key(chat_id, user_id) in _sessions)):
        return False
    if user_id is None:
        sender = await event.get_sender()
        user_id = getattr(sender, "id", None)
    if user_id is None or not is_global_owner(user_id):
        return False
    key = _key(chat_id, user_id)
    if setup:
        command = value[len(SETUP):].strip()
        if len(command) < 2 or len(command) > 64 or command.startswith((SETUP, RESET)):
            await event.reply("❌ نام دستور معتبر نیست؛ مثال: تنظیم عکس فعال سازی")
            return True
        _sessions[key] = (command, time.monotonic())
        await event.reply(f"🖼 عکسِ دستور «{command}» را در همین گروه ارسال کنید؛ کپشنِ عکس نادیده گرفته می‌شود.")
        return True
    if reset:
        command = value[len(RESET):].strip()
        if command:
            runtime_db.kv_delete(NAMESPACE, command)
            warm_config().pop(normalize(command), None)
            _media_cache.pop(normalize(command), None)
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
        entry = {"source": ref}
        runtime_db.kv_set(NAMESPACE, command, entry)
        warm_config()[command] = entry
        _media_cache[command] = _media_ref._media(event.message)
    except Exception as error:
        bot.logger.log_error(f"COMMAND PHOTO SAVE FAILED command={command!r} error={error!r}")
        await event.reply("❌ عکس ذخیره نشد؛ دوباره ارسال کنید.")
        return True
    _sessions.pop(key, None)
    await event.reply(
        f"✅ عکس دستور «{command}» تنظیم شد."
        + ("" if archived else " ⚠️ پیام اصلی عکس را حذف نکنید."))
    return True


async def _send_photo(bot, peer, command, entry, text, send_kwargs):
    """Try the cached server-side media; refresh only on missing/stale refs.

    None means send the original unmodified text via the caller's path.
    """
    command = normalize(command)
    media = _media_cache.get(command)
    for attempt in range(2):
        try:
            if media is None:
                media = await _media_ref._source_media(bot, entry.get("source"))
            if media is None:
                raise ValueError("configured photo is unavailable")
            sent = await bot.client.send_file(peer, media, caption=text, **send_kwargs)
            _media_cache[command] = media
            return sent
        except Exception as error:
            _media_cache.pop(command, None)
            # Never resend after an ambiguous network error: the first photo
            # may already be delivered. Retry only a definite stale reference.
            reason = str(error).upper()
            if (attempt == 0 and media is not None
                    and any(marker in reason for marker in (
                        "FILE_REFERENCE", "FILE_ID_INVALID", "MEDIA_EMPTY"))):
                media = None
                continue
            logger = getattr(bot, "logger", None)
            if logger is not None:
                logger.log_error(f"COMMAND PHOTO SEND FAILED command={command!r} error={error!r}")
            return None


async def send_command_photo(bot, peer, command, text, entities, *, parse_mode=None):
    """For command handlers that send via client.send_message, not event.reply.

    Returns (sent, used_photo). A sent message can itself be None if a client
    queues it, so keep an explicit success flag.
    """
    entry = photo_for(command)
    if not isinstance(entry, dict) or not entry.get("source"):
        return None, False
    sent = await _send_photo(bot, peer, command, entry, text, {
        "formatting_entities": entities, "parse_mode": parse_mode,
    })
    return sent, sent is not None


def bind_overlay(overlay):
    """Bind one configured command to its task; never a global chat setting."""
    return _active_photo.set(overlay) if overlay is not None else None


def unbind_overlay(token):
    if token is not None:
        overlay = _active_photo.get()
        if overlay is not None:
            overlay.closed = True
        _active_photo.reset(token)


def install_client(bot):
    """Catch command handlers that send through client.send_message directly.

    The installed wrapper is inert for all other tasks and unconfigured
    commands: one contextvar read, no disk lookup or network request.
    """
    client = getattr(bot, "client", None)
    original = getattr(client, "send_message", None)
    if not callable(original) or getattr(original, "_command_photo_client_wrapped", False):
        return False

    @functools.wraps(original)
    async def wrapped(*args, **kwargs):
        overlay = _active_photo.get()
        if (overlay is not None and not overlay.used
                and not getattr(overlay, "closed", False) and len(args) <= 2):
            peer = args[0] if args else kwargs.get("entity", kwargs.get("peer"))
            text = (args[1] if len(args) > 1 else
                    kwargs.get("message", kwargs.get("text")))
            if isinstance(text, str):
                from modules.outgoing_sender import _chat_key
                if _chat_key(peer) == _chat_key(overlay.event.chat_id):
                    overlay.used = True
                    options = {"formatting_entities": kwargs.get("formatting_entities")}
                    for key in ("parse_mode", "buttons", "silent", "schedule",
                                "background", "reply_to"):
                        if key in kwargs:
                            options[key] = kwargs[key]
                    sent = await _send_photo(bot, peer, overlay.command, overlay.entry,
                                             text, options)
                    if sent is not None:
                        return sent
        return await original(*args, **kwargs)

    wrapped._command_photo_client_wrapped = True
    try:
        client.send_message = wrapped
    except (AttributeError, TypeError):
        return False
    return True


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
            send_kwargs = {"formatting_entities": kwargs.get("formatting_entities") or None}
            for option in ("buttons", "silent", "parse_mode", "schedule"):
                if option in kwargs:
                    send_kwargs[option] = kwargs[option]
            if reply:
                send_kwargs["reply_to"] = getattr(getattr(self.event, "message", None), "id", None)
            sent = await _send_photo(self.bot, self.event.chat_id, self.command,
                                     self.entry, text, send_kwargs)
            if sent is not None:
                return sent
        except Exception as error:
            self.bot.logger.log_error(
                f"COMMAND PHOTO SEND FAILED command={self.command!r} error={error!r}")
        return await original(*args, **kwargs)


def install_on_event(bot, event, command, *, skip_if_game_active=False):
    """Wrap just this event; no global client/handler monkeypatch or access change."""
    if getattr(event, "is_private", False) or getattr(event, "_command_photo_reply", None):
        return None
    entry = photo_for(command)
    if not isinstance(entry, dict) or not entry.get("source"):
        return None
    if skip_if_game_active:
        # Only for configured triggers. Game answers/stages must NEVER receive
        # the photo meant for the opening message of a game.
        from handlers.message_handler import is_game_answer_active
        actor = (getattr(event, "sender_id", None)
                 or getattr(getattr(event, "sender", None), "id", None))
        if is_game_answer_active(getattr(event, "chat_id", None), actor):
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
