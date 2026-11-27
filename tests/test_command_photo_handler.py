"""Offline owner setup and media+formatted-caption command response tests."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handlers import command_photo_handler as photos
from modules import runtime_db
from modules.group_dispatch import classify_priority, LANE_ADMIN


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
    async def send_message(self, target, text, **kwargs):
        self.sent.append((target, None, {"text": text, **kwargs}))
        return SimpleNamespace(id=203)
    async def edit_message(self, target, mid, text, **kwargs):
        self.sent.append((target, "edit", {"mid": mid, "text": text, **kwargs}))
        return SimpleNamespace(id=mid)


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
            self.assertEqual(classify_priority(start.message.message, start)[1], LANE_ADMIN)
            image = Event(group, 7, 'ignored caption', photo=True, mid=55)
            self.assertEqual(classify_priority('', image)[1], LANE_ADMIN)
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

    def test_full_purge_early_route_keeps_formatted_confirmation(self):
        import inspect
        from handlers import message_handler
        from modules.full_purge import service
        source = inspect.getsource(message_handler.handle_new_message)
        hook = source.index('install_on_event(bot, event, normalize_command(message_text),')
        self.assertLess(hook, source.index('_full_purge.claims('))
        self.assertLess(hook, source.index('_two_factor_gate('))

        async def run():
            runtime_db.kv_set(photos.NAMESPACE, 'پاکسازی کامل',
                              {'source': {'chat_id': 'me', 'message_id': 101}})
            self.bot.client.archive[101] = SimpleNamespace(photo=object(), media=None)
            event = Event(-1000000000007, 7, 'پاکسازی کامل')
            photos.install_on_event(self.bot, event, 'پاکسازی کامل')
            result = await service.send_confirm(self.bot, event, event.chat_id)
            self.assertEqual(result.id, 202)
            self.assertEqual(event.replies, [])
            sent = self.bot.client.sent[-1]
            self.assertEqual(sent[2]['caption'], service.texts.confirm_text())
            self.assertEqual(len(sent[2]['formatting_entities']),
                             len(service.texts.confirm_entities(sent[2]['caption'])))
        asyncio.run(run())

    def test_core_fast_owner_register_group_uses_photo(self):
        from handlers import message_handler as handler
        from unittest.mock import AsyncMock

        async def run():
            runtime_db.kv_set(photos.NAMESPACE, 'ثبت گروه',
                              {'source': {'chat_id': 'me', 'message_id': 101}})
            self.bot.client.archive[101] = SimpleNamespace(photo=object(), media=None)
            self.bot.logger.log_info = lambda _: None
            event = Event(-1005432, 7, 'ثبت گروه')
            with (patch.object(handler, 'is_global_owner', return_value=True),
                  patch.object(handler, '_fast_owner_chat', new_callable=AsyncMock,
                               return_value=(SimpleNamespace(title='تست'), event.chat_id, 'تست')),
                  patch.object(handler, '_group_main_owner_display', new_callable=AsyncMock,
                               return_value='مالک'),
                  patch.object(handler, 'activate_group')):
                result = await handler.handle_fast_owner_command(self.bot, event, 'ثبت گروه')
            self.assertTrue(result)
            self.assertEqual(event.replies, [])
            sent = self.bot.client.sent[-1]
            self.assertEqual(sent[0], event.chat_id)
            self.assertIn('ثبت شد', sent[2]['caption'])
            self.assertEqual(sent[2]['reply_to'], event.message.id)
        asyncio.run(run())

    def test_protocol_activation_keeps_photo_and_updates_caption(self):
        from modules.virtual_protocol_ui import activate, deactivate
        class Service:
            def __init__(self, bot):
                self.bot = bot
                self.groups = {}
                self.activations = {}
            async def toggle(self, group, enabled):
                if enabled:
                    self.groups[str(group)] = 1
                else:
                    self.groups.pop(str(group), None)

        async def run():
            runtime_db.kv_set(photos.NAMESPACE, 'پروتکل روشن',
                              {'source': {'chat_id': 'me', 'message_id': 101}})
            self.bot.client.archive[101] = SimpleNamespace(photo=object(), media=None)
            service = Service(self.bot)
            event = SimpleNamespace(chat_id=-1000000000007, sender_id=7,
                                    sender=SimpleNamespace(username='owner'))
            await activate(service, event, step_seconds=.001)
            self.assertEqual(service.groups.get('7'), 1)
            sent = self.bot.client.sent
            self.assertEqual(sent[0][0], event.chat_id)
            self.assertIsNotNone(sent[0][1])
            self.assertIn('پروتکل روباه در حال فعال سازی', sent[0][2]['caption'])
            self.assertEqual(sent[-1][1], 'edit')
            self.assertIn('امـنیتی', sent[-1][2]['text'])
            self.assertEqual(sent[-1][2]['mid'], 202)
            # Repeat uses cached media; no extra get_messages RPC is needed.
            with patch.object(self.bot.client, 'get_messages', side_effect=AssertionError('slow fetch')):
                await activate(service, event, step_seconds=.001)
            self.assertIsNotNone(sent[-1][1])
            setup = Event(event.chat_id, 7, 'تنظیم عکس پروتکل خاموش')
            image = Event(event.chat_id, 7, 'caption ignored', photo=True, mid=67)
            self.assertTrue(await photos.handle_setup(self.bot, setup, event.chat_id,
                                                      7, setup.message.message))
            self.assertTrue(await photos.handle_setup(self.bot, image, event.chat_id,
                                                      7, image.message.message))
            await deactivate(service, event)
            self.assertEqual(sent[-1][2]['caption'], '🔺 پروتکل امنیتی روباه خاموش شد')
        asyncio.run(run())

    def test_game_stage_is_not_photographed(self):
        from handlers import message_handler as handler
        runtime_db.kv_set(photos.NAMESPACE, 'بازی',
                          {'source': {'chat_id': 'me', 'message_id': 101}})
        event = Event(-1000000000007, 7, 'بازی')
        with patch.object(handler, 'is_game_answer_active', return_value=True):
            self.assertIsNone(photos.install_on_event(
                self.bot, event, 'بازی', skip_if_game_active=True))
        self.assertEqual(self.bot.client.sent, [])

    def test_unconfigured_commands_do_not_query_sqlite(self):
        photos.warm_config()
        with patch.object(runtime_db, 'query_all', side_effect=AssertionError('per-message DB read')):
            for _ in range(1000):
                self.assertIsNone(photos.photo_for('دستور بدون عکس'))

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
