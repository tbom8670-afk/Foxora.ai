"""Independent anti-story check of visible active story captions.

Uses its own per-group switch and queue. No OCR; API errors and unavailable
stories are unknown, not violations. Captions are not logged or persisted.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
import re
from functools import lru_cache

from modules import bio_link
from modules.manage_features.queue import PerGroupQueue, QueueFull
from modules.profile_guard import filters, matcher, story_storage as storage, texts

_EXTRA_TERMS = (
    "داغم", "داغ", "حشری", "فیلم سوپر",
    "ربات", "ربات فیلم", "منتظرتم بیا", "فروش فیلم",
)
QUEUE = PerGroupQueue("story_guard", max_pending_per_key=32,
                      max_pending_total=128, max_keys=128, task_timeout=90.0)
_REPEAT = re.compile(r"([^\W_])(?:[\W_]*\1)+", re.UNICODE)
# Beyond the shared bio detector, recognize arbitrary domain suffixes and
# common app/deep-link schemes, not just a hardcoded list of TLDs.
_URL_SCHEME = re.compile(r"(?<![\w@])(?:https?|ftps?|hxxps?|splus|soroush|tg|telegram)://[^\s/]+", re.I)
_DOMAIN = re.compile(r"(?<![\w@.-])([\w](?:[\w-]{0,61}[\w])?(?:\.[\w](?:[\w-]{0,61}[\w])?)*\.([^\W\d_]{2,63}))(?![\w-])", re.UNICODE)
_FILE_SUFFIXES = frozenset(('jpg', 'jpeg', 'png', 'gif', 'webp', 'pdf', 'doc', 'docx',
                            'txt', 'mp3', 'mp4', 'zip', 'rar', 'exe', 'apk', 'py', 'sh'))


def filter_terms():
    # Use the live lists, including owner imports and per-instance edits.
    return tuple(dict.fromkeys(filters.channel_names() + filters.bio_sentences() + list(_EXTRA_TERMS)))


def _fold_stretched(value):
    # Repeated characters even with punctuation/emoji/space between them.
    # Do not discard arbitrary letters or match inside unrelated longer words.
    return _REPEAT.sub(r"\1", matcher.normalize(value)).replace('_', ' ')


@lru_cache(maxsize=16)
def _patterns(terms):
    return matcher.build(terms), matcher.build(tuple(_fold_stretched(p) for p in terms))


def _caption_link(caption):
    """Detect links without treating a bare @username or email as a URL."""
    if not isinstance(caption, str) or not caption.strip():
        return False
    label = bio_link.find_link(caption, detect_username=False)
    if label and not label.startswith('آیدی '):
        return True
    value = bio_link.compact(caption)
    if _URL_SCHEME.search(value):
        return True
    for match in _DOMAIN.finditer(value):
        # Bare filenames are ambiguous; a protocol or path is still a link.
        if match.group(2).lower() in _FILE_SUFFIXES and value[match.end():match.end()+1] != '/':
            continue
        return True
    return False


def caption_match(caption):
    original, stretched = _patterns(filter_terms())
    return (matcher.find_match(caption, original)
            or matcher.find_match(_fold_stretched(caption), stretched)
            or ('لینک' if _caption_link(caption) else None))


def _caption_entity_link(item):
    # The API exposes hidden text URLs as entities, not in caption text.
    # Do not infer a link from formatting entities such as bold or mentions.
    for entity in getattr(item, 'entities', None) or ():
        kind = type(entity).__name__
        if kind == 'MessageEntityUrl':
            return True
        if kind == 'MessageEntityTextUrl' and str(getattr(entity, 'url', '') or '').strip():
            return True
    return False


async def handle_toggle(bot, event, chat_id, user_id, sender, mode):
    from modules.profile_guard import guard
    if not guard._is_admin(chat_id, user_id, sender):
        try:
            await event.reply("🏕 فقط ادمین های ثبت شده مجاز هستند")
        except Exception:
            pass
        return True
    if mode == 'on':
        storage.enable(chat_id)
        filters.reload()
        await guard._send(bot, event, chat_id, texts.story_enabled(len(filter_terms())))
    else:
        storage.disable(chat_id)
        await guard._send(bot, event, chat_id, texts.story_disabled())
    _log(bot, f'{mode} chat_id={chat_id} user_id={user_id}')
    return True


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


def _items(response):
    # GetPeerStories -> response.stories.stories; GetStoriesByID -> response.stories.
    container = getattr(response, 'stories', None)
    items = container if isinstance(container, (list, tuple)) else getattr(container, 'stories', None)
    return items if isinstance(items, (list, tuple)) else None


def matching_caption(response):
    """Return the first matching caption, never media/empty/skipped items."""
    for item in _items(response) or ():
        caption = getattr(item, 'caption', None)
        if (isinstance(caption, str) and caption.strip() and _active(item)
                and (caption_match(caption) or _caption_entity_link(item))):
            return caption
    return None


async def _exempt(bot, chat_id, user_id, sender):
    from modules.profile_guard import guard
    if not storage.is_enabled(chat_id):
        _log(bot, f'skip chat_id={chat_id} user_id={user_id} reason=disabled')
        return True
    if guard._is_exempt(chat_id, user_id, sender):
        _log(bot, f'skip chat_id={chat_id} user_id={user_id} reason=local_exemption')
        return True
    if str(user_id) == str(getattr(bot, 'bot_account_id', None)):
        return True
    try:
        from modules import native_admin_exemption
        exempt = bool(
            native_admin_exemption.enabled()
            and await native_admin_exemption.is_native_admin(
                bot, chat_id, user_id, sender, fresh=True
            )
        )
        if exempt:
            _log(bot, f'skip chat_id={chat_id} user_id={user_id} reason=native_admin')
        return exempt
    except Exception as error:
        # Unexpected exemption errors must not permit punishment.
        _log(bot, f'skip chat_id={chat_id} user_id={user_id} reason=exemption_error error={type(error).__name__}', error=True)
        return True


async def _fetch_caption(bot, sender, scheduled):
    """A fresh RPC; None is unknown/no matching caption, never an infraction."""
    global _last_request
    from modules.profile_guard import service
    from splusthon.tl.functions.stories import GetPeerStoriesRequest, GetStoriesByIDRequest
    from splusthon.tl.types import StoryItemSkipped
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
        items = _items(response)
        if items is None:
            _log(bot, 'invalid response shape', error=True)
            return None
        caption = matching_caption(response)
        skipped = [int(item.id) for item in items
                   if isinstance(item, StoryItemSkipped) and _active(item) and item.id is not None]
        _log(bot, f'peer stories count={len(items)} captions={sum(isinstance(getattr(i, "caption", None), str) for i in items)} skipped={len(skipped)}')
        if caption or not skipped:
            return caption
        # Some servers return only IDs for skipped stories; request their full
        # content instead of treating those as proof that there is no caption.
        wait = service.rpc_min_interval() - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()
        expanded = await asyncio.wait_for(
            bot.client(GetStoriesByIDRequest(peer=peer, id=skipped[:100])),
            timeout=service.fetch_timeout(),
        )
        expanded_items = _items(expanded)
        if expanded_items is None:
            _log(bot, 'invalid expanded response shape', error=True)
            return None
        _log(bot, f'expanded stories count={len(expanded_items)} captions={sum(isinstance(getattr(i, "caption", None), str) for i in expanded_items)}')
        return matching_caption(expanded)


async def _act(bot, event, chat_id, user_id, sender, message_id, caption):
    from modules.profile_guard import guard
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
        async def submit_job():
            try:
                future = await QUEUE.submit(
                    chat_id,
                    lambda: _run(bot, event, chat_id, user_id, sender, message_id, key, now),
                    dedupe_key=user_id, logger=getattr(bot, 'logger', None),
                )
                await future
            except QueueFull:
                _log(bot, f'queue full chat_id={chat_id} user_id={user_id}', error=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log(bot, f'queue error chat_id={chat_id} user_id={user_id} error={type(error).__name__}', error=True)
            finally:
                _inflight.discard(key)
        task = loop.create_task(submit_job())
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except RuntimeError:
        _inflight.discard(key)
        return False
