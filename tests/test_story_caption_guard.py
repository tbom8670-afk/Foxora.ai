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
from modules.profile_guard import story_guard as story, guard, storage, texts


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
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
        assert recorded and recorded[0][2] == 'story'
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


def test_handler_schedules_after_profile_guard():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'handlers/message_handler.py').read_text(encoding='utf-8')
    assert 'elif _profile_guard.storage.is_enabled(chat_id):' in source
    assert '_story_guard.schedule(' in source


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
