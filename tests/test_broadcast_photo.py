"""Photo+caption broadcast stays one media message through preview and delivery."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handlers import broadcast_handler as bh
from modules import broadcast_state as state


class FakeLogger:
    def __init__(self):
        self.errors = []
    def log_info(self, message):
        pass
    def log_error(self, message):
        self.errors.append(message)


class Client:
    def __init__(self):
        self.photos = []
        self.texts = []
        self.source = None
    async def send_file(self, target, photo, caption=None, formatting_entities=None):
        self.photos.append((target, photo, caption, formatting_entities))
        return SimpleNamespace(id=2000 + len(self.photos))
    async def send_message(self, target, text, formatting_entities=None):
        self.texts.append((target, text, formatting_entities))
    async def get_messages(self, chat, ids):
        return self.source
    async def iter_dialogs(self):
        yield SimpleNamespace(is_group=True, id=-100123, entity=-100123)


class Event:
    def __init__(self, text, photo=False, entities=None, chat_id=99):
        self.chat_id = chat_id
        self.is_private = True
        self.replies = []
        self.message = SimpleNamespace(id=30, message=text, caption=text,
            photo=object() if photo else None, entities=entities or [])
    async def reply(self, text, formatting_entities=None):
        self.replies.append((text, formatting_entities))
        return SimpleNamespace(id=1000 + len(self.replies))


class PhotoTests(unittest.TestCase):
    def setUp(self):
        state.clear(333)
        self.bot = SimpleNamespace(logger=FakeLogger(), client=Client())
        self.active = patch.object(bh, 'load_groups', return_value=[-100123])
        self.enabled = patch.object(bh, 'is_active', return_value=True)
        self.delay = patch.object(bh.asyncio, 'sleep', return_value=None)
        for p in (self.active, self.enabled, self.delay):
            p.start()

    def tearDown(self):
        for p in (self.delay, self.enabled, self.active):
            p.stop()
        state.clear(333)

    def send(self, event, text=None):
        return asyncio.run(bh.handle_private_broadcast(self.bot, event, 333,
                                                        event.message.message if text is None else text))

    def test_photo_preview_and_group_caption_entities_are_atomic(self):
        self.send(Event('اطلاع رسانی'))
        from modules.admin_help_builder import MessageEntityBold
        bold = MessageEntityBold(offset=0, length=4)
        image = Event('سلام دوستان', photo=True, entities=[bold])
        self.send(image)
        self.assertEqual(self.bot.client.photos[0][2], 'سلام دوستان')
        self.assertEqual(self.bot.client.photos[0][3], [bold])
        self.assertNotIn(('سلام دوستان', None), image.replies)
        self.assertIn('تایید', image.replies[-1][0])
        self.assertEqual(state.get(333)['media_ref'], {'chat_id': 99, 'message_id': 30})
        self.send(Event('تایید'))
        delivered = [p for p in self.bot.client.photos if p[0] == -100123]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][2:], ('سلام دوستان', [bold]))
        self.assertEqual(self.bot.client.texts, [])

    def test_recover_photo_from_source_after_restart_and_never_fallback_to_text(self):
        self.send(Event('اطلاع رسانی'))
        self.send(Event('عنوان', photo=True))
        state.get(333)['media'] = None  # process restarted; persistent source ref remains
        self.bot.client.source = Event('عنوان', photo=True).message
        self.send(Event('تایید'))
        self.assertEqual(len([p for p in self.bot.client.photos if p[0] == -100123]), 1)
        self.assertEqual(self.bot.client.texts, [])

    def test_unavailable_original_does_not_send_caption_alone(self):
        self.send(Event('اطلاع رسانی'))
        self.send(Event('عنوان', photo=True))
        state.get(333)['media'] = None
        confirm = Event('تایید')
        self.send(confirm)
        self.assertIn('در دسترس نیست', confirm.replies[-1][0])
        self.assertFalse([p for p in self.bot.client.photos if p[0] == -100123])
        self.assertFalse(self.bot.client.texts)

    def test_group_origin_photo_caption_follows_same_workflow(self):
        start = Event('اطلاع رسانی', chat_id=-100123)
        start.is_private = False
        self.assertTrue(asyncio.run(bh.handle_group_broadcast(
            self.bot, start, 333, 'اطلاع رسانی')))
        image = Event('کپشن خبر', photo=True, chat_id=-100123)
        image.is_private = False
        self.assertTrue(asyncio.run(bh.handle_group_broadcast(
            self.bot, image, 333, image.message.message)))
        other = Event('تایید', chat_id=-100456)
        self.assertFalse(asyncio.run(bh.handle_group_broadcast(
            self.bot, other, 333, 'تایید')))
        confirm = Event('تایید', chat_id=-100123)
        self.assertTrue(asyncio.run(bh.handle_group_broadcast(
            self.bot, confirm, 333, 'تایید')))
        self.assertEqual(len([p for p in self.bot.client.photos
                              if p[0] == -100123 and p[2] == 'کپشن خبر']), 2)

    def test_plain_text_behavior_unchanged(self):
        self.send(Event('اطلاع رسانی'))
        self.send(Event('متن عادی'))
        self.send(Event('تایید'))
        self.assertEqual(self.bot.client.texts[0][1], 'متن عادی')
        self.assertEqual(self.bot.client.photos, [])

    def test_oversized_photo_caption_rejected_before_preview(self):
        self.send(Event('اطلاع رسانی'))
        self.send(Event('ا' * 1025, photo=True))
        self.assertEqual(state.get(333)['phase'], 'awaiting_message')
        self.assertEqual(self.bot.client.photos, [])


if __name__ == '__main__':
    unittest.main()
