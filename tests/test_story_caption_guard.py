"""Offline regression tests for the experimental visible story caption guard."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from splusthon.tl.functions.stories import GetPeerStoriesRequest, GetStoriesByIDRequest
from splusthon.tl.types import StoryItemSkipped
from modules import native_admin_exemption as native
_REAL_IS_NATIVE_ADMIN = native.is_native_admin
from modules.profile_guard import story_guard as story, guard, story_storage as storage, texts
from modules.manage_features.queue import PerGroupQueue
_REAL_STORY_ENABLED = storage.is_enabled


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(story, 'QUEUE', PerGroupQueue('story_test', max_pending_per_key=32, max_pending_total=128, max_keys=128, task_timeout=90))
    monkeypatch.setattr(story, '_inflight', set())
    monkeypatch.setattr(story, '_retry_after', {})
    monkeypatch.setattr(story, '_sem', None)
    monkeypatch.setattr(story, '_last_request', 0.0)
    monkeypatch.setattr(story, '_flood_until', 0.0)
    monkeypatch.setattr(storage, 'is_enabled', lambda group: True)
    monkeypatch.setattr(guard, '_is_exempt', lambda *args: False)
    monkeypatch.setenv('BOT_NATIVE_ADMIN_EXEMPT', '1')
    async def member_role(bot, chat_id, user_id, user=None, resolved_chat=None, *, fresh=False):
        return False
    # Keep the real module and a signature-compatible role checker.
    monkeypatch.setattr(native, 'is_native_admin', member_role)


def result(*items):
    return NS(stories=NS(stories=list(items)))


def item(caption, seconds=600):
    return NS(caption=caption, expire_date=datetime.now(timezone.utc) + timedelta(seconds=seconds))


@pytest.mark.parametrize('caption', ['کانالم', 'بیا تو كانالم 🌹', 'ک.ا.ن.ا.ل.م'])
def test_caption_match(caption):
    assert story.matching_caption(result(item(caption))) == caption


def test_no_match_for_skipped_empty_expired_or_unrelated():
    assert story.matching_caption(result(NS(id=1), item(None), item('سلام'), item('کانالم', -1))) is None
    assert story.matching_caption(NS(stories=None)) is None


class Client:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.deleted = []
    async def get_input_entity(self, sender):
        return sender
    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response
    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, ids))


def make_bot(response):
    return NS(client=Client(response), bot_account_id=999,
              admin_actions=NS(mute_user=AsyncMock(return_value=True)))


async def run_check(bot, monkeypatch):
    sent = []
    recorded = []
    async def send(bot, event, group, payload, notice=False):
        sent.append((payload, notice))
    monkeypatch.setattr(guard, '_send', send)
    from modules.profile_guard import penalties
    monkeypatch.setattr(penalties, 'record', lambda *args: recorded.append(args))
    sender = NS(id=22, first_name='کاربر', last_name='', username=None)
    event = NS(is_private=False)
    scheduled = time.monotonic()
    await story._run(bot, event, 11, 22, sender, 33, ('11', '22'), scheduled)
    return sent, recorded


def test_full_matching_request_punishment_and_caption(monkeypatch):
    async def go():
        bot = make_bot(result(item('بیا تو کانالم دوستان')))
        sent, recorded = await run_check(bot, monkeypatch)
        assert len(bot.client.requests) == 1
        assert isinstance(bot.client.requests[0], GetPeerStoriesRequest)
        assert bot.client.deleted == [(11, [33])]
        assert bot.admin_actions.mute_user.await_args.args[:3] == (11, 22, guard.bio_mute_seconds())
        assert not recorded  # Do not overwrite anti-channel penalty state.
        assert sent[0][1] is True
        text, entities = sent[0][0]
        assert 'کپشن استوری : بیا تو کانالم دوستان' in text
        assert 'کپشن استوری فیلتر شده' in text and 'سکوت' in text
        assert any(isinstance(e, texts.MessageEntityBlockquote) for e in entities)
    asyncio.run(go())


@pytest.mark.parametrize('response', [result(item('سلام')), result(NS(id=1)), RuntimeError('no access')])
def test_unknown_and_clean_never_punish(response, monkeypatch):
    async def go():
        bot = make_bot(response)
        sent, recorded = await run_check(bot, monkeypatch)
        assert not bot.client.deleted and not sent and not recorded
        bot.admin_actions.mute_user.assert_not_awaited()
    asyncio.run(go())


def test_permissions_switch_and_failure_are_honest(monkeypatch):
    async def go():
        bot = make_bot(result(item('کانالم')))
        monkeypatch.setattr(storage, 'is_enabled', lambda group: False)
        sent, recorded = await run_check(bot, monkeypatch)
        assert not bot.client.requests and not sent
        monkeypatch.setattr(storage, 'is_enabled', lambda group: True)
        async def fail(*a): raise RuntimeError('denied')
        bot.client.delete_messages = fail
        bot.admin_actions.mute_user = AsyncMock(return_value=False)
        sent, recorded = await run_check(bot, monkeypatch)
        assert not recorded
        assert 'حذف پیام تأیید نشد' in sent[0][0][0]
        assert 'سکوت اعمال نشد' in sent[0][0][0]
    asyncio.run(go())


def test_scheduling_is_background_and_deduplicated(monkeypatch):
    async def go():
        bot = make_bot(result(item('سلام')))
        event = NS(is_private=False)
        sender = NS(id=22, first_name='کاربر', last_name='', username=None)
        assert story.schedule(bot, event, 11, 22, sender, 33)
        assert not story.schedule(bot, event, 11, 22, sender, 34)
        for _ in range(50):
            if not story._inflight:
                break
            await asyncio.sleep(.01)
        assert not story._inflight
        assert len(bot.client.requests) == 1
        assert not story.schedule(bot, event, 11, 22, sender, 35)
    asyncio.run(go())


def test_group_disabled_before_action_and_native_admin(monkeypatch):
    async def go():
        bot = make_bot(result(item('کانالم')))
        calls = iter([True, False])
        monkeypatch.setattr(storage, 'is_enabled', lambda group: next(calls))
        sent, recorded = await run_check(bot, monkeypatch)
        assert not sent and not recorded and not bot.client.deleted
        monkeypatch.setattr(storage, 'is_enabled', lambda group: True)
        async def admin_role(bot, chat_id, user_id, user=None, resolved_chat=None, *, fresh=False):
            return True
        monkeypatch.setattr(native, 'is_native_admin', admin_role)
        bot2 = make_bot(result(item('کانالم')))
        sent, recorded = await run_check(bot2, monkeypatch)
        assert not bot2.client.requests and not sent
    asyncio.run(go())


def test_flood_stops_other_story_checks(monkeypatch):
    class FloodWait(Exception):
        seconds = 20
    async def go():
        bot = make_bot(FloodWait())
        await run_check(bot, monkeypatch)
        assert story._flood_until > time.monotonic()
        assert not story.schedule(bot, NS(is_private=False), 12, 55, NS(id=55), 33)
        assert not bot.client.deleted
    asyncio.run(go())


def test_old_queued_request_never_hits_server(monkeypatch):
    async def go():
        bot = make_bot(result(item('کانالم')))
        sent = []
        async def send(*args, **kwargs): sent.append(args)
        monkeypatch.setattr(guard, '_send', send)
        await story._run(bot, NS(is_private=False), 11, 22, NS(id=22), 33,
                         ('11','22'), time.monotonic() - 61)
        assert not bot.client.requests and not bot.client.deleted and not sent
    asyncio.run(go())


def test_story_caption_entity_offsets_handle_emoji():
    text, entities = texts.notice_story('@test', '🌹 کانالم 🌹', duration_seconds=14400,
                                       deleted=True, muted=True)
    encoded = text.encode('utf-16-le')
    for entity in entities:
        assert encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode('utf-16-le')
    assert '🌹 کانالم 🌹' in text


def test_handler_schedules_independently_of_profile_guard():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'handlers/message_handler.py').read_text(encoding='utf-8')
    assert 'elif _story_storage.is_enabled(chat_id):' in source
    assert source.index('_story_guard.schedule(') < source.index('# ===== 🦊 گیت «ضد کانال')


def test_real_native_role_path_accepts_fresh_and_checks_authority(monkeypatch):
    """Exercise the real role checker, with only server calls faked."""
    async def go():
        monkeypatch.setattr(native, 'is_native_admin', _REAL_IS_NATIVE_ADMIN)
        async def no_list(*args, **kwargs): return None
        monkeypatch.setattr(native, 'fetch_group_admin_ids', no_list)
        bot = make_bot(result(item('کانالم')))
        bot.client.get_permissions = AsyncMock(return_value=NS(is_admin=False, is_creator=False))
        sent, _ = await run_check(bot, monkeypatch)
        assert bot.client.get_permissions.await_count >= 1
        assert len(bot.client.requests) == 1
        assert sent and bot.client.deleted == [(11, [33])]
        bot2 = make_bot(result(item('کانالم')))
        bot2.client.get_permissions = AsyncMock(return_value=NS(is_admin=True, is_creator=False))
        sent2, _ = await run_check(bot2, monkeypatch)
        assert not bot2.client.requests and not bot2.client.deleted and not sent2
    asyncio.run(go())


def test_skipped_story_is_fetched_by_id_before_matching(monkeypatch):
    async def go():
        skipped = StoryItemSkipped(id=42, date=datetime.now(timezone.utc),
                                   expire_date=datetime.now(timezone.utc) + timedelta(minutes=5))
        bot = make_bot(result(skipped))
        async def request(req):
            bot.client.requests.append(req)
            if isinstance(req, GetPeerStoriesRequest):
                return result(skipped)
            assert isinstance(req, GetStoriesByIDRequest) and req.id == [42]
            return NS(stories=[item('کانالم')])
        # Special methods resolve on the class, not the instance.
        class SkippedClient(Client):
            async def __call__(self, req): return await request(req)
        bot.client = SkippedClient(None)
        sent, _ = await run_check(bot, monkeypatch)
        assert len(bot.client.requests) == 2 and sent
        assert 'کانالم' in sent[0][0][0]
    asyncio.run(go())


def test_complete_live_channel_and_bio_lists_plus_requested_words():
    from modules.profile_guard import filters
    terms = story.filter_terms()
    assert set(filters.channel_names()) <= set(terms)
    assert set(filters.bio_sentences()) <= set(terms)
    for phrase in ('داغم', 'داغ', 'حشری', 'فیلم سوپر', 'ربات', 'ربات فیلم', 'منتظرتم بیا', 'فروش فیلم'):
        assert phrase in terms
        assert story.caption_match(phrase), phrase
    for phrase in terms:
        assert story.caption_match(phrase), phrase


@pytest.mark.parametrize('caption', [
    'د ا ا ا غ غ م', 'دــااااغــم', 'د-ا-غ-م', 'د_ا_غ_م',
    'حشششری', 'ح.ش.ش.ش.ر.ی', 'فیییلم سوووپر', 'ف-ی-ل-م س-و-پ-ر',
    'بیا تو كــااااناللللم', 'چ.ن.ل.م',
])
def test_obfuscated_captions(caption):
    assert story.caption_match(caption), caption


def test_matching_avoids_several_unrelated_longer_words():
    for phrase in ('داغستان', 'یک کتاب خوب', 'فيلمبرداری', 'کسی آمد'):
        assert not story.caption_match(phrase), phrase


def test_filter_lists_update_without_restart(monkeypatch):
    monkeypatch.setattr(story.filters, 'channel_names', lambda: ['عبارت اختصاصی تازه'])
    monkeypatch.setattr(story.filters, 'bio_sentences', lambda: ['بیو اختصاصی تازه'])
    assert story.caption_match('عبارت اختصاصی تازه')
    assert story.caption_match('بیو اختصاصی تازه')
    monkeypatch.setattr(story.filters, 'channel_names', lambda: ['عبارت جدید دوم'])
    assert story.caption_match('عبارت جدید دوم')
    assert not story.caption_match('عبارت اختصاصی تازه')


def test_independent_group_switch_and_commands(tmp_path, monkeypatch):
    # Use the real persistence layer rather than the fixture's always-on stub.
    from modules.profile_guard import story_storage, storage as channel_storage
    monkeypatch.setattr(story_storage, 'is_enabled', _REAL_STORY_ENABLED)
    monkeypatch.setattr(story_storage, 'FILE', tmp_path / 'story.json')
    monkeypatch.setattr(story_storage, '_cache', None)
    monkeypatch.setattr(story_storage, '_cache_mtime', None)
    monkeypatch.setattr(channel_storage, 'FILE', tmp_path / 'channel.json')
    monkeypatch.setattr(channel_storage, '_cache', None)
    monkeypatch.setattr(channel_storage, '_cache_mtime', None)
    assert story_storage.toggle_mode('ضد استوری روشن') == 'on'
    assert story_storage.toggle_mode('ضد استوری خاموش') == 'off'
    assert story_storage.toggle_mode('ضد کانال روشن') is None
    assert not story_storage.is_enabled('1')
    story_storage.enable('1')
    assert story_storage.is_enabled('1') and not story_storage.is_enabled('2')
    assert not channel_storage.is_enabled('1')
    channel_storage.enable('2')
    assert not story_storage.is_enabled('2')
    story_storage.disable('1')
    assert not story_storage.is_enabled('1') and channel_storage.is_enabled('2')


def test_story_toggle_is_admin_only_and_uses_profile_style(monkeypatch):
    async def go():
        sent = []
        evt = NS(reply=AsyncMock())
        bot = NS()
        async def fake_send(bot, event, chat, payload, **kw): sent.append(payload)
        monkeypatch.setattr(guard, '_send', fake_send)
        monkeypatch.setattr(guard, '_is_admin', lambda *args: False)
        on = AsyncMock()
        monkeypatch.setattr(storage, 'enable', on)
        assert await story.handle_toggle(bot, evt, 11, 22, NS(), 'on')
        evt.reply.assert_awaited_once()
        on.assert_not_called()
        monkeypatch.setattr(guard, '_is_admin', lambda *args: True)
        enabled = []
        monkeypatch.setattr(storage, 'enable', lambda gid: enabled.append(gid))
        await story.handle_toggle(bot, evt, 11, 22, NS(), 'on')
        assert enabled == [11]
        assert 'ضد استوری' in sent[-1][0]
        assert any(isinstance(e, texts.MessageEntityBold) for e in sent[-1][1])
        disabled = []
        monkeypatch.setattr(storage, 'disable', lambda gid: disabled.append(gid))
        await story.handle_toggle(bot, evt, 11, 22, NS(), 'off')
        assert disabled == [11] and 'خاموش شد' in sent[-1]
    asyncio.run(go())


def test_story_queue_is_dedicated_and_serializes_same_group(monkeypatch):
    async def go():
        calls = []
        first_waiting, release = asyncio.Event(), asyncio.Event()
        async def run(bot, event, gid, uid, sender, mid, key, scheduled):
            calls.append(('start', uid))
            if uid == 1:
                first_waiting.set()
                await release.wait()
            calls.append(('end', uid))
        monkeypatch.setattr(story, '_run', run)
        bot = NS()
        evt = NS(is_private=False)
        assert story.schedule(bot, evt, 11, 1, NS(id=1), 101)
        await asyncio.wait_for(first_waiting.wait(), 1)
        assert story.schedule(bot, evt, 11, 2, NS(id=2), 102)
        await asyncio.sleep(.02)
        assert calls == [('start', 1)]
        release.set()
        await story.QUEUE.drain()
        assert calls == [('start', 1), ('end', 1), ('start', 2), ('end', 2)]
        assert story.QUEUE.name == 'story_test'
    asyncio.run(go())


def test_real_message_handler_routes_toggle_and_independent_check(tmp_path, monkeypatch):
    from handlers import message_handler
    from test_native_admin_warning_period import _setup_scenario
    from test_bio_link_guard import FakeEvent
    from modules.profile_guard import storage as channel_storage
    monkeypatch.setattr(storage, 'is_enabled', _REAL_STORY_ENABLED)
    monkeypatch.setattr(storage, 'FILE', tmp_path / 'stories.json')
    monkeypatch.setattr(storage, '_cache', None)
    monkeypatch.setattr(storage, '_cache_mtime', None)
    monkeypatch.setattr(channel_storage, 'is_enabled', lambda chat: False)
    chat = -10012345
    bot, deleted, tracker, actions = _setup_scenario(tmp_path, monkeypatch, chat)
    monkeypatch.setattr(guard, '_is_admin', lambda group, uid, sender: uid == 1001)
    sender = NS(id=1001, username='owner', first_name='مالک', last_name='')
    user = NS(id=223344, username='member', first_name='کاربر', last_name='')
    calls = []
    monkeypatch.setattr(story, 'schedule', lambda *args: calls.append(args) or True)
    async def go():
        await message_handler.handle_new_message(bot, FakeEvent(chat, sender, 'ضد استوری روشن', 20))
        assert storage.is_enabled(chat)
        assert any('ضد استوری' in x[1] and 'فعال شد' in x[1] for x in bot.sent_notices)
        await message_handler.handle_new_message(bot, FakeEvent(chat, user, 'سلام معمولی', 21))
        assert len(calls) == 1 and calls[0][2] == chat and calls[0][3] == user.id
        await message_handler.handle_new_message(bot, FakeEvent(chat, sender, 'ضد استوری خاموش', 22))
        assert not storage.is_enabled(chat)
        await message_handler.handle_new_message(bot, FakeEvent(chat, user, 'سلام معمولی', 23))
        assert len(calls) == 1
    asyncio.run(go())


def test_each_list_term_resists_repetition_and_dash_on_its_own():
    for phrase in story.filter_terms():
        original, folded = story._patterns((phrase,))
        decorated = '-'.join(ch * 3 for ch in phrase if ch.isalnum())
        assert (story.matcher.find_match(decorated, original)
                or story.matcher.find_match(story._fold_stretched(decorated), folded)), phrase


@pytest.mark.parametrize('phrase,decorated', [
    ('ربات', 'ر-بـااات'),
    ('ربات فیلم', 'ر.ب.ا.ت ف.ی.ل.م'),
    ('منتظرتم بیا', 'ممم.ن.ت.ظ.ر.ت.م ب-ی-ا'),
    ('فروش فیلم', 'فــرروووش ف.ی.ل.م'),
])
def test_new_requested_terms_individually_and_obfuscated(phrase, decorated, monkeypatch):
    # Isolate each phrase so an overlapping shorter existing term cannot hide a bug.
    monkeypatch.setattr(story, 'filter_terms', lambda: (phrase,))
    assert story.matching_caption(result(item(phrase))) == phrase
    assert story.matching_caption(result(item(decorated))) == decorated


def test_robot_term_is_not_a_substring_of_longer_unrelated_word(monkeypatch):
    monkeypatch.setattr(story, 'filter_terms', lambda: ('ربات',))
    assert not story.caption_match('رباتیک')


@pytest.mark.parametrize('caption', [
    'ببین https://example.com/a', 'http://site.ir', 'example.org',
    'example.photography', 'https://example.app', 'splus.ir',
    'splus.ir/c/25372045/841', 't.me/example', 'sapp.ir/example',
    'www.example.com', 'example[.]com', 's p l u s . i r / example',
    'https://localhost:8080/path', 'tg://resolve?domain=example',
    'https://مثال.ایران/خبر', 'مثال.ایران',
])
def test_link_only_captions_trigger(caption, monkeypatch):
    # No filter terms: a prior match must not hide missing link detection.
    monkeypatch.setattr(story, 'filter_terms', lambda: ())
    assert story.caption_match(caption), caption
    assert story.matching_caption(result(item(caption))) == caption


@pytest.mark.parametrize('caption', [
    'سلام دوستان', 'فقط @username', 'contact@example.com', 'عکس image.jpg',
    'نسخه 1.0.0.0', 'http://', 'یادداشت file.pdf',
])
def test_not_a_link_alone(caption, monkeypatch):
    monkeypatch.setattr(story, 'filter_terms', lambda: ())
    assert not story.caption_match(caption), caption


def test_hidden_caption_link_entities_and_expiry(monkeypatch):
    from splusthon.tl.types import MessageEntityTextUrl, MessageEntityUrl, MessageEntityBold
    monkeypatch.setattr(story, 'filter_terms', lambda: ())
    hidden = item('اینجا را ببینید')
    hidden.entities = [MessageEntityTextUrl(offset=0, length=4, url='https://example.com')]
    assert story.matching_caption(result(hidden)) == hidden.caption
    hidden.expire_date = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert story.matching_caption(result(hidden)) is None
    visible = item('نشانی')
    visible.entities = [MessageEntityUrl(offset=0, length=5)]
    assert story.matching_caption(result(visible)) == visible.caption
    visible.entities = [MessageEntityBold(offset=0, length=5)]
    assert story.matching_caption(result(visible)) is None


def test_link_only_uses_existing_story_warning_and_moderation(monkeypatch):
    async def go():
        monkeypatch.setattr(story, 'filter_terms', lambda: ())
        bot = make_bot(result(item('https://example.org/profile')))
        sent, recorded = await run_check(bot, monkeypatch)
        assert bot.client.deleted == [(11, [33])]
        bot.admin_actions.mute_user.assert_awaited_once()
        assert sent[0][1] is True
        assert 'https://example.org/profile' in sent[0][0][0]
        assert not recorded
    asyncio.run(go())


def test_link_only_does_not_bypass_disabled_or_admin_exemption(monkeypatch):
    async def go():
        monkeypatch.setattr(story, 'filter_terms', lambda: ())
        bot = make_bot(result(item('https://example.org')))
        monkeypatch.setattr(storage, 'is_enabled', lambda group: False)
        sent, _ = await run_check(bot, monkeypatch)
        assert not bot.client.requests and not bot.client.deleted and not sent
        monkeypatch.setattr(storage, 'is_enabled', lambda group: True)
        async def admin(*args, **kwargs): return True
        monkeypatch.setattr(native, 'is_native_admin', admin)
        sent, _ = await run_check(bot, monkeypatch)
        assert not bot.client.requests and not bot.client.deleted and not sent
    asyncio.run(go())


def test_skipped_story_hidden_link_is_fetched_and_actioned(monkeypatch):
    from splusthon.tl.types import MessageEntityTextUrl
    async def go():
        monkeypatch.setattr(story, 'filter_terms', lambda: ())
        skipped = StoryItemSkipped(id=42, date=datetime.now(timezone.utc),
                                   expire_date=datetime.now(timezone.utc) + timedelta(minutes=5))
        linked = item('مشاهده')
        linked.entities = [MessageEntityTextUrl(offset=0, length=6, url='https://example.org')]
        class SkippedClient(Client):
            async def __call__(self, request):
                self.requests.append(request)
                if isinstance(request, GetPeerStoriesRequest):
                    return result(skipped)
                assert isinstance(request, GetStoriesByIDRequest)
                return NS(stories=[linked])
        bot = make_bot(None)
        bot.client = SkippedClient(None)
        sent, _ = await run_check(bot, monkeypatch)
        assert len(bot.client.requests) == 2
        assert bot.client.deleted == [(11, [33])]
        bot.admin_actions.mute_user.assert_awaited_once()
        assert sent and 'مشاهده' in sent[0][0][0]
    asyncio.run(go())
