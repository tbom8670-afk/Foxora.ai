"""Protocol UI: nonblocking activation animation and notice dispatch."""
import asyncio
import logging
import time

from modules import virtual_protocol_texts as texts
from modules.group_id import normalize_group_id
from modules.notice_cleanup import _chat_id_for_rpc, extract_sent_id
from modules.user_display import format_user

LOG = logging.getLogger(__name__)


async def send(bot, peer, payload, ttl=None, *, command=None):
    text, entities = payload
    sent, photo_sent = (None, False)
    if command is not None:
        from handlers.command_photo_handler import send_command_photo
        sent, photo_sent = await send_command_photo(
            bot, peer, command, text, entities, parse_mode=None)
    if not photo_sent:
        sent = await bot.client.send_message(
            peer, text, formatting_entities=entities, parse_mode=None)
    if ttl is not None:
        cleanup = getattr(bot, 'notice_cleanup', None)
        scheduled = False
        if cleanup is not None:
            try:
                scheduled = bool(cleanup.schedule(peer, sent, ttl=ttl))
            except Exception:
                scheduled = False
        if not scheduled:
            from modules.profile_guard import cleanup as fallback_cleanup
            try:
                scheduled = fallback_cleanup.schedule(bot, peer, sent, ttl=ttl, text=text) is not None
            except Exception:
                scheduled = False
        if not scheduled:
            LOG.warning('virtual protocol notice cleanup unavailable')
    return sent


def event_peer(event):
    from modules.light_spam_ingest import _event_rpc_peer
    return _event_rpc_peer(event) or getattr(event, 'chat_id', None)


async def user_label(bot, user_id, event=None):
    user = getattr(event, 'sender', None) if event is not None else None
    if user is None and event is not None and callable(getattr(event, 'get_sender', None)):
        try:
            user = await asyncio.wait_for(event.get_sender(), 3)
        except Exception:
            pass
    if user is None:
        try:
            user = await asyncio.wait_for(bot.client.get_entity(int(user_id)), 3)
        except Exception:
            pass
    return format_user(user)


async def activate(service, event, *, step_seconds=1.0):
    group = normalize_group_id(event.chat_id)
    peer = event_peer(event)
    bot = service.bot
    try:
        if service.groups.get(group):
            await send(bot, peer, texts.enabled(group, await user_label(bot, getattr(event, 'sender_id', None), event)), command="پروتکل روشن")
            return
        # The first square is displayed immediately, then 19 incremental edits.
        sent = await send(bot, peer, texts.progress(1), command="پروتکل روشن")
        mid = extract_sent_id(sent)
        if not mid:
            return
        start = time.monotonic()
        for count in range(2, 21):
            await asyncio.sleep(max(0.0, start + (count - 1)*step_seconds - time.monotonic()))
            text, entities = texts.progress(count)
            try:
                await asyncio.wait_for(bot.client.edit_message(peer, mid, text,
                    formatting_entities=entities, parse_mode=None), max(.05, step_seconds*.8))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning('virtual protocol activation edit failed group=%s step=%s', group, count)
                return
        await asyncio.sleep(max(0.0, start + 20*step_seconds - time.monotonic()))
        await service.toggle(group, True)
        text, entities = texts.enabled(group, await user_label(bot, getattr(event, 'sender_id', None), event))
        await bot.client.edit_message(peer, mid, text, formatting_entities=entities, parse_mode=None)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.exception('virtual protocol activation failed')
    finally:
        if service.activations.get(group) is asyncio.current_task():
            service.activations.pop(group, None)


def begin_activation(service, event):
    group = normalize_group_id(event.chat_id)
    existing = service.activations.get(group)
    if existing and not existing.done():
        return False
    if len(service.activations) >= 128:
        return False
    service.activations[group] = asyncio.create_task(activate(service, event), name='virtual-protocol-activation')
    return True


async def deactivate(service, event):
    group = normalize_group_id(event.chat_id)
    task = service.activations.pop(group, None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await service.toggle(group, False)
    try:
        await send(service.bot, event_peer(event), texts.disabled(), command="پروتکل خاموش")
    except Exception:
        LOG.exception('virtual protocol deactivation notice failed')
