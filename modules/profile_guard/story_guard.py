"""Experimental anti-channel check for a visible active story's caption.

Runs only for groups where profile guard is enabled, in the background after a
user message. No OCR; errors/skipped stories are unknown, not violations. No
captions are logged or persisted. Existing owner/admin/VIP exemptions apply.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time

from modules.profile_guard import matcher, storage, texts

_TERMS = matcher.build(["کانالم"])
_inflight: set[tuple[str, str]] = set()
_tasks: set[asyncio.Task] = set()
_retry_after: dict[tuple[str, str], float] = {}
_sem = None
_last_request = 0.0
_flood_until = 0.0
_MAX_INFLIGHT = 128
_MAX_PENDING_AGE = 60.0
_CHECK_INTERVAL = 60.0
_ERROR_INTERVAL = 30.0


def _log(bot, text, *, error=False):
    from modules.profile_guard import guard
    guard._log(bot, 'STORY ' + text, error=error)


def _semaphore():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(1)
    return _sem


def _active(story):
    expiry = getattr(story, 'expire_date', None)
    if isinstance(expiry, datetime):
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry.timestamp() <= time.time():
            return False
    return True


def matching_caption(response):
    """Return the first matching caption, never media/empty/skipped items."""
    container = getattr(response, 'stories', None)
    items = getattr(container, 'stories', None)
    if not isinstance(items, (list, tuple)):
        return None
    for item in items:
        caption = getattr(item, 'caption', None)
        if isinstance(caption, str) and _active(item) and matcher.find_match(caption, _TERMS):
            return caption
    return None


async def _exempt(bot, chat_id, user_id, sender):
    from modules.profile_guard import guard
    if not storage.is_enabled(chat_id) or guard._is_exempt(chat_id, user_id, sender):
        return True
    if str(user_id) == str(getattr(bot, 'bot_account_id', None)):
        return True
    try:
        from modules import native_admin_exemption
        return bool(await native_admin_exemption.exempt(bot, chat_id, user_id, sender, fresh=True))
    except Exception:
        # No definitive exemption result: fail closed for this new feature.
        return True


async def _fetch_caption(bot, sender, scheduled):
    """A fresh RPC; None is unknown/no matching caption, never an infraction."""
    global _last_request
    from modules.profile_guard import service
    from splusthon.tl.functions.stories import GetPeerStoriesRequest
    async with _semaphore():
        if time.monotonic() - scheduled > _MAX_PENDING_AGE or time.monotonic() < _flood_until:
            raise TimeoutError("story request expired or flood cooldown active")
        wait = service.rpc_min_interval() - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        peer = await asyncio.wait_for(bot.client.get_input_entity(sender), timeout=service.fetch_timeout())
        _last_request = time.monotonic()
        response = await asyncio.wait_for(
            bot.client(GetPeerStoriesRequest(peer=peer)), timeout=service.fetch_timeout()
        )
    return matching_caption(response)


async def _act(bot, event, chat_id, user_id, sender, message_id, caption):
    from modules.profile_guard import guard, penalties
    duration = guard.bio_mute_seconds()
    deleted = muted = False
    try:
        await bot.client.delete_messages(int(chat_id), [int(message_id)])
        deleted = True
    except Exception as error:
        _log(bot, f'delete failed chat_id={chat_id} user_id={user_id} error={type(error).__name__}', error=True)
    try:
        if getattr(bot, 'admin_actions', None) is not None:
            muted = bool(await bot.admin_actions.mute_user(chat_id, user_id, duration, user=sender, chat=None))
    except Exception as error:
        _log(bot, f'mute failed chat_id={chat_id} user_id={user_id} error={type(error).__name__}', error=True)
    if muted:
        try:
            penalties.record(chat_id, user_id, 'story', time.time() + duration)
        except Exception as error:
            _log(bot, f'penalty record failed error={type(error).__name__}', error=True)
    # Bound the untrusted caption so the warning fits message size limits.
    shown = caption if len(caption) <= 1500 else caption[:1500] + '…'
    payload = texts.notice_story(guard._display_name(sender), shown,
                                 duration_seconds=duration, deleted=deleted, muted=muted)
    await guard._send(bot, event, chat_id, payload, notice=True)
    _log(bot, f'action chat_id={chat_id} user_id={user_id} deleted={deleted} muted={muted}')


async def _run(bot, event, chat_id, user_id, sender, message_id, key, scheduled):
    global _flood_until
    try:
        if await _exempt(bot, chat_id, user_id, sender):
            return
        caption = await _fetch_caption(bot, sender, scheduled)
        _retry_after[key] = time.monotonic() + _CHECK_INTERVAL
        if not caption:
            _log(bot, f'check complete chat_id={chat_id} user_id={user_id} match=no')
            return
        if time.monotonic() - scheduled > _MAX_PENDING_AGE:
            _log(bot, f'check expired chat_id={chat_id} user_id={user_id}')
            return
        # Settings and authority may change while waiting for the server.
        if await _exempt(bot, chat_id, user_id, sender):
            return
        await _act(bot, event, chat_id, user_id, sender, message_id, caption)
    except Exception as error:
        if 'flood' in type(error).__name__.lower() or 'flood' in str(error).lower():
            try:
                seconds = max(1, min(3600, int(getattr(error, 'seconds', 30))))
            except (TypeError, ValueError):
                seconds = 30
            _flood_until = max(_flood_until, time.monotonic() + seconds)
        _retry_after[key] = time.monotonic() + _ERROR_INTERVAL
        detail = str(error).lower()
        status = ('unsupported' if 'not_supported' in detail or 'not supported' in detail
                  else 'privacy' if 'privacy' in detail or 'privacy' in type(error).__name__.lower()
                  else 'flood' if time.monotonic() < _flood_until else 'error')
        _log(bot, f'check failed chat_id={chat_id} user_id={user_id} status={status} error={type(error).__name__}', error=True)
    finally:
        _inflight.discard(key)


def schedule(bot, event, chat_id, user_id, sender, message_id):
    if not chat_id or not user_id or sender is None or message_id is None or getattr(event, 'is_private', False):
        return False
    key = (str(chat_id), str(user_id))
    now = time.monotonic()
    if (now < _flood_until or key in _inflight or len(_inflight) >= _MAX_INFLIGHT
            or _retry_after.get(key, 0) > now):
        return False
    # Bound the success/failure cooldown map without keeping captions in it.
    if len(_retry_after) > 4096:
        for old in list(_retry_after):
            if _retry_after[old] <= now:
                del _retry_after[old]
    try:
        loop = asyncio.get_running_loop()
        _inflight.add(key)
        task = loop.create_task(_run(bot, event, chat_id, user_id, sender, message_id, key, now))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except RuntimeError:
        _inflight.discard(key)
        return False
