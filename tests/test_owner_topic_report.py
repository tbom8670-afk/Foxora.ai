"""Offline tests of owner report routing, links, rich warning and privacy."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote, PeerChannel, PeerUser
from modules import owner_topic_report as report

OWNER = 123456
CHAT = -1000025372045


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(report, 'DB_PATH', tmp_path / 'reports.sqlite3')
    monkeypatch.setattr(report, 'get_owner', lambda: {'user_id': OWNER})
    monkeypatch.setattr(report, 'is_global_owner', lambda uid: str(uid) == str(OWNER))
    monkeypatch.setattr(report, '_pending', set())
    monkeypatch.setattr(report, '_tasks', set())
    monkeypatch.setattr(report, '_send_sem', None)
    monkeypatch.delenv('OWNER_TOPIC_REPORT_ENABLED', raising=False)


def u16(t): return len(t.encode('utf-16-le')) // 2


def slice_entity(text, entity):
    encoded = text.encode('utf-16-le')
    return encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode('utf-16-le')


@pytest.mark.parametrize('text', [
    'یاد جاویدنامان', '۱۸ دی', '19 دی', '۱۸ و ۱۹ دی', 'هجده دی',
    'بسیج', 'نیروی انتظامی', 'فراجا', 'جمهوری اسلامی', 'پهلوی',
    'رضاشاه', 'محمد رضا شاه', 'خامنه‌ای', 'خمینی', 'رئیسی',
    'پزشکیان', 'موساد', 'اسرائیل', 'Mossad', 'Israel',
])
def test_requested_topics_are_recognized_without_needing_an_insult(text):
    assert report.find_topic(text) is not None


def test_ordinary_words_not_overbroad():
    assert report.find_topic('سلام و روز بخیر') is None
    assert report.find_topic('محمد به ایران رفت') is None
    assert report.find_topic('شاهرخ') is None


class Event:
    def __init__(self, text='سلام', *, group=True, sender_id=44, out=False, mid=841):
        self.is_private = not group
        self.is_group = group
        self.is_channel = group
        self.out = out
        self.chat_id = CHAT if group else OWNER
        self.sender_id = sender_id
        self.sender = NS(id=sender_id, username='member', first_name='سارا', last_name='احمدی')
        self.chat = NS(id=25372045, title='گروه آزمایش', username='TestGroup') if group else NS(id=OWNER)
        self.message = NS(id=mid, message=text, entities=[],
                          peer_id=PeerChannel(25372045) if group else PeerUser(OWNER))
        self.reply_to = None
        self.replies = []
    async def get_chat(self): return self.chat
    async def get_sender(self): return self.sender
    async def reply(self, text, **kwargs): self.replies.append(text)


class Client:
    def __init__(self): self.sent = []
    async def send_message(self, target, text, **kwargs):
        self.sent.append((target, text, kwargs))
        return NS(id=9100 + len(self.sent))


def bot(): return NS(client=Client(), bot_account_id=999, logger=NS(log_info=lambda x: None, log_error=lambda x: None))


def test_user_supplied_group_message_link_pattern():
    event = Event('بسیج')
    assert report._links(event, event.chat, 841) == ('https://splus.ir/c/25372045/841', 'https://splus.ir/TestGroup')
    event.message.peer_id = NS(chat_id=999)
    event.is_channel = False
    assert report._links(event, NS(username=None), 841) == ('در دسترس نیست', 'در دسترس نیست')


def test_private_report_and_rich_warning_roundtrip(monkeypatch):
    async def scenario():
        from modules import owner_private
        monkeypatch.setattr(owner_private, 'resolve_private_owner_peer', AsyncMock(return_value='owner-peer'))
        b = bot()
        ev = Event('بحث درباره پهلوی')
        assert report.observe(b, ev)
        await asyncio.gather(*tuple(report._tasks))
        assert len(b.client.sent) == 1
        target, text, kwargs = b.client.sent[0]
        assert target == 'owner-peer'
        for value in ('🚨 گزارش کاربر', 'گروه آزمایش', '@member', 'بحث درباره پهلوی',
                      'سارا احمدی', 'https://splus.ir/c/25372045/841'):
            assert value in text
        assert slice_entity(text, kwargs['formatting_entities'][0]) == '🚨 گزارش کاربر'
        assert report.find_mapping(OWNER, 9101) == (CHAT, 841)
        # Multi-line formatting including emoji: both entity types retain the
        # right offset after the Persian command prefix is stripped.
        w = Event('هشدار: 🚨متن مهم\nخط نقل قول', group=False, sender_id=OWNER, mid=300)
        w.reply_to = NS(reply_to_msg_id=9101)
        pre = 'هشدار: '
        w.message.entities = [
            MessageEntityBold(offset=u16(pre), length=u16('🚨متن مهم')),
            MessageEntityBlockquote(offset=u16(pre + '🚨متن مهم\n'), length=u16('خط نقل قول')),
        ]
        assert await report.handle_owner_reply(b, w)
        group, text, opts = b.client.sent[-1]
        assert group == CHAT and text == '🚨متن مهم\nخط نقل قول'
        assert opts['reply_to'] == 841 and opts['parse_mode'] is None
        assert [slice_entity(text, e) for e in opts['formatting_entities']] == ['🚨متن مهم', 'خط نقل قول']
        assert w.replies == ['✅ هشدار به گروه ارسال شد.']
    asyncio.run(scenario())


def test_unauthorized_other_private_and_group_cannot_send_warning():
    async def scenario():
        b = bot()
        report.save_mapping(OWNER, 9101, CHAT, 841)
        for ev in [Event('هشدار: متن', group=False, sender_id=77),
                   Event('هشدار: متن', group=True, sender_id=OWNER)]:
            ev.reply_to = NS(reply_to_msg_id=9101)
            assert not await report.handle_owner_reply(b, ev)
        assert not b.client.sent
    asyncio.run(scenario())


def test_unknown_report_and_invalid_warning_never_send():
    async def scenario():
        b = bot()
        ev = Event('هشدار: متن', group=False, sender_id=OWNER)
        ev.reply_to = NS(reply_to_msg_id=404)
        assert not await report.handle_owner_reply(b, ev)
        report.save_mapping(OWNER, 9101, CHAT, 841)
        ev.reply_to = NS(reply_to_msg_id=9101)
        ev.message.message = 'هشدار:  '
        assert await report.handle_owner_reply(b, ev)
        assert not b.client.sent and 'متن هشدار' in ev.replies[-1]
    asyncio.run(scenario())


def test_clip_prefix_crossing_bold_and_quotes():
    raw = 'هشدار: متن نمونه'
    msg = NS(message=raw, entities=[MessageEntityBold(offset=0, length=u16(raw))])
    body, entities = report.warning_body(msg)
    assert body == 'متن نمونه'
    assert slice_entity(body, entities[0]) == body
    assert msg.entities[0].offset == 0 and msg.entities[0].length == u16(raw)


def test_failure_never_claims_warning_was_sent():
    async def scenario():
        b = bot()
        b.client.send_message = AsyncMock(side_effect=RuntimeError('no permission'))
        report.save_mapping(OWNER, 9101, CHAT, 841)
        ev = Event('هشدار: متن', group=False, sender_id=OWNER)
        ev.reply_to = NS(reply_to_msg_id=9101)
        assert await report.handle_owner_reply(b, ev)
        assert 'تأیید نشد' in ev.replies[-1]
    asyncio.run(scenario())


def test_observer_is_bounded_and_does_not_act_on_private_or_self(monkeypatch):
    async def scenario():
        b = bot()
        assert not report.observe(b, Event('پهلوی', group=False, sender_id=77))
        assert not report.observe(b, Event('پهلوی', group=True, out=True))
        assert not report.observe(b, Event('پهلوی', group=True, sender_id=999))
        assert not report.observe(b, Event('سلام', group=True))
        monkeypatch.setenv('OWNER_TOPIC_REPORT_ENABLED', '0')
        assert not report.observe(b, Event('پهلوی', group=True))
        assert not report._tasks
    asyncio.run(scenario())


def test_core_observer_precedes_subscription_gate():
    text = (Path(__file__).resolve().parents[1] / 'core/bot_working_split_ok.py').read_text(encoding='utf-8')
    start = text.index('async def new_message_handler(event)')
    assert text.index('_owner_topic_report.observe(self, event)', start) < text.index('_group_subscription_gate(event)', start)
    assert text.index('_owner_topic_report.handle_owner_reply(self, event)', start) < text.index('_group_subscription_gate(event)', start)


def test_warning_empty_rpc_response_is_not_claimed_success():
    async def scenario():
        b = bot()
        b.client.send_message = AsyncMock(return_value=None)
        report.save_mapping(OWNER, 9101, CHAT, 841)
        ev = Event('هشدار: متن', group=False, sender_id=OWNER)
        ev.reply_to = NS(reply_to_msg_id=9101)
        assert await report.handle_owner_reply(b, ev)
        assert 'تأیید نشد' in ev.replies[-1]
    asyncio.run(scenario())


def test_real_new_message_handler_observes_before_inactive_gate(monkeypatch):
    # Reuse the real core-registration harness, but stop before processing the
    # blocked group. This tests the actual entry handler rather than a mock
    # of its routing or a substring check alone.
    import test_broadcast_routing as harness
    import core.bot_working_split_ok as core
    async def scenario():
        harness._captured.clear()
        b = await harness._build()
        assert harness._captured
        handler = next(fn for fn in harness._captured if fn.__name__ == "new_message_handler")
        observed = []
        monkeypatch.setattr(report, 'observe', lambda bot, event: observed.append(event) or True)
        monkeypatch.setattr(core, '_group_subscription_gate', lambda event: (True, 'not_active', '', CHAT, 44))
        ev = Event('پهلوی')
        await handler(ev)
        assert observed == [ev]
        assert not b.client.sent and not ev.replies
    asyncio.run(scenario())


@pytest.mark.parametrize('text', [
    'پـهــــلوی', 'ب.س.ی.ی.ج', 'خاااامنه-ای', 'جاوید_نامان',
    'رژیم ایران', 'رهبر جمهوری اسلامی', 'Reza Shah',
])
def test_spelling_variants_and_leader_references(text):
    assert report.find_topic(text)


def test_no_username_or_group_link_is_not_invented(monkeypatch):
    async def scenario():
        from modules import owner_private
        monkeypatch.setattr(owner_private, 'resolve_private_owner_peer', AsyncMock(return_value='owner-peer'))
        b = bot()
        ev = Event('موساد')
        ev.sender.username = None
        ev.chat.username = None
        assert report.observe(b, ev)
        await asyncio.gather(*tuple(report._tasks))
        _, text, _ = b.client.sent[0]
        assert 'یوزنیم : ندارد' in text
        assert 'لینک گروه : در دسترس نیست' in text
    asyncio.run(scenario())


def test_real_core_handler_end_to_end_with_group_gate_and_owner_reply(monkeypatch):
    import test_broadcast_routing as harness
    import core.bot_working_split_ok as core
    from modules import owner_private
    async def scenario():
        harness._captured.clear()
        b = await harness._build()
        b.client = Client()
        monkeypatch.setattr(owner_private, 'resolve_private_owner_peer', AsyncMock(return_value='owner-peer'))
        monkeypatch.setattr(core, '_group_subscription_gate', lambda event: (True, 'not_active', '', CHAT, 44))
        handler = next(fn for fn in harness._captured if fn.__name__ == 'new_message_handler')
        ev = Event('یاد جاویدنامان')
        await handler(ev)
        assert report._tasks
        await asyncio.gather(*tuple(report._tasks))
        assert len(b.client.sent) == 1 and b.client.sent[0][0] == 'owner-peer'
        w = Event('هشدار: مراقب باشید', group=False, sender_id=OWNER, mid=350)
        w.reply_to = NS(reply_to_msg_id=9101)
        await handler(w)
        assert len(b.client.sent) == 2 and b.client.sent[-1][0] == CHAT
        assert b.client.sent[-1][1] == 'مراقب باشید'
        assert w.replies == ['✅ هشدار به گروه ارسال شد.']
    asyncio.run(scenario())


def test_disabled_feature_neither_reports_nor_sends_warnings(monkeypatch):
    async def scenario():
        b = bot()
        report.save_mapping(OWNER, 9101, CHAT, 841)
        monkeypatch.setenv('OWNER_TOPIC_REPORT_ENABLED', '0')
        assert not report.observe(b, Event('پهلوی'))
        w = Event('هشدار: متن', group=False, sender_id=OWNER)
        w.reply_to = NS(reply_to_msg_id=9101)
        assert not await report.handle_owner_reply(b, w)
        assert not b.client.sent
    asyncio.run(scenario())


def test_real_core_observes_message_edited_to_matching_topic(monkeypatch):
    import test_broadcast_routing as harness
    import core.bot_working_split_ok as core
    from modules import owner_private
    async def scenario():
        harness._captured.clear()
        b = await harness._build()
        b.client = Client()
        monkeypatch.setattr(owner_private, 'resolve_private_owner_peer', AsyncMock(return_value='owner-peer'))
        monkeypatch.setattr(core, '_group_subscription_gate', lambda event: (True, 'not_active', '', CHAT, 44))
        handler = next(fn for fn in harness._captured if fn.__name__ == 'new_message_handler')
        await handler(Event('سلام'))
        ev = Event('یاد جاویدنامان')
        ev.is_edit = True
        await handler(ev)
        await asyncio.gather(*tuple(report._tasks))
        assert len(b.client.sent) == 1 and 'جاویدنامان' in b.client.sent[0][1]
    asyncio.run(scenario())
