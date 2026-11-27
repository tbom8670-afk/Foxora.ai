"""Offline SQLite/file and real group-dispatch tests for numbered backgrounds."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handlers import background_handler as gallery
from modules import runtime_db
from modules.group_dispatch import classify_priority, LANE_ADMIN, LANE_COMMAND, LANE_NORMAL


class FakeEvent:
    is_private = False
    def __init__(self, chat_id, user_id, text, photo=False, message_id=1):
        self.chat_id = chat_id
        self.sender_id = user_id
        self.sender = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(id=message_id, message=text,
            caption=text, photo=object() if photo else None, media=None,
            edit_date=None)
        self.replies = []
    async def reply(self, text, **kw):
        self.replies.append(text)
    async def get_sender(self):
        return self.sender


class FakeClient:
    def __init__(self):
        self.sent = []
        self.fail = False
    async def download_media(self, message, file=None):
        Path(file).write_bytes(b'photo-bytes-' + str(message.id).encode())
        return file
    async def send_file(self, target, photo, caption=None, reply_to=None):
        if self.fail:
            raise RuntimeError('send failed')
        self.sent.append((target, photo, caption, reply_to))


class FakeLogger:
    def __init__(self):
        self.errors = []
    def log_error(self, value):
        self.errors.append(value)


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_file, self.old_conn = runtime_db.DB_FILE, runtime_db._CONN
        runtime_db.DB_FILE = Path(self.temp.name) / 'bot.sqlite3'
        runtime_db._CONN = None
        self.dir_patch = patch.object(gallery, '_GALLERY_DIR', Path(self.temp.name) / 'gallery')
        self.owner_patch = patch.object(gallery, 'is_global_owner', side_effect=lambda uid: uid == 7)
        self.dir_patch.start()
        self.owner_patch.start()
        gallery._sessions.clear()
        gallery._user_locks.clear()
        gallery._catalog_lock = None
        self.bot = SimpleNamespace(client=FakeClient(), logger=FakeLogger())

    def tearDown(self):
        self.owner_patch.stop()
        self.dir_patch.stop()
        gallery._sessions.clear()
        gallery._user_locks.clear()
        gallery._catalog_lock = None
        if runtime_db._CONN is not None:
            runtime_db._CONN.close()
        runtime_db.DB_FILE, runtime_db._CONN = self.old_file, self.old_conn
        self.temp.cleanup()

    async def command(self, event):
        return await gallery.handle(self.bot, event, event.chat_id,
                                    event.sender_id, event.message.message)

    def test_global_owner_registers_shared_numbered_photos_and_no_user_repeat(self):
        async def scenario():
            group = -1000023164149
            public = FakeEvent(group, 42, 'پس زمینه', message_id=1)
            self.assertTrue(await self.command(public))
            self.assertIn('ثبت نشده', public.replies[-1])
            intruder = FakeEvent(group, 42, 'تنظیم پس زمینه')
            self.assertFalse(await self.command(intruder))
            start = FakeEvent(group, 7, 'تنظیم پس زمینه')
            self.assertTrue(await self.command(start))
            self.assertEqual(classify_priority('تنظیم پس زمینه', start)[1], LANE_ADMIN)
            image3 = FakeEvent(group, 7, 'عکس شماره ۳\nتوضیح روی خود عکس', photo=True, message_id=30)
            self.assertEqual(classify_priority(image3.message.message, image3)[1], LANE_ADMIN)
            self.assertTrue(await self.command(image3))
            self.assertIn('شماره 3', image3.replies[-1])
            self.assertEqual(classify_priority(image3.message.message, image3)[1], LANE_NORMAL)
            self.assertTrue(Path(self.temp.name, 'gallery', '3.jpg').exists())
            self.assertTrue(await self.command(FakeEvent(group, 7, 'تنظیم پس زمینه')))
            image8 = FakeEvent(group, 7, 'عکس شماره 8', photo=True, message_id=80)
            self.assertTrue(await self.command(image8))
            self.assertEqual(classify_priority('پس زمینه', public)[1], LANE_COMMAND)
            await self.command(public)
            await self.command(public)
            self.assertEqual([item[2] for item in self.bot.client.sent],
                             ['عکس شماره ۳\nتوضیح روی خود عکس', 'عکس شماره 8'])
            self.assertEqual([item[3] for item in self.bot.client.sent], [1, 1])
            await self.command(public)
            self.assertIn('قبلاً دریافت', public.replies[-1])
            other = FakeEvent(-1000023000011, 56, 'پس زمینه', message_id=4)
            await self.command(other)
            self.assertEqual(self.bot.client.sent[-1][2], 'عکس شماره ۳\nتوضیح روی خود عکس')
            runtime_db._CONN.close()
            runtime_db._CONN = None
            gallery._sessions.clear()  # restart without losing photos/progress
            await self.command(public)
            self.assertIn('قبلاً دریافت', public.replies[-1])
            self.assertEqual(runtime_db.kv_get(gallery.SEEN, '42'), [3, 8])
        asyncio.run(scenario())

    def test_invalid_duplicate_and_failed_send_does_not_mark_as_seen(self):
        async def scenario():
            group = -1000023164149
            await self.command(FakeEvent(group, 7, 'تنظیم پس زمینه'))
            bad = FakeEvent(group, 7, 'سلام', photo=True)
            await self.command(bad)
            self.assertIn('ابتدای کپشن', bad.replies[-1])
            good = FakeEvent(group, 7, 'عکس شماره 1', photo=True)
            await self.command(good)
            await self.command(FakeEvent(group, 7, 'تنظیم پس زمینه'))
            duplicate = FakeEvent(group, 7, 'عکس شماره ۱', photo=True)
            await self.command(duplicate)
            self.assertIn('قبلاً ثبت', duplicate.replies[-1])
            self.assertEqual(gallery._path(1).read_bytes(), b'photo-bytes-1')
            gallery._sessions.clear()
            req = FakeEvent(group, 42, 'پس زمینه')
            self.bot.client.fail = True
            await self.command(req)
            self.assertEqual(runtime_db.kv_get(gallery.SEEN, '42', []), [])
            self.bot.client.fail = False
            await self.command(req)
            self.assertEqual(runtime_db.kv_get(gallery.SEEN, '42'), [1])
        asyncio.run(scenario())

    def test_simultaneous_requests_cannot_repeat_same_number(self):
        async def scenario():
            group = -1000023164149
            await self.command(FakeEvent(group, 7, 'تنظیم پس زمینه'))
            await self.command(FakeEvent(group, 7, 'عکس شماره 1', photo=True))
            a = FakeEvent(group, 42, 'پس زمینه', message_id=9)
            b = FakeEvent(group, 42, 'پس زمینه', message_id=10)
            await asyncio.gather(self.command(a), self.command(b))
            self.assertEqual(len(self.bot.client.sent), 1)
            self.assertTrue(a.replies or b.replies)
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
