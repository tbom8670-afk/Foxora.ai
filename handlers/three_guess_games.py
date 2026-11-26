"""Thin event adapter for the three individual guessing games."""
from __future__ import annotations

import asyncio
from modules import entertainment_control
from modules.three_guess_games import game

_TIMERS = {}


def _timer_key(chat_id, user_id):
    return game._key(chat_id, user_id)


def _cancel_timer(chat_id, user_id):
    task = _TIMERS.pop(_timer_key(chat_id, user_id), None)
    if task is not None and not task.done():
        task.cancel()


async def route(bot, event, chat_id, user_id, sender, text, *, busy, is_command, track_timer, reply_id=None):
    command = entertainment_control.normalize(text)
    state = game.current(chat_id, user_id)
    if getattr(event, 'is_private', False):
        return False
    if command in game.GAMES:
        if state:
            await event.reply('⏳ یک مرحله باز داری؛ اول همان را جواب بده یا صبر کن زمانش تمام شود.')
            return True
        if busy:
            await event.reply('⏳ یک بازی دیگر در جریان است؛ اول آن را تمام کن.')
            return True
        state = game.start(chat_id, user_id, command)
        if not state:
            return True
        if state['kind'] == 'finished':
            await event.reply('🏆 همهٔ ۲۲۰ مرحلهٔ این بازی را دیده‌ای؛ منتظر مراحل تازه باش.')
            return True
        if state['kind'] == 'busy':
            await event.reply('⏳ لطفاً تا پایان بازی بازیکن دیگر صبر کن.')
            return True
        try:
            message, entities = game.payload(state)
            sent = await event.reply(message, formatting_entities=entities)
            message_id = getattr(sent, 'id', None) or getattr(sent, 'message_id', None)
            if not game.bind_prompt(chat_id, user_id, state['token'], message_id):
                return True
        except Exception:
            game.finish(chat_id, user_id, state['token'])
            raise
        async def timeout():
            await asyncio.sleep(game.TIME_LIMIT)
            answer = game.finish(chat_id, user_id, state['token'])
            if answer and entertainment_control.is_enabled(chat_id) and not entertainment_control.leave_blocks(chat_id):
                await event.reply(f'⏰ زمان تمام شد!\n\n✅ جواب درست: {answer}')
        key = _timer_key(chat_id, user_id)
        _cancel_timer(chat_id, user_id)
        task = asyncio.create_task(timeout())
        _TIMERS[key] = task
        def forget(finished):
            if _TIMERS.get(key) is finished:
                _TIMERS.pop(key, None)
            if finished.cancelled():
                game.finish(chat_id, user_id, state['token'])
        task.add_done_callback(forget)
        track_timer(bot, chat_id, task)
        return True
    if not state:
        return False
    if not entertainment_control.is_enabled(chat_id) or entertainment_control.leave_blocks(chat_id):
        game.finish(chat_id, user_id, state['token'])
        _cancel_timer(chat_id, user_id)
        return False
    if entertainment_control.is_game_command(command):
        await event.reply('⏳ اول مرحلهٔ بازت را تمام کن، بعد بازی بعدی را شروع کن.')
        return True
    if is_command(command):
        return False
    name = ' '.join(filter(None, (getattr(sender, 'first_name', None), getattr(sender, 'last_name', None)))) or getattr(sender, 'username', None)
    result = game.answer(chat_id, user_id, text, name=name, reply_id=reply_id)
    if result is None:
        return False
    _cancel_timer(chat_id, user_id)
    if result['payment_error']:
        await event.reply('✅ جواب درست بود، ولی پرداخت سکه تأیید نشد. لطفاً به مدیر اطلاع بده.')
    else:
        await event.reply(f"🎉 جواب درست بود!\n\n🥉 {result['reward']} سکه برنز به موجودیت اضافه شد.")
    return True
