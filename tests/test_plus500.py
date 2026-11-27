"""Plus500 owner isolation, persistence, routing and entity regression tests."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handlers import message_handler as mh
from modules import plus500, admin_help_builder as help_builder


class ReplyEvent:
    is_private = False
    def __init__(self):
        self.replies = []
        self.message = SimpleNamespace(edit_date=None)

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs.get('formatting_entities', [])))


class Plus500Tests(unittest.TestCase):
    def setUp(self):
        self.data = {}
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(plus500, 'get_group_owner', side_effect=lambda chat: {101: 7, 202: 8}.get(chat)),
            patch.object(plus500.runtime_db, 'kv_get', side_effect=lambda ns, key, default=None: self.data.get((ns, key), default)),
            patch.object(plus500.runtime_db, 'kv_set', side_effect=lambda ns, key, value: self.data.__setitem__((ns, key), value)),
            patch.object(plus500.runtime_db, 'kv_delete', side_effect=lambda ns, key: self.data.pop((ns, key), None)),
        ]
        for p in self.patches:
            p.start()
        plus500._SESSIONS.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        plus500._SESSIONS.clear()
        self.temp.cleanup()

    def run_message(self, event, group, actor, text):
        return asyncio.run(plus500.handle(event, group, actor, text))

    def test_only_registered_group_owner_and_group_isolation(self):
        event = ReplyEvent()
        for actor in (8, 42, None):
            self.assertTrue(self.run_message(event, 101, actor, 'plus500'))
            self.assertFalse(plus500.is_awaiting(101, actor))
        self.assertFalse(event.replies)
        self.assertTrue(self.run_message(event, 101, 7, 'plus500'))
        self.assertTrue(plus500.is_awaiting(101, 7))
        self.assertFalse(plus500.is_awaiting(202, 7))
        self.assertFalse(self.run_message(ReplyEvent(), 202, 7, 'سکوت'))

    def test_workflow_alias_argument_persistence_conflicts_and_reset(self):
        event = ReplyEvent()
        self.assertTrue(self.run_message(event, 101, 7, 'plus500'))
        self.assertIn('سکوت', plus500.supported_commands())
        self.assertTrue(self.run_message(event, 101, 7, 'سکوت'))
        self.assertTrue(self.run_message(event, 101, 7, 'ساکت'))
        self.assertEqual(plus500.resolve(101, 'ساکت 10'), 'سکوت 10')
        self.assertEqual(plus500.resolve(101, 'سکوت 10'), 'سکوت 10')
        self.assertEqual(plus500.resolve(202, 'ساکت 10'), 'ساکت 10')
        self.assertFalse(plus500.is_awaiting(101, 7))
        self.assertFalse(self.run_message(event, 101, 7, 'قفل'))
        self.assertTrue(self.run_message(event, 101, 7, 'plus500'))
        self.assertTrue(self.run_message(event, 101, 7, 'قفل'))
        self.assertTrue(self.run_message(event, 101, 7, 'ساکت'))
        self.assertIn('قبلاً', event.replies[-1][0])
        self.assertTrue(self.run_message(event, 101, 7, 'plus500'))
        self.assertTrue(self.run_message(event, 101, 7, 'قفل'))
        self.assertTrue(self.run_message(event, 101, 7, 'سکوت'))
        self.assertIn('تداخل', event.replies[-1][0])
        plus500._SESSIONS.clear()  # simulate restart: DB persists, session does not
        self.assertEqual(plus500.resolve(101, 'ساکت 10'), 'سکوت 10')
        self.assertTrue(asyncio.run(plus500.notify_old_command(
            ReplyEvent(), 101, 8, 'سکوت 10')))
        self.assertTrue(self.run_message(event, 101, 7, 'لغو plus500'))
        self.assertEqual(plus500.resolve(101, 'ساکت 10'), 'ساکت 10')
        self.assertFalse(self.data)

    def test_after_rename_original_only_notifies_and_selection_is_explicit(self):
        owner = ReplyEvent()
        self.run_message(owner, 101, 7, 'plus500')
        self.assertIn('اسم کدام دستور', owner.replies[-1][0])
        self.run_message(owner, 101, 7, 'سکوت')
        self.assertIn('نام جدید دستور را', owner.replies[-1][0])
        self.run_message(owner, 101, 7, 'ساکت')
        self.assertFalse(plus500.is_awaiting(101, 7))
        self.assertFalse(self.run_message(owner, 101, 7, 'سکوت'))
        notice = ReplyEvent()
        self.assertTrue(asyncio.run(plus500.notify_old_command(
            notice, 101, 7, 'سکوت')))
        self.assertEqual(len(notice.replies), 1)
        self.assertIn('مالک ثبت‌شده', notice.replies[0][0])
        self.assertIn('ساکت', notice.replies[0][0])
        def spans(kind):
            raw = notice.replies[0][0].encode('utf-16-le')
            return [raw[e.offset*2:(e.offset+e.length)*2].decode('utf-16-le')
                    for e in notice.replies[0][1] if type(e).__name__ == kind]
        self.assertIn('🔺 نام قبلی دستور : سکوت', spans('MessageEntityBlockquote'))
        self.assertIn('🔺 نام جدید دستور : ساکت', spans('MessageEntityBlockquote'))
        self.assertFalse(asyncio.run(plus500.notify_old_command(
            ReplyEvent(), 101, 7, 'ساکت 10')))
        self.assertFalse(asyncio.run(plus500.notify_old_command(
            ReplyEvent(), 202, 7, 'سکوت')))
        source = (Path(__file__).resolve().parents[1] / 'core/bot_working_split_ok.py').read_text()
        self.assertLess(source.index('await plus500.notify_old_command('),
                        source.index('elif (is_fast_moderation_command(_priority_alias_text)'))
        self.run_message(owner, 101, 7, 'لغو plus500')
        self.assertFalse(asyncio.run(plus500.notify_old_command(
            ReplyEvent(), 101, 7, 'سکوت')))

    def test_utf16_format_entities_and_admin_help(self):
        for payload in (plus500._prompt(), plus500._request_new('سکوت'),
                        plus500._success('سکوت', 'ساکت'),
                        plus500._renamed_notice('سکوت', 'ساکت'), plus500._reset()):
            text, entities = payload
            encoded = text.encode('utf-16-le')
            for entity in entities:
                self.assertLessEqual(entity.offset + entity.length, len(encoded) // 2)
                self.assertGreater(entity.length, 0)
        title = '📓 برای تغییر نام دستورات ربات بنویسید'
        lines = 'برای فعال سازی : plus500\nخاموش کردن : لغو plus500'
        blocks = help_builder.build_admin_help_messages()
        matches = [(text, entities) for text, entities in blocks if title in text]
        self.assertEqual(len(matches), 1)
        text, entities = matches[0]
        def extract(entity):
            raw = text.encode('utf-16-le')
            return raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode('utf-16-le')
        self.assertIn(title, [extract(e) for e in entities if type(e).__name__ == 'MessageEntityBold'])
        self.assertIn(lines, [extract(e) for e in entities if type(e).__name__ == 'MessageEntityBlockquote'])
        for line in lines.split('\n'):
            self.assertIn(line, [extract(e) for e in entities if type(e).__name__ == 'MessageEntityBold'])

    def test_alias_uses_original_command_direct_lane_before_normal_ingest(self):
        from modules.group_dispatch import classify_priority, LANE_ADMIN, LANE_NORMAL
        self.run_message(ReplyEvent(), 101, 7, 'plus500')
        self.run_message(ReplyEvent(), 101, 7, 'سکوت')
        self.run_message(ReplyEvent(), 101, 7, 'ساکن')
        event = SimpleNamespace(chat_id=101, sender_id=42, is_private=False)
        self.assertEqual(classify_priority('ساکن', event)[1], LANE_NORMAL)
        rewritten = plus500.resolve(101, 'ساکن')
        self.assertEqual(rewritten, 'سکوت')
        self.assertEqual(classify_priority(rewritten, event)[1], LANE_ADMIN)
        self.assertEqual(plus500.routing_priority(101, 42, 'ساکن', event)[1], LANE_ADMIN)
        self.assertEqual(plus500.routing_priority(101, 42, 'ساکن 10', event)[1], LANE_ADMIN)
        self.assertEqual(plus500.routing_priority(101, 7, 'سکوت', event)[1], LANE_ADMIN)
        self.assertIsNone(plus500.routing_priority(202, 42, 'ساکن', event))
        self.assertEqual(plus500.resolve(101, 'ساکن 10'), 'سکوت 10')
        target_event = SimpleNamespace(
            message=SimpleNamespace(message='ساکن', caption=None),
            reply_to=SimpleNamespace(reply_to_msg_id=1234))
        before = plus500.install_original_text(target_event, rewritten)
        self.assertEqual(target_event.message.message, 'سکوت')
        self.assertEqual(target_event.reply_to.reply_to_msg_id, 1234)
        plus500.restore_original_text(target_event, before)
        self.assertEqual(target_event.message.message, 'ساکن')
        core = (Path(__file__).resolve().parents[1] / 'core/bot_working_split_ok.py').read_text()
        self.assertLess(core.index('_plus_lane = plus500.routing_priority('),
                        core.index('decision = ingest_event(self, event)'))
        self.assertIn('priority, kind = _plus_lane', core)

    def test_alias_runs_real_dispatch_with_original_text_and_reply_target(self):
        self.run_message(ReplyEvent(), 101, 7, 'plus500')
        self.run_message(ReplyEvent(), 101, 7, 'اخراج')
        self.run_message(ReplyEvent(), 101, 7, 'سیکتیر')
        canonical = plus500.resolve(101, 'سیکتیر')
        self.assertEqual(canonical, 'اخراج')
        event = SimpleNamespace(chat_id=101, is_private=False, sender_id=7,
            message=SimpleNamespace(message='سیکتیر', caption=None, edit_date=None),
            reply_to=SimpleNamespace(reply_to_msg_id=1234))
        observed = []
        async def original_pipeline(ev):
            observed.append((ev.message.message, ev.reply_to.reply_to_msg_id,
                             getattr(ev, '_plus500_alias_source', None)))
            self.assertFalse(await plus500.notify_old_command(
                ev, 101, 7, ev.message.message))
            return 'executed'
        result = asyncio.run(plus500.dispatch_as_original(
            event, 'سیکتیر', canonical, original_pipeline))
        self.assertEqual(result, 'executed')
        self.assertEqual(observed, [('اخراج', 1234, 'سیکتیر')])
        self.assertEqual(event.message.message, 'سیکتیر')
        self.assertFalse(hasattr(event, '_plus500_alias_source'))
        core = (Path(__file__).resolve().parents[1] / 'core/bot_working_split_ok.py').read_text()
        self.assertIn('await plus500.dispatch_as_original(', core)

    def test_timed_mute_reuses_authorization_and_passes_duration(self):
        from tests.test_fast_moderation import User, Event, Bot
        sender, target = User(7, 'owner'), User(31, 'target')
        event, bot = Event(sender), Bot(sender, target)
        async def native(*args, **kwargs):
            return False
        with patch.object(mh, '_has_group_management_permission',
                          side_effect=lambda bot, chat, uid, username: uid == sender.id), \
             patch.object(mh, '_is_native_group_admin', side_effect=native):
            self.assertTrue(asyncio.run(mh.handle_fast_moderation_command(
                bot, event, 'سکوت ۱۰', sender)))
            self.assertIsNotNone(bot.moderation_queue.job)
            asyncio.run(bot.moderation_queue.job['operation']())
        self.assertEqual(bot.admin_actions.calls[0][2]['duration_seconds'], 600)
        with patch.object(mh, '_has_group_management_permission', return_value=False):
            other = Event(User(99, 'stranger'))
            self.assertFalse(asyncio.run(mh.handle_fast_moderation_command(
                bot, other, 'سکوت 10', other.sender)))

    def test_fast_lane_guards_and_numeric_mute(self):
        core = (Path(__file__).resolve().parents[1] / 'core/bot_working_split_ok.py').read_text()
        self.assertIn('not plus500.is_awaiting(chat_id, sender_id)', core)
        self.assertIn('is_fast_moderation_command(_priority_alias_text)', core)
        self.assertIn('sender_id", None), _priority_alias_text)', core)
        source = (Path(__file__).resolve().parents[1] / 'handlers/message_handler.py').read_text()
        self.assertLess(source.index('_plus_original = plus500.resolve('), source.index('Numeric timed mute uses'))
        self.assertIn('duration_seconds=duration_minutes * 60', source)
        self.assertTrue(mh.is_fast_moderation_command('سکوت'))


class Plus500SqlitePersistenceTests(unittest.TestCase):
    def test_persists_per_group_after_database_reopen_and_reset_isolated(self):
        from modules import runtime_db
        with tempfile.TemporaryDirectory() as folder:
            original_file, original_conn = runtime_db.DB_FILE, runtime_db._CONN
            runtime_db.DB_FILE = Path(folder) / 'real.sqlite3'
            runtime_db._CONN = None
            plus500._SESSIONS.clear()
            try:
                with patch.object(plus500, 'get_group_owner', return_value=7):
                    event = ReplyEvent()
                    group = -1000023164149
                    for word in ('plus500', 'اخراج', 'سیک'):
                        self.assertTrue(asyncio.run(plus500.handle(
                            event, group, 7, word)))
                    runtime_db.kv_set('unrelated', 'setting', {'enabled': True})
                    self.assertEqual(plus500.resolve(group, 'سیک'), 'اخراج')
                    self.assertEqual(plus500.resolve(123456, 'سیک'), 'سیک')
                    runtime_db._CONN.close()
                    runtime_db._CONN = None  # simulate a fresh process
                    plus500._SESSIONS.clear()
                    self.assertEqual(plus500.resolve(23164149, 'سیک'), 'اخراج')
                    self.assertTrue(asyncio.run(plus500.notify_old_command(
                        ReplyEvent(), group, 42, 'اخراج')))
                    asyncio.run(plus500.handle(event, group, 7, 'لغو plus500'))
                    self.assertEqual(plus500.resolve(group, 'سیک'), 'سیک')
                    self.assertEqual(runtime_db.kv_get('unrelated', 'setting'),
                                     {'enabled': True})
            finally:
                if runtime_db._CONN is not None:
                    runtime_db._CONN.close()
                runtime_db.DB_FILE, runtime_db._CONN = original_file, original_conn
                plus500._SESSIONS.clear()


if __name__ == '__main__':
    unittest.main()
