"""Five-minute, deletion-only warning consequence for native group admins.

This is deliberately separate from ``spam_lock``: the latter belongs to the
ordinary ban/mute pipeline and is cleared when a native administrator's stale
punishment state is reconciled. No restriction RPC or timer task is created
here. All entry points use the same per-bot, per-(group, user) deadline.
"""

import asyncio
import time

from modules.group_id import normalize_group_id


DURATION_SECONDS = 5 * 60


def _key(chat_id, user_id):
    if chat_id is None or user_id is None:
        return None
    return normalize_group_id(chat_id), str(user_id)


def _locks(bot):
    locks = getattr(bot, "_native_admin_warning_locks", None)
    if locks is None:
        locks = bot._native_admin_warning_locks = {}
    return locks


def _record(bot, chat_id, user_id, now=None):
    key = _key(chat_id, user_id)
    if key is None:
        return None
    locks = _locks(bot)
    record = locks.get(key)
    if record is None:
        return None
    now = time.monotonic() if now is None else now
    if record[0] <= now:
        locks.pop(key, None)
        return None
    return record


def is_active(bot, chat_id, user_id, *, now=None):
    """Read and lazily expire the independent deletion period."""
    return _record(bot, chat_id, user_id, now) is not None


def begin(bot, chat_id, user_id, *, trigger_message_id=None, now=None):
    """Start once; later warnings must not extend the existing five minutes.

    ``trigger_message_id`` is the last message before the period starts. This
    also protects older events/edits that were queued before the threshold.
    """
    key = _key(chat_id, user_id)
    if key is None:
        return False
    now = time.monotonic() if now is None else now
    if _record(bot, chat_id, user_id, now) is not None:
        return False
    cutoff = (
        trigger_message_id
        if isinstance(trigger_message_id, int) and trigger_message_id > 0
        else None
    )
    _locks(bot)[key] = (now + DURATION_SECONDS, cutoff)
    return True


def release(bot, chat_id, user_id):
    """Only cancel this deletion period, never another user's spam lock."""
    key = _key(chat_id, user_id)
    return key is not None and _locks(bot).pop(key, None) is not None


def cleanup_expired(bot, *, now=None):
    """Sweep idle groups too; the read path also expires on its own."""
    now = time.monotonic() if now is None else now
    locks = _locks(bot)
    removed = 0
    for key, (deadline, _cutoff) in list(locks.items()):
        if deadline <= now:
            locks.pop(key, None)
            removed += 1
    return removed


def _event_user_id(event):
    message = getattr(event, "message", None)
    update = getattr(event, "original_update", None)
    for candidate in (
        getattr(event, "sender_id", None),
        getattr(message, "sender_id", None),
        getattr(getattr(event, "sender", None), "id", None),
        getattr(getattr(event, "_bot_cached_sender", None), "id", None),
        getattr(message, "from_id", None),
        getattr(update, "user_id", None),
        getattr(update, "from_id", None),
    ):
        value = getattr(candidate, "user_id", candidate)
        if value is not None:
            return value
    return None


def _event_rpc_peer(event):
    for owner in (getattr(event, "message", None), event):
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


def drop_if_active(bot, event):
    """Queue only subsequent messages by this admin, regardless of content.

    Called before the priority-command, game and media paths, and again in
    the handler/overflow path for events already in flight. Registered bot
    authorities and VIPs keep their existing exemptions. No role lookup or
    network RPC is made in this hot-path check.
    """
    if getattr(event, "is_private", False):
        return False
    chat_id = getattr(event, "chat_id", None)
    user_id = _event_user_id(event)
    record = _record(bot, chat_id, user_id)
    if record is None:
        return False

    sender = (getattr(event, "sender", None)
              or getattr(event, "_bot_cached_sender", None))
    username = getattr(sender, "username", None)
    if str(user_id) == str(getattr(bot, "bot_account_id", None)):
        release(bot, chat_id, user_id)
        return False
    from modules import admin_tools, vip_storage
    if (admin_tools.has_admin_permission(chat_id, user_id, username)
            or vip_storage.is_vip(chat_id, user_id)):
        release(bot, chat_id, user_id)
        return False

    message = getattr(event, "message", None)
    message_id = getattr(message, "id", None)
    if not isinstance(message_id, int) or message_id <= 0:
        return False
    if record[1] is not None and message_id <= record[1]:
        return False

    ids = [message_id]
    if (getattr(event, "_is_edited_message", False)
            or getattr(event, "is_edit", False)
            or getattr(message, "edit_date", None) is not None):
        from modules.bot_reply_registry import pop_reply_ids
        ids.extend(pop_reply_ids(chat_id, message_id))

    queue = getattr(bot, "message_delete_queue", None)
    if queue is not None:
        peer = _event_rpc_peer(event)
        try:
            queue.enqueue(chat_id, ids, priority=1, rpc_peer=peer)
        except TypeError as error:
            if "rpc_peer" not in str(error):
                raise
            queue.enqueue(chat_id, ids, priority=1)
    else:
        # Compatibility with minimal bots; production always has the queue.
        asyncio.create_task(
            bot.client.delete_messages(chat_id, ids), name="delete:native-admin-warning"
        )
    return True
