"""Sync light-path spam ingest that runs before GroupDispatcher.

Only ``chat_id``, ``user_id``, ``message_id`` and the raw text are used.
No RPC, no ``get_sender`` / ``get_chat``, no profile/games, no per-message log.
"""
import time

from modules import big_spam
from modules import message_tracker
from modules.group_dispatch import PRIORITY_NORMAL, classify_priority
from modules.group_id import normalize_group_id


class IngestResult:
    __slots__ = ("skip_heavy", "detected", "reason", "tracked")

    def __init__(self, skip_heavy, detected, reason="", tracked=False):
        self.skip_heavy = bool(skip_heavy)
        self.detected = bool(detected)
        self.reason = reason or ""
        self.tracked = bool(tracked)


def incident_key(chat_id, user_id):
    """One runtime key for both full ``-100...`` and short channel IDs."""
    return normalize_group_id(chat_id), str(user_id)


def _peer_id(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    for attr in ("user_id", "channel_id", "chat_id", "id"):
        try:
            found = getattr(value, attr, None)
        except Exception:
            continue
        if isinstance(found, int):
            return found
        if found is not None:
            try:
                return int(found)
            except (TypeError, ValueError):
                pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_user_id(event, message):
    """Read the sender without an RPC across SPlusthon event shapes."""
    candidates = [
        getattr(event, "sender_id", None),
        getattr(message, "sender_id", None) if message is not None else None,
        getattr(event, "sender", None),
        getattr(message, "from_id", None) if message is not None else None,
    ]
    update = getattr(event, "original_update", None)
    if update is not None:
        candidates.extend((
            getattr(update, "user_id", None),
            getattr(update, "from_id", None),
        ))
    for candidate in candidates:
        found = _peer_id(candidate)
        if found is not None:
            return found
    return None


def _event_rpc_peer(event):
    """Read an already-resolved Soroush InputPeer without an RPC."""
    message = getattr(event, "message", None)
    for owner in (message, event):
        if owner is None:
            continue
        for attr in ("_input_chat", "input_chat"):
            try:
                peer = getattr(owner, attr, None)
            except Exception:
                peer = None
            if peer is not None:
                return peer
    return None


def extract_event(event):
    """Read ids and text from the event object only. Never awaits."""
    message = getattr(event, "message", None)
    chat_id = getattr(event, "chat_id", None)
    message_id = getattr(message, "id", None) if message is not None else None
    text = ""
    if message is not None:
        text = (
            getattr(message, "message", None)
            or getattr(message, "caption", None)
            or ""
        )
    user_id = _event_user_id(event, message)
    is_private = bool(getattr(event, "is_private", False))
    return chat_id, user_id, message_id, text, is_private


def _cheap_admin_bypass(bot, chat_id, user_id):
    """Registered owner/admin only. Never calls the network."""
    probe = getattr(bot, "_light_admin_bypass", None)
    if callable(probe):
        try:
            return bool(probe(chat_id, user_id))
        except Exception:
            return False
    if user_id is None:
        return False
    if user_id == getattr(bot, "bot_account_id", None):
        return True
    try:
        from modules.admin_tools import has_admin_permission
        if has_admin_permission(chat_id, user_id, None):
            return True
    except Exception:
        pass
    try:
        from modules import vip_storage
        if vip_storage.is_vip(chat_id, user_id):
            return True
    except Exception:
        pass
    cache = getattr(bot, "native_group_admin_cache", None)
    if cache:
        try:
            from modules.group_id import normalize_group_id
            cached = cache.get((normalize_group_id(chat_id), str(user_id)))
            if cached and cached[0]:
                return True
        except Exception:
            pass
    return False


def _capture(bot, chat_id, user_id, message_id):
    incidents = getattr(bot, "_big_spam_incidents", None)
    if not incidents:
        return
    incident = incidents.get(incident_key(chat_id, user_id))
    if incident is not None and isinstance(message_id, int) and message_id > 0:
        incident["ids"].add(message_id)


def _start_wave(bot, event, chat_id, user_id, ids, reason):
    starter = getattr(bot, "_queue_big_spam_ban", None)
    if callable(starter):
        return starter(event, chat_id, user_id, None, ids, reason)
    from handlers.message_handler import _queue_big_spam_ban
    return _queue_big_spam_ban(bot, event, chat_id, user_id, None, ids, reason)


def ingest(bot, chat_id, user_id, message_id, text, *, event=None, is_private=False):
    """Track + detect. ``skip_heavy`` means do not enqueue the full handler."""
    try:
        return _ingest(
            bot, chat_id, user_id, message_id, text,
            event=event, is_private=is_private,
        )
    except Exception as error:
        # Never let the early safety path crash message delivery, but do not
        # fail silently either.  Rate-limit the diagnostic so one malformed
        # event cannot create a second log flood.
        now = time.monotonic()
        last = float(getattr(bot, "_light_spam_error_logged_at", 0.0) or 0.0)
        if now - last >= 30.0:
            bot._light_spam_error_logged_at = now
            logger = getattr(bot, "logger", None)
            if logger is not None:
                logger.log_error(
                    "LIGHT SPAM INGEST FAILED "
                    f"chat_id={chat_id} user_id={user_id} "
                    f"message_id={message_id} error={error!r}"
                )
        return IngestResult(skip_heavy=False, detected=False)


def ingest_event(bot, event):
    chat_id, user_id, message_id, text, is_private = extract_event(event)
    return ingest(
        bot, chat_id, user_id, message_id, text,
        event=event, is_private=is_private,
    )


def _ingest(bot, chat_id, user_id, message_id, text, *, event=None, is_private=False):
    priority, _kind = classify_priority(text)
    # Admin and public/user commands skip detect so they stay on their
    # dedicated dispatcher lanes and cannot be delayed or mis-tagged.
    if priority < PRIORITY_NORMAL:
        return IngestResult(skip_heavy=False, detected=False)
    if is_private:
        return IngestResult(skip_heavy=False, detected=False)
    if chat_id is None or message_id is None or user_id is None:
        return IngestResult(skip_heavy=False, detected=False)
    if not str(text or "").strip():
        return IngestResult(skip_heavy=False, detected=False)
    if _cheap_admin_bypass(bot, chat_id, user_id):
        return IngestResult(skip_heavy=False, detected=False)

    # 🦊 پیام‌های «مرکز فرماندهی روباه» (ثبت‌نام/خرید/حمله/دفاع) دادهٔ بازی‌اند،
    # نه پیام عمومی. بدون این بررسی، «شرکت» و پاسخ‌های اعضای عادی در همین
    # لایه بلعیده می‌شد و بازی فقط برای مالک/ادمین کار می‌کرد.
    try:
        from handlers import fox_command_router as _fox_command
        if _fox_command.bypass(chat_id, user_id, text):
            return IngestResult(skip_heavy=False, detected=False,
                                reason="fox_command")
    except Exception:
        pass

    # پاسخ داخل یک بازیِ در جریان (بقا/زامبی/دادگاه روباه/…) دادهٔ بازی است،
    # نه پیام عمومی. پیش از بررسی قفل هرزنگاری و incident‌ها سنجیده می‌شود
    # تا بازیکنِ عادی وسط بازی پیامش حذف/بلعیده نشود؛ پیش‌تر این بررسی
    # بعد از شاخه‌های قفل بود و «شرکت»/پاسخ بازیکن از دست می‌رفت.
    game_probe = getattr(bot, "_light_game_answer_active", None)
    if callable(game_probe):
        try:
            if game_probe(chat_id, user_id):
                return IngestResult(skip_heavy=False, detected=False,
                                    reason="game_answer")
        except Exception:
            pass

    key = incident_key(chat_id, user_id)
    incidents = getattr(bot, "_big_spam_incidents", {}) or {}
    if key in incidents:
        tracked = message_tracker.add_message(chat_id, user_id, message_id, text)
        _capture(bot, chat_id, user_id, message_id)
        return IngestResult(
            skip_heavy=True, detected=True, tracked=tracked,
        )

    # A confirmed spam lock may outlive the short incident drain. Delete new
    # burst messages here instead of sending them through the expensive full
    # handler, where get_chat/get_sender and a full normal queue delayed them.
    lock_probe = getattr(bot, "is_spam_locked", None)
    if callable(lock_probe) and lock_probe(key):
        tracked = message_tracker.add_message(chat_id, user_id, message_id, text)
        queue = getattr(bot, "message_delete_queue", None)
        if queue is not None:
            rpc_peer = _event_rpc_peer(event)
            if rpc_peer is None:
                queue.enqueue(chat_id, [message_id], priority=1)
            else:
                try:
                    queue.enqueue(
                        chat_id, [message_id], priority=1,
                        rpc_peer=rpc_peer,
                    )
                except TypeError as error:
                    if "rpc_peer" not in str(error):
                        raise
                    queue.enqueue(chat_id, [message_id], priority=1)
            return IngestResult(
                skip_heavy=True, detected=True,
                reason="active_spam_lock", tracked=tracked,
            )
        return IngestResult(
            skip_heavy=False, detected=True,
            reason="active_spam_lock", tracked=tracked,
        )

    tracked = message_tracker.add_message(chat_id, user_id, message_id, text)
    rows = message_tracker.get_user_recent_messages(chat_id, user_id)
    game_probe = getattr(bot, "_light_game_answer_active", None)
    game_answer = bool(game_probe(chat_id, user_id)) if callable(game_probe) else False
    hit, reason, ids = big_spam.detect_big_spam(
        text, rows, allow_generic=not game_answer,
    )
    if not hit:
        return IngestResult(skip_heavy=False, detected=False, tracked=tracked)

    if isinstance(message_id, int) and message_id > 0:
        ids.add(message_id)
    _start_wave(bot, event, chat_id, user_id, ids, reason)
    return IngestResult(
        skip_heavy=True, detected=True, reason=reason, tracked=tracked,
    )
