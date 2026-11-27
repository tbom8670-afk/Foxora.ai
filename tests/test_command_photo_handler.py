"""Offline owner setup and media+formatted-caption command response tests."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handlers import command_photo_handler as photos
from modules import runtime_db


class Event:
    is_private = False
    def __init__(self, chat, owner, text='', photo=False, mid=10):
        self.chat_id, self.sender_id = chat, owner
        self.message = SimpleNamespace(id=mid, message=text, caption=text,
                                       photo=object() if photo else None, media=None, edit_date=None)
        self.replies = []
    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(id=len(self.replies))
    async def respond(self, text, **kwargs):
        return await self.reply(text, **kwargs)


class Client:
    def __init__(self):
        self.sent = []
        self.archive = {}
    async def send_file(self, target, media, **kwargs):
        self.sent.append((target, media, kwargs))
        if target == 'me':
            self.archive[101] = SimpleNamespace(id=101, photo=media, media=None)
            return SimpleNamespace(id=101)
        return SimpleNamespace(id=202)
    async def get_messages(self, target, ids):
        return self.archive.get(ids) if target == 'me' else None


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_file, self.old_conn = runtime_db.DB_FILE, runtime_db._CONN
        runtime_db.DB_FILE = Path(self.tmp.name) / 'runtime.sqlite3'
        runtime_db._CONN = None
        self.patcher = patch.object(photos, 'is_global_owner', side_effect=lambda uid: uid == 7)
        self.patcher.start()
        photos._sessions.clear()
        self.bot = SimpleNamespace(client=Client(), logger=SimpleNamespace(log_error=lambda _: None))

    def tearDown(self):
        self.patcher.stop()
        photos._sessions.clear()
        if runtime_db._CONN:
            runtime_db._CONN.close()
        runtime_db.DB_FILE, runtime_db._CONN = self.old_file, self.old_conn
        self.tmp.cleanup()

    def test_owner_setup_shared_with_other_groups_first_reply_only(self):
        async def run():
            group = -1000000001234
            self.assertFalse(await photos.handle_setup(self.bot, Event(group, 8, 'تنظیم عکس بازی'), group, 8, 'تنظیم عکس بازی'))
            start = Event(group, 7, 'تنظیم عکس بازی')
            self.assertTrue(await photos.handle_setup(self.bot, start, group, 7, start.message.message))
            self.assertTrue(photos.is_awaiting(1234, 7))
            image = Event(group, 7, 'ignored caption', photo=True, mid=55)
            self.assertTrue(await photos.handle_setup(self.bot, image, group, 7, image.message.message))
            self.assertEqual(photos.photo_for('بازی')['source'], {'chat_id': 'me', 'message_id': 101})
            other = Event(-1005432, 12, 'بازی', mid=56)
            overlay = photos.install_on_event(self.bot, other, 'بازی')
            self.assertIsNotNone(overlay)
            entities = [object()]
            await other.reply('🎮 متن اصلی', formatting_entities=entities)
            await other.reply('🏆 نتیجه بازی')
            sent = self.bot.client.sent[-1]
            self.assertEqual(sent[0], -1005432)
            self.assertEqual(sent[2]['caption'], '🎮 متن اصلی')
            self.assertIs(sent[2]['formatting_entities'], entities)
            self.assertEqual(sent[2]['reply_to'], 56)
            self.assertEqual(other.replies, [('🏆 نتیجه بازی', {})])
            runtime_db._CONN.close()
            runtime_db._CONN = None
            self.assertEqual(photos.photo_for('بازی')['source']['message_id'], 101)
            self.assertTrue(await photos.handle_setup(self.bot, Event(group, 7, 'لغو عکس بازی'), group, 7, 'لغو عکس بازی'))
            self.assertIsNone(photos.photo_for('بازی'))
        asyncio.run(run())

    def test_activation_entities_and_missing_media_fallback(self):
        async def run():
            runtime_db.kv_set(photos.NAMESPACE, 'فعال سازی', {'source': {'chat_id': 'me', 'message_id': 101}})
            event = Event(-1005432, 7, 'فعال سازی')
            entities = [object()]
            await photos.send_activation(self.bot, event, 'متن فعال سازی', entities)
            self.assertEqual(event.replies[0][1]['formatting_entities'], entities)
            self.bot.client.archive[101] = SimpleNamespace(photo=object(), media=None)
            await photos.send_activation(self.bot, event, 'متن فعال سازی', entities)
            self.assertEqual(self.bot.client.sent[-1][2]['caption'], 'متن فعال سازی')
            self.assertIs(self.bot.client.sent[-1][2]['formatting_entities'], entities)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
