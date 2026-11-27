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
                        source.index('elif (is_fast_moderation_command(text)'))
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
        source = (Path(__file__).resolve().parents[1] / 'handlers/message_handler.py').read_text()
        self.assertLess(source.index('message_text = plus500.resolve('), source.index('Numeric timed mute uses'))
        self.assertIn('duration_seconds=duration_minutes * 60', source)
        self.assertTrue(mh.is_fast_moderation_command('سکوت'))


if __name__ == '__main__':
    unittest.main()
