"""Offline tests of content, ownership, real economy and routing."""
import asyncio
from types import SimpleNamespace as NS

import pytest
import economy
from economy import game_progress, storage
from modules.three_guess_games import game
from handlers import three_guess_games as adapter
from modules import entertainment_control
from splusthon.tl.types import MessageEntityBlockquote

CHAT = -1001234567890


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    storage.use_file(tmp_path / 'economy.json')
    game.ACTIVE.clear()
    monkeypatch.setattr(entertainment_control, 'is_enabled', lambda chat: True)
    monkeypatch.setattr(entertainment_control, 'leave_blocks', lambda chat: False)
    yield
    game.ACTIVE.clear()


def test_bank_unique_human_readable_and_no_answer_leak():
    assert len(game.BANK) == 220
    assert len({game._norm(item['answer']) for item in game.BANK}) == 220
    assert len({item['description'] for item in game.BANK}) == 220
    assert len({item['clues'] for item in game.BANK}) == 220
    for item in game.BANK:
        assert len(item['clues']) == 3 and all(item['clues'])
        assert len(item['description']) >= 20
        if len(game._norm(item['answer'])) >= 3:
            assert game._norm(item['answer']) not in game._norm(item['description']), item
            assert game._norm(item['answer']) not in game._norm(' '.join(item['clues'])), item


@pytest.mark.parametrize('command', ['سرنخ', 'کلمه ناقص', 'کلمه مرتبط'])
def test_all_220_stages_never_repeat_even_on_timeout_and_wrong(command, monkeypatch):
    # Memory implementation keeps this exhaustive test cheap; persistence is
    # independently tested with the real economy store below.
    seen = {}
    monkeypatch.setattr(game_progress, 'seen', lambda chat, user, key: set(seen.get((chat, user, key), set())))
    def mark(chat, user, key, value):
        seen.setdefault((chat, user, key), set()).add(value)
        return len(seen[(chat, user, key)])
    monkeypatch.setattr(game_progress, 'mark_seen', mark)
    indices, prompts = [], []
    for expected in range(1, 221):
        state = game.start(CHAT, 42, command)
        assert state['stage'] == expected
        assert game.bind_prompt(CHAT, 42, state['token'], expected)
        assert game.answer(CHAT, 42, 'جواب اشتباه') is None
        assert game.current(CHAT, 42)['index'] == state['index']
        indices.append(state['index'])
        prompts.append(game.payload(state)[0])
        assert game.finish(CHAT, 42, state['token']) == state['item']['answer']
    assert len(set(indices)) == len(set(prompts)) == 220
    assert game.start(CHAT, 42, command)['kind'] == 'finished'


@pytest.mark.parametrize('command,amount', [('سرنخ', 4), ('کلمه ناقص', 4), ('کلمه مرتبط', 5)])
def test_real_economy_exactly_once_and_private_owner(command, amount):
    state = game.start(CHAT, 101, command)
    assert game.bind_prompt(CHAT, 101, state['token'], 50)
    assert game.answer(CHAT, 202, state['item']['answer']) is None
    assert economy.get_balance(CHAT, 202)[economy.BRONZE] == 0
    assert game.current(CHAT, 101)
    assert game.answer(CHAT, 101, 'غلط') is None
    assert game.current(CHAT, 101)
    result = game.answer(CHAT, 101, state['item']['answer'])
    assert not result['payment_error']
    assert economy.get_balance(CHAT, 101)[economy.BRONZE] == amount
    assert game.answer(CHAT, 101, state['item']['answer']) is None
    assert game.finish(CHAT, 101, state['token']) is None
    assert economy.get_balance(CHAT, 101)[economy.BRONZE] == amount


def test_progress_is_independent_and_survives_storage_reload():
    first = game.start(CHAT, 101, 'سرنخ')
    assert game.bind_prompt(CHAT, 101, first['token'], 500)
    game.finish(CHAT, 101, first['token'])
    storage.flush()
    storage._cache = None
    storage._cache_mtime = None
    second = game.start(CHAT, 101, 'سرنخ')
    assert second['stage'] == 2 and second['index'] != first['index']
    game.finish(CHAT, 101, second['token'])
    other = game.start(CHAT, 101, 'کلمه ناقص')
    assert other['stage'] == 1
    assert game_progress.seen_count(CHAT, 102, 'clue_guess') == 0


def test_parallel_players_and_cross_replies_cannot_steal():
    first = game.start(CHAT, 101, 'سرنخ')
    game.bind_prompt(CHAT, 101, first['token'], 501)
    second = game.start(CHAT, 202, 'سرنخ')
    game.bind_prompt(CHAT, 202, second['token'], 502)
    assert first['index'] != second['index']
    assert game.answer(CHAT, 202, first['item']['answer'], reply_id=501) is None
    assert game.answer(CHAT, 202, second['item']['answer'], reply_id=501) is None
    assert game.current(CHAT, 101) and game.current(CHAT, 202)
    assert game.answer(CHAT, 202, second['item']['answer'], reply_id=502)
    assert game.current(CHAT, 101)


def test_late_answer_and_old_timer_cannot_affect_next():
    first = game.start(CHAT, 101, 'سرنخ')
    game.bind_prompt(CHAT, 101, first['token'], 1)
    game.current(CHAT, 101)['deadline'] = 0
    assert game.answer(CHAT, 101, first['item']['answer']) is None
    assert game.finish(CHAT, 101, first['token']) == first['item']['answer']
    second = game.start(CHAT, 101, 'سرنخ')
    game.bind_prompt(CHAT, 101, second['token'], 2)
    assert game.finish(CHAT, 101, first['token']) is None
    assert game.current(CHAT, 101)['token'] == second['token']


@pytest.mark.parametrize('command,header', [('سرنخ','🔍 سرنخ'), ('کلمه ناقص','♟️ کلمه ناقص'), ('کلمه مرتبط','🦊 کلمه مرتبط')])
def test_screenshot_quote_entity_and_timer(command, header):
    state = game.start(CHAT, 101, command)
    text, entities = game.payload(state)
    assert text.startswith(header + '\n\n') and '۳۰ ثانیه مهلت' in text
    assert len(entities) == 1 and isinstance(entities[0], MessageEntityBlockquote)
    e = entities[0]
    encoded = text.encode('utf-16-le')
    assert encoded[e.offset*2:(e.offset+e.length)*2].decode('utf-16-le') == header


def test_incomplete_mask_keeps_length_and_reconstructs_answer():
    for i, item in enumerate(game.BANK):
        answer = item['answer'].replace('\u200c', ' ')
        masked = game._masked(answer, i)
        actual = [c for c in answer if c.isalpha()]
        display = [c for c in masked if c.isalpha() or c == 'ـ']
        assert len(actual) == len(display) and 'ـ' in display
        assert all(shown == 'ـ' or shown == original for original, shown in zip(actual, display))


class Event:
    def __init__(self, private=False):
        self.is_private = private
        self.replies = []
    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return NS(id=1000 + len(self.replies))


def test_real_adapter_timeout_and_next_stage(monkeypatch):
    async def go():
        monkeypatch.setattr(game, 'TIME_LIMIT', .01)
        bot = NS(group_timer_tasks={})
        sender = NS(first_name='کاربر', last_name='', username=None)
        event = Event()
        def track(bot, chat, task):
            bot.group_timer_tasks.setdefault(chat, set()).add(task)
        kwargs = dict(busy=False, is_command=lambda text: False, track_timer=track)
        assert await adapter.route(bot, event, CHAT, 101, sender, 'سرنخ', **kwargs)
        first = game.current(CHAT, 101)
        assert first and 'سرنخ' in event.replies[0][0]
        assert await adapter.route(bot, event, CHAT, 202, sender, first['item']['answer'], **kwargs) is False
        await asyncio.gather(*bot.group_timer_tasks[CHAT])
        assert first['item']['answer'] in event.replies[-1][0]
        assert await adapter.route(bot, event, CHAT, 101, sender, 'سرنخ', **kwargs)
        assert game.current(CHAT, 101)['stage'] == 2
        for task in bot.group_timer_tasks[CHAT]: task.cancel()
        await asyncio.gather(*bot.group_timer_tasks[CHAT], return_exceptions=True)
    asyncio.run(go())


def test_disabled_games_cannot_start_via_public_guard():
    for command in game.GAMES:
        assert entertainment_control.is_game_command(command)


def test_list_source_contains_exact_user_text_and_bold_entities():
    from pathlib import Path
    src = (Path(__file__).parents[1] / 'handlers/message_handler.py').read_text()
    for text in ['⛵سرنخ', '🗻کلمه ناقص', '🧳کلمه مرتبط']:
        assert src.count(text) == 2
    assert 'با سرنخ های که بهتون میدن جمله رو بگید' in src
    assert 'کلمه های ناقص رو کامل کن و جمله رو بگو' in src
    assert 'با کلمه های که بهت میگن حدس بزن' in src


@pytest.mark.parametrize('command', ['سرنخ', 'کلمه ناقص', 'کلمه مرتبط'])
def test_actual_group_handler_routes_game_and_pays_only_owner(tmp_path, monkeypatch, command):
    from handlers import message_handler as handler
    from test_native_admin_warning_period import _setup_scenario
    from test_bio_link_guard import FakeEvent
    chat = -10012345
    bot, _, _, _ = _setup_scenario(tmp_path, monkeypatch, chat)
    monkeypatch.setattr(handler, '_chat_game_busy', lambda chat: False)
    monkeypatch.setattr(handler, 'fox_game_active', lambda chat: False)
    monkeypatch.setattr(handler, 'emoji_guess_active', lambda *args: False)
    monkeypatch.setattr(handler, 'word_puzzle_active', lambda *args: False)
    monkeypatch.setattr(handler, 'jadvalestan_active', lambda *args: False)
    monkeypatch.setattr(handler, 'get_fill_token', lambda *args: None)
    monkeypatch.setattr(handler, 'get_riddle_token', lambda *args: None)
    owner = NS(id=101, first_name='کاربر', last_name='', username='ordinary_101')
    outsider = NS(id=202, first_name='دیگری', last_name='', username='ordinary_202')
    async def go():
        first = FakeEvent(chat, owner, command, 111)
        await handler.handle_new_message(bot, first)
        state = game.current(chat, 101)
        assert state is not None, (first.replies, bot.logger)
        assert first.replies and isinstance(first.replies[0][1]['formatting_entities'][0], MessageEntityBlockquote)
        attempted = FakeEvent(chat, outsider, state['item']['answer'], 112)
        await handler.handle_new_message(bot, attempted)
        assert game.current(chat, 101) is not None
        assert economy.get_balance(chat, 202)[economy.BRONZE] == 0
        answer = FakeEvent(chat, owner, state['item']['answer'], 113)
        await handler.handle_new_message(bot, answer)
        assert game.current(chat, 101) is None
        assert economy.get_balance(chat, 101)[economy.BRONZE] == game.GAMES[command][2]
        tasks = list(bot.group_timer_tasks.get(chat, ()))
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(go())


def test_actual_game_list_bold_and_exact_text(tmp_path, monkeypatch):
    from handlers import message_handler as handler
    from test_native_admin_warning_period import _setup_scenario
    from test_bio_link_guard import FakeEvent
    from splusthon.tl.types import MessageEntityBold
    chat = -10012345
    bot, _, _, _ = _setup_scenario(tmp_path, monkeypatch, chat)
    user = NS(id=101, first_name='کاربر', last_name='', username='ordinary_101')
    async def go():
        event = FakeEvent(chat, user, 'لیست بازی', 400)
        await handler.handle_new_message(bot, event)
        assert event.replies
        text, options = event.replies[-1]
        encoded = text.encode('utf-16-le')
        bold = [encoded[e.offset*2:(e.offset+e.length)*2].decode('utf-16-le')
                for e in options['formatting_entities'] if isinstance(e, MessageEntityBold)]
        assert all(word in bold for word in ['⛵سرنخ', '🗻کلمه ناقص', '🧳کلمه مرتبط'])
    asyncio.run(go())


def test_send_failure_does_not_mark_unseen_or_leave_session():
    class FailingEvent(Event):
        async def reply(self, *args, **kwargs): raise OSError('send failure')
    async def go():
        with pytest.raises(OSError):
            await adapter.route(NS(), FailingEvent(), CHAT, 101, NS(first_name='کاربر'), 'سرنخ',
                                busy=False, is_command=lambda _: False, track_timer=lambda *a: None)
        assert not game.current(CHAT, 101)
        assert not game_progress.seen(CHAT, 101, 'clue_guess')
    asyncio.run(go())


def test_timer_cancellation_closes_stale_session():
    async def go():
        bot = NS(group_timer_tasks={})
        def track(bot, chat, task): bot.group_timer_tasks.setdefault(chat, set()).add(task)
        await adapter.route(bot, Event(), CHAT, 101, NS(first_name='کاربر'), 'سرنخ',
                            busy=False, is_command=lambda _: False, track_timer=track)
        task = next(iter(bot.group_timer_tasks[CHAT]))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert game.current(CHAT, 101) is None
        assert game_progress.seen_count(CHAT, 101, 'clue_guess') == 1
    asyncio.run(go())


def test_no_canonical_answer_is_swallowed_as_bot_command():
    from handlers import message_handler
    for item in game.BANK:
        assert not message_handler._game_text_is_command(item['answer']), item['answer']
        assert not entertainment_control.is_game_command(item['answer']), item['answer']


def test_dispatcher_fast_lane_recognizes_starts_and_only_owners_answers():
    from modules import group_dispatch
    for command in game.GAMES:
        assert group_dispatch.classify_priority(command)[1] == group_dispatch.LANE_COMMAND
        assert group_dispatch.is_game_message(command)
    state = game.start(CHAT, 101, 'سرنخ')
    game.bind_prompt(CHAT, 101, state['token'], 1010)
    own = NS(chat_id=CHAT, sender_id=101, is_private=False)
    other = NS(chat_id=CHAT, sender_id=202, is_private=False)
    assert group_dispatch.classify_priority(state['item']['answer'], own)[1] == group_dispatch.LANE_COMMAND
    assert group_dispatch.is_game_message(state['item']['answer'], own)
    assert group_dispatch.classify_priority(state['item']['answer'], other)[1] == group_dispatch.LANE_NORMAL


def test_core_priority_dispatch_reaches_real_game_and_pays(tmp_path, monkeypatch):
    from test_economy_routing import build_handler, Event as CoreEvent
    from modules import group_storage
    from handlers import message_handler as handler
    from core import bot_working_split_ok as core
    chat = CHAT
    monkeypatch.setattr(core, '_group_subscription_gate', lambda e: (False, 'active', '', chat, e.sender_id))
    monkeypatch.setattr(core, 'is_active', lambda chat: True)
    monkeypatch.setattr(handler, '_chat_game_busy', lambda chat: False)
    monkeypatch.setattr(handler, 'fox_game_active', lambda chat: False)
    monkeypatch.setattr(handler, 'emoji_guess_active', lambda *args: False)
    monkeypatch.setattr(handler, 'word_puzzle_active', lambda *args: False)
    monkeypatch.setattr(handler, 'jadvalestan_active', lambda *args: False)
    monkeypatch.setattr(handler, 'get_fill_token', lambda *args: None)
    monkeypatch.setattr(handler, 'get_riddle_token', lambda *args: None)
    async def go():
        bot, on_message = await build_handler()
        event = CoreEvent('سرنخ', 101, chat)
        event.sender_id = 101
        await on_message(event)
        state = game.current(chat, 101)
        assert state is not None, (event.replies, bot.logger.errors[-3:])
        answer = CoreEvent(state['item']['answer'], 101, chat)
        answer.sender_id = 101
        await on_message(answer)
        assert game.current(chat, 101) is None, (answer.replies, bot.logger.errors[-3:])
        assert economy.get_balance(chat, 101)[economy.BRONZE] == 4
        tasks = list(bot.group_timer_tasks.get(chat, ()))
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(go())


def test_clue_game_shows_three_clues_as_in_sample():
    state = game.start(CHAT, 101, 'سرنخ')
    text, _ = game.payload(state)
    clue_line = text.splitlines()[2]
    assert clue_line.count(' « ') == 2
    assert all(part.strip() for part in clue_line.split(' « '))
    assert 'من چی هستم؟؟' in text
