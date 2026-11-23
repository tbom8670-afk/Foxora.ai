import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from modules.virtual_protocols import Job, Service, Store, normalize, similar, intercept


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'config.json'
        self.db = Path(self.tmp.name) / 'vp.sqlite'
        self.service = Service(self.config, self.db)

    async def flush(self):
        await self.service.word_queue.join()
        await self.service.reply_queue.join()
        await self.service.queue.join()

    async def asyncTearDown(self):
        # Let thread-backed recovery finish before the temporary DB is removed.
        task = self.service.recovery_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await self.service.word_queue.join()
        await self.service.reply_queue.join()
        await self.service.queue.join()
        await self.service.bio_queue.join()
        await self.service.action_queue.join()
        if self.service.maintenance_task is not None:
            self.service.maintenance_task.cancel()
            await asyncio.gather(self.service.maintenance_task, return_exceptions=True)

    async def test_toggle_persists_and_groups_are_independent(self):
        self.assertFalse(self.service.submit(10, 1, 1, 'hello world'))
        self.assertFalse(self.db.exists())
        await self.service.toggle(-1000000000010, True)
        self.assertTrue(Service(self.config, self.db).groups.get('10'))
        self.assertFalse(self.service.submit(20, 1, 1, 'hello world'))
        self.assertTrue(self.service.submit(10, 1, 1, 'hello world'))
        await self.flush()
        await self.service.toggle(10, False)
        self.assertFalse(Service(self.config, self.db).groups.get('10'))
        self.assertFalse(self.service.submit(10, 1, 2, 'hello world'))
        await self.service.toggle(10, True)
        # New generation prevents reusing earlier history.
        self.assertTrue(self.service.submit(10, 1, 2, 'hello world'))
        await self.flush()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(distinct generation) from vp_messages').fetchone()[0], 2)

    async def test_command_permission_and_delayed_activation(self):
        msg = SimpleNamespace(id=1, message='پروتکل روشن', reply_to=None)
        event = SimpleNamespace(message=msg, chat_id=7, sender_id=123, is_private=False)
        self.service.bot = SimpleNamespace(client=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=99))))
        with patch('modules.admin_tools.has_admin_permission', return_value=False), patch('modules.virtual_protocol_ui.begin_activation') as begin:
            self.assertTrue(await intercept(self.service, event))
            begin.assert_not_called()
        with patch('modules.admin_tools.has_admin_permission', return_value=True), patch('modules.virtual_protocol_ui.begin_activation') as begin:
            self.assertTrue(await intercept(self.service, event))
            begin.assert_called_once_with(self.service,event)
        self.assertNotIn('7', self.service.groups)  # animation has not completed
        await self.service.toggle(7, True)
        msg.message = 'پروتکل خاموش'
        with patch('modules.admin_tools.has_admin_permission', return_value=True):
            self.assertTrue(await intercept(self.service, event))
        self.assertNotIn('7', self.service.groups)
        self.service.bot.client.send_message.assert_awaited_once()

    async def test_repeats_and_replay_dedup(self):
        await self.service.toggle(1, True)
        for mid in range(1, 5):
            self.assertTrue(self.service.submit(1, 9, mid, 'سلام خوبی 😊' if mid%2 else 'سلام، خوبی!', reply=33))
        await self.flush()
        self.assertTrue(self.service.submit(1, 9, 4, 'سلام خوبی', reply=33))
        await self.flush()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_messages').fetchone()[0], 4)
            score, level, ev = db.execute('select score,level,evidence from vp_evidence order by message_id desc limit 1').fetchone()
        ev = json.loads(ev)
        self.assertGreaterEqual(score, 45)
        self.assertEqual(level, 'strong')
        self.assertEqual(ev['repeat_count'], 4)
        self.assertTrue(ev['signals']['same_reply_target'])
        self.assertEqual(ev['decision'], 'evidence_recorded')

    async def test_natural_replies_not_flagged(self):
        await self.service.toggle(1, True)
        texts = ['سلام، خوبی؟', 'فردا هوا بارانی میشه', 'این کتاب رو خیلی دوست داشتم', 'سپاس از راهنمایی شما', 'امروز ساعت چند حرکت میکنیم؟', 'نتیجه مسابقه رو دیدی؟', 'به نظرم پیشنهادت عالیه', 'غذا رو با سبزی آماده کن', 'چند روز دیگه تعطیل هستیم', 'برای جلسه باید بیشتر فکر کنیم', 'کجا این عکس رو گرفتی؟', 'ممنون پاسخ مشکلم رو حل کرد']
        for mid, text in enumerate(texts, 1):
            self.service.submit(1, 9, mid, text, reply=mid+100)
        await self.flush()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_evidence').fetchone()[0], 0)

    async def test_queue_disabled_jobs_and_dedup(self):
        await self.service.toggle(1, True)
        await self.service.lock.acquire()
        self.assertTrue(self.service.submit(1, 2, 1, 'مثال یک پیام کامل'))
        self.assertFalse(self.service.submit(1, 2, 1, 'مثال یک پیام کامل'))
        # Simulate a durable off while the worker is blocked on the same lock.
        self.service.groups = {}
        self.service.lock.release()
        await self.flush()
        self.assertFalse(self.db.exists())

    def test_persian_normalization(self):
        self.assertTrue(similar(normalize('سلام، خوبی! 👋'), normalize('سلام خوبی')))
        self.assertEqual(normalize('كِتاب'), normalize('کتاب'))
        self.assertTrue(similar('hi', 'hi'))
        self.assertFalse(similar('hi', 'ho'))

    def test_real_24_hour_window(self):
        store = Store(self.db)
        now = time.time()
        with store.connect() as db:
            db.execute('insert into vp_messages values(?,?,?,?,?,?,?,?,?)', ('1','g','2',1,now-86401,'پیام تکراری نمونه',normalize('پیام تکراری نمونه'),None,0))
            db.execute('insert into vp_messages values(?,?,?,?,?,?,?,?,?)', ('1','g','2',2,now-86399,'پیام تکراری نمونه',normalize('پیام تکراری نمونه'),None,0))
        result = store.process(Job('1','2',3,'پیام تکراری نمونه',None,now,'g'))
        self.assertEqual(result[2]['repeat_count'], 2)

    async def test_similarity_alone_is_not_coordination(self):
        await self.service.toggle(1, True)
        self.service.submit(1, 2, 1, 'متن یکسان نمونه بلند')
        self.service.submit(1, 3, 2, 'متن یکسان نمونه بلند')
        await self.flush()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_evidence').fetchone()[0], 0)


    async def test_cross_group_requires_explicit_federation(self):
        policy = Path(self.tmp.name) / 'policy.json'
        policy.write_text(json.dumps({"federation": [[1, 2]]}), encoding='utf-8')
        self.service = Service(self.config, self.db, policy_path=policy)
        await self.service.toggle(1, True)
        await self.service.toggle(2, True)
        for mid in range(1, 3):
            self.service.submit(2, 9, mid, 'متن تبلیغ نمونه برای تکرار')
        await self.flush()
        for mid in range(1, 5):
            self.service.submit(1, 9, mid, 'متن تبلیغ نمونه برای تکرار', reply=100)
        await self.flush()
        with sqlite3.connect(self.db) as db:
            ev = json.loads(db.execute("select evidence from vp_evidence where group_id='1' order by message_id desc limit 1").fetchone()[0])
        self.assertTrue(ev['signals']['cross_group'])
        self.assertEqual(ev['cross_group']['matching_groups'], ['2'])
        await self.service.toggle(2, False)
        self.assertNotIn('2', self.service.peers_for('1'))
        isolated = Service(self.config, self.db, policy_path=Path(self.tmp.name)/'missing')
        self.assertEqual(set(isolated.peers_for('1')), {'1'})

    async def test_durable_recovery_and_idempotence(self):
        await self.service.toggle(1, True)
        generation = self.service.groups['1']
        job = Job('1', '8', 44, 'یک پیام معتبر برای بازیابی', None, time.time(), generation)
        self.service.store.stage(job)
        restored = Service(self.config, self.db, policy_path=Path(self.tmp.name)/'none')
        restored.start()
        await restored.recovery_task
        await restored.word_queue.join()
        await restored.queue.join()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_pending').fetchone()[0], 0)
            self.assertEqual(db.execute('select count(*) from vp_messages').fetchone()[0], 1)
        self.service.store.stage(job)  # simulate crash after commit, before ack
        await restored._recover()
        await restored.word_queue.join()
        await restored.queue.join()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_pending').fetchone()[0], 0)
            self.assertEqual(db.execute('select count(*) from vp_messages').fetchone()[0], 1)

    async def test_bio_real_adapter_is_optional_and_no_raw_bio_stored(self):
        self.service.bot = SimpleNamespace(client=SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(id=9))))
        await self.service.toggle(1, True)
        profile={'status':'ok', 'about':'عضو شو https://example.com', 'channel_title':None}
        with patch('modules.profile_guard.service.snapshot', new=AsyncMock(return_value=profile)) as fetch, patch('modules.profile_guard.filters.bio_sentences',return_value=['عضو شو']):
            for mid in range(1, 5):
                self.service.submit(1, 9, mid, 'عضو شو در کانال ما', reply=55)
            await self.flush()
            await self.service.bio_queue.join()
            fetch.assert_awaited_once()
        with sqlite3.connect(self.db) as db:
            ev = json.loads(db.execute('select evidence from vp_evidence order by message_id desc limit 1').fetchone()[0])
        self.assertEqual(ev['bio']['status'], 'ok')
        self.assertTrue(ev['bio']['confirmed'])
        self.assertNotIn('example.com', json.dumps(ev))

    async def test_release_feedback_and_private_report(self):
        await self.service.toggle(1, True)
        await self.service.note_release(1, 9, 20, 'unmute')
        for mid in range(1, 5):
            self.service.submit(1, 9, mid, 'نمونه پیام یکسان برای آزمایش', reply=33)
        await self.flush()
        with patch('modules.admin_tools.has_admin_permission', return_value=False):
            with self.assertRaises(PermissionError):
                await self.service.reports(1, 21)
        with patch('modules.admin_tools.has_admin_permission', return_value=True):
            reports = await self.service.reports(1, 20)
        feedback = reports[0]['evidence']['history']['manager_feedback']
        self.assertEqual(feedback[0]['action'], 'unmute')
        self.assertEqual(feedback[0]['actor_id'], '20')
        self.assertFalse(feedback[0]['linked_protocol'])
        self.assertIsNone(feedback[0]['reason'])

    async def test_expired_durable_job_not_recovered(self):
        await self.service.toggle(1, True)
        gen = self.service.groups['1']
        self.service.store.stage(Job('1', '8', 45, 'پیام قدیمی بازیابی', None, time.time()-121, gen))
        await self.service._recover()
        await self.flush()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_messages').fetchone()[0], 0)
            self.assertEqual(db.execute('select count(*) from vp_pending').fetchone()[0], 0)

    async def test_confirmed_false_positive_lowers_new_risk(self):
        await self.service.toggle(1, True)
        for mid in range(1, 5):
            self.service.submit(1, 9, mid, 'نمونه پیام تکراری درباره تبلیغ', reply=33)
        await self.flush()
        with patch('modules.admin_tools.has_admin_permission', return_value=False):
            with self.assertRaises(PermissionError):
                await self.service.mark_false_positive(1, 20, 4)
        with patch('modules.admin_tools.has_admin_permission', return_value=True):
            self.assertTrue(await self.service.mark_false_positive(1, 20, 4))
        await asyncio.sleep(.01)
        self.service.submit(1, 9, 5, 'نمونه پیام تکراری درباره تبلیغ', reply=33)
        await self.flush()
        with sqlite3.connect(self.db) as db:
            before = db.execute('select score from vp_evidence where message_id=4').fetchone()[0]
            after, evidence = db.execute('select score,evidence from vp_evidence where message_id=5').fetchone()
        self.assertEqual(before - after, 30)
        self.assertTrue(json.loads(evidence)['signals']['manager_feedback'])

    async def test_bio_fetch_cancelled_on_disable(self):
        self.service.bot = SimpleNamespace(client=SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(id=9))))
        await self.service.toggle(1, True)
        started = asyncio.Event()
        async def slow_fetch(*args, **kwargs):
            started.set()
            await asyncio.sleep(100)
        with patch('modules.profile_guard.service.snapshot', side_effect=slow_fetch), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.profile_guard.filters.channel_names',return_value=[]):
            self.service.submit(1, 9, 1, 'درود بر دوستان')
            await self.flush()
            await asyncio.wait_for(started.wait(), 2)
            await self.service.toggle(1, False)
            await asyncio.wait_for(self.service.bio_queue.join(), 2)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_evidence').fetchone()[0], 0)

    def fake_action_bot(self):
        class DeleteQueue:
            async def enqueue(self, group, ids, **kwargs): return (1, [])
        class Cleanup:
            def __init__(self): self.ttls = []
            def schedule(self, group, sent, *, ttl): self.ttls.append(ttl); return True
        class Queue:
            def __init__(self): self.jobs = []
            def enqueue(self, *args, **kwargs):
                self.jobs.append((args, kwargs))
                async def run():
                    try:
                        result = await kwargs['operation']()
                        if result is False: await kwargs['on_failure'](RuntimeError('moderation operation returned False'))
                        else: await kwargs['on_success'](result)
                    except Exception as e:
                        await kwargs['on_failure'](e)
                asyncio.create_task(run())
                return True
        client = SimpleNamespace(get_permissions=AsyncMock(return_value=SimpleNamespace(is_admin=False,is_creator=False)),
             get_entity=AsyncMock(return_value=SimpleNamespace(id=9,username='oela')),
             send_message=AsyncMock(side_effect=lambda *a,**k: SimpleNamespace(id=101)))
        return SimpleNamespace(client=client, bot_account_id=999, message_delete_queue=DeleteQueue(),
            notice_cleanup=Cleanup(), moderation_queue=Queue(),
            admin_actions=SimpleNamespace(mute_user=AsyncMock(return_value=True),ban_user=AsyncMock(return_value=True)))

    async def test_three_stage_warning_mute_and_group_mode(self):
        bot = self.fake_action_bot()
        self.service.bot = bot
        await self.service.toggle(1, True)
        evidence = {'signals': {'repeat': True}, 'ad_signal': False, 'channel_signal': False, 'repeat_count': 4, 'bio': {}}
        with patch('modules.admin_tools.has_admin_permission', return_value=False), patch('modules.vip_storage.is_vip', return_value=False), patch('modules.punishment_mode.is_mute', return_value=False):
            for stage in range(1, 4):
                await asyncio.sleep(.01)
                job = Job('1','9',stage,'یک پیام تکراری',33,time.time(),self.service.groups['1'])
                self.assertTrue(self.service._schedule_action(job,evidence))
                await self.service.action_queue.join()
                if stage == 1:
                    self.assertEqual(bot.notice_cleanup.ttls, [70])
                    bot.admin_actions.mute_user.assert_not_awaited()
                elif stage == 2:
                    self.assertEqual(bot.notice_cleanup.ttls, [70, 60])
                    self.assertEqual(bot.admin_actions.mute_user.await_args.args[2], 14400)
                    self.assertIn('درصورت تکرار دوباره اخراج',bot.client.send_message.await_args.args[1])
            bot.admin_actions.ban_user.assert_awaited_once()
            self.assertEqual(bot.client.send_message.await_count, 3)
            self.assertEqual(bot.notice_cleanup.ttls, [70,60,60])
            self.assertIn('مجازات: اخراج',bot.client.send_message.await_args.args[1])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select stage from vp_cycles').fetchone()[0],3)
            self.assertEqual([r[0] for r in db.execute('select status from vp_actions order by stage')], ['warned','punished','punished'])

    async def test_warning_failure_does_not_advance_cycle(self):
        bot = self.fake_action_bot()
        bot.client.send_message.side_effect = RuntimeError('send failure')
        self.service.bot = bot
        await self.service.toggle(1, True)
        evidence = {'signals': {'repeat': True}, 'ad_signal': False, 'channel_signal': False, 'repeat_count': 4, 'bio': {}}
        job = Job('1','9',1,'یک پیام تکراری',33,time.time(),self.service.groups['1'])
        with patch('modules.admin_tools.has_admin_permission', return_value=False), patch('modules.vip_storage.is_vip', return_value=False):
            self.service._schedule_action(job,evidence)
            await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_cycles').fetchone()[0],0)
            self.assertEqual(db.execute('select status from vp_actions').fetchone()[0],'failed')
        bot.admin_actions.mute_user.assert_not_awaited()

    async def test_activation_animation_updates_one_square_and_waits(self):
        from modules.virtual_protocol_ui import activate
        client = SimpleNamespace(send_message=AsyncMock(side_effect=[SimpleNamespace(id=100),SimpleNamespace(id=101)]), edit_message=AsyncMock())
        self.service.bot = SimpleNamespace(client=client)
        event = SimpleNamespace(chat_id=-1000000000007,sender_id=9,sender=SimpleNamespace(username='test'))
        await activate(self.service,event,step_seconds=.002)
        self.assertIn('7',self.service.groups)
        self.assertEqual(client.edit_message.await_count,20)
        self.assertEqual(client.send_message.await_count,1)
        self.assertEqual(client.send_message.await_args_list[0].args[1].count('❏'),1)
        self.assertEqual(client.edit_message.await_args_list[-2].args[2].count('❏'),20)

    async def test_activation_cancel_before_completion(self):
        from modules.virtual_protocol_ui import activate, deactivate
        client = SimpleNamespace(send_message=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(id=100)), edit_message=AsyncMock())
        self.service.bot = SimpleNamespace(client=client)
        event = SimpleNamespace(chat_id=7,sender_id=9,sender=SimpleNamespace(username='test'))
        task=asyncio.create_task(activate(self.service,event,step_seconds=.05))
        self.service.activations['7'] = task
        await asyncio.sleep(.02)
        await deactivate(self.service,event)
        self.assertTrue(task.cancelled())
        self.assertNotIn('7', self.service.groups)

    def test_template_entities_and_spoiler_offsets_are_utf16(self):
        from modules.virtual_protocol_texts import warning, enabled, progress
        from modules.profile_guard.texts import u16
        text, entities = warning('@oela','بیوگرافی',0,matched='تبلیغ تست')
        spoilers = [e for e in entities if type(e).__name__ == 'MessageEntitySpoiler']
        self.assertEqual(len(spoilers),1)
        e=spoilers[0]
        data=text.encode('utf-16-le')
        self.assertEqual(data[e.offset*2:(e.offset+e.length)*2].decode('utf-16-le'),'تبلیغ تست')
        quote = [e for e in entities if type(e).__name__ == 'MessageEntityBlockquote']
        self.assertEqual(len(quote),2)
        self.assertEqual(len([e for e in enabled(7,'@x')[1] if type(e).__name__ == 'MessageEntityBlockquote']),7)
        self.assertEqual(progress(20)[0].count('❏'),20)

    async def test_direct_lists_work_with_guards_off_on_first_message(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        profile={'status':'empty','about':None,'channel_title':None}
        with patch('modules.profile_guard.storage.is_enabled',return_value=False) as channel_enabled, patch('modules.ad_spam_guard.storage.is_enabled',return_value=False) as ad_enabled, patch('modules.profile_guard.filters.channel_names',return_value=['فیلم سوپر']), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=['واژه ممنوع']), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=profile)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            cases=[('ف.ی.ل.م سوپر','profile_list_in_message','تبلیغات'),('خدمات میدم','ad_list','تبلیغات'),('واژه ممنوع','group_word','هرزنامه')]
            for mid,(text,source,category) in enumerate(cases,1):
                uid=8+mid
                self.service.submit(1,uid,mid,text)
                await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
                with sqlite3.connect(self.db) as db:
                    level,raw=db.execute('select level,evidence from vp_evidence where message_id=?',(mid,)).fetchone()
                self.assertEqual(level,'strong')
                self.assertEqual(json.loads(raw)['direct_filter']['source'],source)
                self.assertIn('نـوع تشـخیص پـیام : '+category,bot.client.send_message.await_args.args[1])
                self.assertEqual(bot.notice_cleanup.ttls[-1],70)
            self.assertEqual(bot.client.send_message.await_count,3)
            channel_enabled.assert_not_called(); ad_enabled.assert_not_called()
            bot.admin_actions.mute_user.assert_not_awaited()

    async def test_first_normal_message_checks_bio_when_guards_off(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        profile={'status':'ok','about':'من ف.ی.ل.م سوپر دارم','channel_title':None}
        with patch('modules.profile_guard.storage.is_enabled',return_value=False), patch('modules.ad_spam_guard.storage.is_enabled',return_value=False), patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=['فیلم سوپر']), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=profile)) as snapshot, patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service.submit(1,9,1,'درود بر دوستان')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        snapshot.assert_awaited_once()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=1').fetchone()
        evidence=json.loads(raw)
        self.assertEqual(level,'strong')
        self.assertEqual(evidence['direct_filter']['source'],'profile_bio')
        self.assertEqual(evidence['bio']['matched'],'فیلم سوپر')
        self.assertIn('بیوگرافی',bot.client.send_message.await_args.args[1])
        self.assertEqual(bot.notice_cleanup.ttls,[70])

    async def test_first_normal_message_checks_attached_channel_when_guards_off(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        profile={'status':'ok','about':None,'channel_title':'فیلم سوپر','participants':23}
        with patch('modules.profile_guard.storage.is_enabled',return_value=False), patch('modules.ad_spam_guard.storage.is_enabled',return_value=False), patch('modules.profile_guard.filters.channel_names',return_value=['فیلم سوپر']), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=profile)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service.submit(1,9,1,'درود بر دوستان')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            ev=json.loads(db.execute('select evidence from vp_evidence').fetchone()[0])
        self.assertEqual(ev['direct_filter']['source'],'profile_channel')
        self.assertEqual(ev['bio']['channel_members'],23)
        self.assertIn('نام کانال بیوگرافی',bot.client.send_message.await_args.args[1])
        self.assertEqual(bot.notice_cleanup.ttls,[70])

    async def test_explicit_text_does_not_wait_for_slow_profile(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        started=asyncio.Event(); release=asyncio.Event()
        async def slow(*a,**k):
            started.set(); await release.wait(); return None
        with patch('modules.profile_guard.filters.channel_names',return_value=['عبارت تبلیغی خاص']), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.profile_guard.service.snapshot',side_effect=slow), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service.submit(1,9,1,'عبارت تبلیغی خاص')
            await self.flush(); await asyncio.wait_for(started.wait(),2)
            await asyncio.wait_for(self.service.action_queue.join(),2)
            self.assertEqual(bot.notice_cleanup.ttls,[70])
            release.set(); await self.service.bio_queue.join()

    async def test_single_common_list_word_is_explicit_violation(self):
        from modules.virtual_protocols import content_signals
        with patch('modules.profile_guard.filters.channel_names',return_value=['حال']), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]):
            self.assertEqual(content_signals('سلام حالت خوبه؟','1')[2:],('profile_list_in_message','حال'))

    async def test_direct_filter_progresses_all_three_stages(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        profile={'status':'empty','about':None,'channel_title':None}
        with patch('modules.profile_guard.filters.channel_names',return_value=['عبارت فیلتر']), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=profile)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False), patch('modules.punishment_mode.is_mute',return_value=False):
            for mid in range(1,4):
                self.service.submit(1,9,mid,'عبارت فیلتر')
                await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
            self.assertEqual(bot.notice_cleanup.ttls,[70,60,60])
            self.assertEqual(bot.admin_actions.mute_user.await_args.args[2],14400)
            bot.admin_actions.ban_user.assert_awaited_once()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select stage from vp_cycles').fetchone()[0],3)
            self.assertEqual(db.execute('select count(*) from vp_actions').fetchone()[0],3)

    async def test_disabled_protocol_does_not_scan_profile_or_act(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        with patch('modules.profile_guard.service.snapshot',new=AsyncMock()) as snapshot:
            self.assertFalse(self.service.submit(1,9,1,'خدمات میدم'))
            await asyncio.sleep(.01)
        snapshot.assert_not_awaited()
        bot.client.send_message.assert_not_awaited()
        self.assertFalse(self.db.exists())

    async def test_no_profile_data_is_not_positive_evidence(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service.submit(1,9,1,'درود بر دوستان')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        bot.client.send_message.assert_not_awaited()
        bot.admin_actions.mute_user.assert_not_awaited()

    async def test_end_to_end_detection_schedules_real_warning(self):
        bot = self.fake_action_bot()
        self.service.bot = bot
        await self.service.toggle(1, True)
        with patch('modules.admin_tools.has_admin_permission', return_value=False), patch('modules.vip_storage.is_vip', return_value=False), patch('modules.bio_link_service.fetch_about', new=AsyncMock(return_value=('empty',None))):
            for mid in range(1, 7):
                self.service.submit(1,9,mid,'عضو شو در کانال تبلیغات ما',reply=33)
            await self.flush()
            await self.service.bio_queue.join()
            await self.service.action_queue.join()
        self.assertEqual(bot.client.send_message.await_count,1)
        self.assertIn('هشدار پروتکل',bot.client.send_message.await_args.args[1])
        self.assertEqual(bot.notice_cleanup.ttls,[70])

    async def test_24_hour_cycle_slides_on_fresh_offense_and_dedup(self):
        await self.service.toggle(1, True)
        store=self.service.store
        ev={'signals': {'repeat': True}}
        now=time.time()
        j=Job('1','9',1,'نمونه پیام',None,now,self.service.groups['1'])
        self.assertEqual(store.reserve_stage(j,ev),1)
        store.confirm_stage(j,1)
        self.assertIsNone(store.reserve_stage(j,ev))
        future=Job('1','9',2,'نمونه پیام',None,now+20*3600,self.service.groups['1'])
        # Stage 3 ignores further punishment but must extend the reset clock.
        with sqlite3.connect(self.db) as db:
            db.execute('update vp_cycles set stage=3')
        self.assertIsNone(store.reserve_stage(future,ev))
        next_job=Job('1','9',3,'نمونه پیام',None,now+43*3600,self.service.groups['1'])
        self.assertIsNone(store.reserve_stage(next_job,ev))
        reset=Job('1','9',4,'نمونه پیام',None,now+68*3600,self.service.groups['1'])
        self.assertEqual(store.reserve_stage(reset,ev),1)

    async def test_final_mode_mute_and_failed_mute_do_not_advance(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        evidence={'signals': {'repeat':True}, 'ad_signal':False,'channel_signal':False,'repeat_count':4,'bio':{}}
        with patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False), patch('modules.punishment_mode.is_mute',return_value=True):
            def job(mid): return Job('1','9',mid,'تکرار پیام کامل',None,time.time(),self.service.groups['1'])
            self.service._schedule_action(job(1),evidence); await self.service.action_queue.join()
            bot.admin_actions.mute_user.return_value=False
            self.service._schedule_action(job(2),evidence); await self.service.action_queue.join()
            with sqlite3.connect(self.db) as db:
                self.assertEqual(db.execute('select stage from vp_cycles').fetchone()[0],1)
            bot.admin_actions.mute_user.return_value=True
            self.service._schedule_action(job(3),evidence); await self.service.action_queue.join()
            self.assertIn('درصورت تکرار دوباره سکوت',bot.client.send_message.await_args.args[1])
            self.service._schedule_action(job(4),evidence); await self.service.action_queue.join()
            bot.admin_actions.ban_user.assert_awaited_once()
            self.assertIn('مجازات: سکوت دائمی',bot.client.send_message.await_args.args[1])
            with sqlite3.connect(self.db) as db:
                self.assertEqual(db.execute('select penalty_kind from vp_actions where message_id=4').fetchone()[0],'mute')

    async def test_delete_failure_sends_no_warning_and_no_advance(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        bot.message_delete_queue.enqueue=AsyncMock(return_value=(0,[1]))
        await self.service.toggle(1,True)
        job=Job('1','9',1,'تکرار پیام کامل',None,time.time(),self.service.groups['1'])
        ev={'signals': {'repeat':True}, 'ad_signal':False,'channel_signal':False,'repeat_count':4,'bio':{}}
        with patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service._schedule_action(job,ev); await self.service.action_queue.join()
        bot.client.send_message.assert_not_awaited()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_cycles').fetchone()[0],0)
            self.assertEqual(db.execute('select status from vp_actions').fetchone()[0],'failed')

    async def test_stale_action_is_uncertain_and_blocks_retry(self):
        await self.service.toggle(1,True)
        ev={'signals': {'repeat':True}}
        gen=self.service.groups['1']
        j=Job('1','9',1,'تکرار پیام کامل',None,time.time(),gen)
        self.assertEqual(self.service.store.reserve_stage(j,ev),1)
        with sqlite3.connect(self.db) as db:
            db.execute('update vp_actions set ts=?',(time.time()-121,))
        later=Job('1','9',2,'تکرار پیام کامل',None,time.time(),gen)
        self.assertIsNone(self.service.store.reserve_stage(later,ev))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select status from vp_actions').fetchone()[0],'uncertain')

    async def test_disable_prevents_queued_punishment(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        ev={'signals': {'repeat':True}, 'ad_signal':False,'channel_signal':False,'repeat_count':4,'bio':{}}
        with patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            first=Job('1','9',1,'تکرار پیام کامل',None,time.time(),self.service.groups['1'])
            self.service._schedule_action(first,ev); await self.service.action_queue.join()
            original=bot.moderation_queue.enqueue
            queued=[]
            def hold(*args,**kwargs): queued.append(kwargs); return True
            bot.moderation_queue.enqueue=hold
            second=Job('1','9',2,'تکرار پیام کامل',None,time.time(),self.service.groups['1'])
            self.service._schedule_action(second,ev)
            for _ in range(100):
                if queued: break
                await asyncio.sleep(.005)
            self.assertTrue(queued)
            await self.service.toggle(1,False)
            self.assertFalse(await queued[0]['operation']())
            await queued[0]['on_failure'](RuntimeError('disabled'))
            await self.service.action_queue.join()
            bot.admin_actions.mute_user.assert_not_awaited()

    async def test_coordination_uses_multiple_ids_and_repeated_pattern(self):
        await self.service.toggle(1, True)
        text = 'این محتوای تکراری هماهنگ است'
        # Two other accounts, three messages total, plus repeat activity by target.
        for mid,uid in [(1,2),(2,3),(3,2),(4,9),(5,9),(6,9),(7,9)]:
            self.service.submit(1,uid,mid,text,reply=50)
        await self.flush()
        with sqlite3.connect(self.db) as db:
            ev = json.loads(db.execute('select evidence from vp_evidence where message_id=7').fetchone()[0])
        self.assertTrue(ev['signals']['coordination'])
        self.assertEqual(ev['coordinated_user_ids'], ['2','3'])

    async def test_conversation_uses_incoming_replies(self):
        await self.service.toggle(1, True)
        self.service.submit(1, 9, 1, 'سلام به همه دوستان')
        self.service.submit(1, 2, 2, 'سلام خوش آمدید', reply=1)
        self.service.submit(1, 3, 3, 'درود به دوست عزیز', reply=1)
        for mid, text in [(4,'امروز آسمان زیبا است'),(5,'امیدوارم روزتان عالی باشد'),(6,'من فردا کتاب میخوانم'),(7,'هوای بهار آرام است')]:
            self.service.submit(1,9,mid,text,reply=2)
        await self.flush()
        # Ask the pure store directly for diagnostics even when classified natural.
        job = Job('1','9',8,'متن دیگری درباره برنامه فردا',2,time.time(),self.service.groups['1'])
        result = self.service.store.process(job)
        self.assertEqual(result[0], 'natural')
        self.assertTrue(result[2]['conversation_indicator'])
        self.assertEqual(result[2]['incoming_distinct_users'],2)
        self.assertEqual(result[2]['outgoing_distinct_users'],1)

    async def test_queue_has_a_bounded_total_capacity(self):
        from modules.virtual_protocols import MAX_PENDING
        await self.service.toggle(1, True)
        await self.service.lock.acquire()
        for mid in range(1,MAX_PENDING+1):
            self.assertTrue(self.service.submit(1,9,mid,'محتوای نمونه برای صف'))
        self.assertFalse(self.service.submit(1,9,MAX_PENDING+1,'محتوای نمونه برای صف'))
        self.assertEqual(self.service.dropped,1)
        self.service.groups = {}
        self.service.lock.release()
        await self.flush()
        self.assertEqual(len(self.service.pending),0)

    async def test_repeated_reply_cycle_is_identified(self):
        await self.service.toggle(1, True)
        for mid,text in enumerate(['سلام خوبی', 'اوف خوبی', 'هوا خوبه']*2, 1):
            self.service.submit(1, 9, mid, text, reply=100+mid)
        await self.flush()
        with sqlite3.connect(self.db) as db:
            row = db.execute('select level,evidence from vp_evidence where message_id=6').fetchone()
        self.assertEqual(row[0], 'review')
        ev = json.loads(row[1])
        self.assertEqual(ev['cycle_size'], 3)
        self.assertTrue(ev['reply_only'])
        self.assertTrue(ev['signals']['cycle'])

    def test_periodicity_needs_several_timed_events(self):
        from modules.virtual_protocols import regular_interval
        self.assertFalse(regular_interval([0, 10, 20]))
        self.assertTrue(regular_interval([0, 10, 20, 30, 40]))
        self.assertFalse(regular_interval([0, 10, 23, 28, 51]))

    async def test_screenshot_different_short_replies_are_review_not_punished(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        texts=['اوف سلام','چخبر','جون','خوبیی','کجای','سلام']
        with patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            for mid,text in enumerate(texts,1):
                msg=SimpleNamespace(id=mid,message=text,reply_to=SimpleNamespace(reply_to_msg_id=100))
                event=SimpleNamespace(message=msg,chat_id=1,sender_id=9,is_private=False)
                self.assertFalse(await intercept(self.service,event))
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=6').fetchone()
        ev=json.loads(raw)
        self.assertEqual(level,'review')
        self.assertEqual(ev['same_target_replies_60_seconds'],6)
        self.assertFalse(ev['signals']['cycle'])
        bot.client.send_message.assert_not_awaited()
        bot.admin_actions.mute_user.assert_not_awaited()

    async def test_four_identical_short_replies_reach_first_warning(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        with patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False), patch('modules.bio_link_service.fetch_about',new=AsyncMock(return_value=('empty',None))):
            for mid in range(1,5): self.service.submit(1,9,mid,'سلام',reply=100)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=4').fetchone()
        self.assertEqual(level,'strong')
        self.assertEqual(json.loads(raw)['repeat_count'],4)
        self.assertEqual(bot.notice_cleanup.ttls,[70])
        bot.admin_actions.mute_user.assert_not_awaited()

    async def test_short_alternating_reply_cycle_reaches_warning(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        with patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False), patch('modules.bio_link_service.fetch_about',new=AsyncMock(return_value=('empty',None))):
            for mid,text in enumerate(['سلام','خوبی','چخبر']*2,1):
                self.service.submit(1,9,mid,text,reply=100)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=6').fetchone()
        ev=json.loads(raw)
        self.assertEqual(level,'strong')
        self.assertTrue(ev['signals']['cycle'])
        self.assertTrue(ev['signals']['reply_target_burst'])
        self.assertEqual(bot.notice_cleanup.ttls,[70])
        bot.admin_actions.mute_user.assert_not_awaited()

    async def test_short_slow_repeats_every_three_minutes_reach_real_warning(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        now=time.time(); generation=self.service.groups['1']
        with self.service.store.connect() as db:
            for mid in range(1,6):
                db.execute('insert into vp_messages values(?,?,?,?,?,?,?,?,?)',
                    ('1',generation,'9',mid,now-(6-mid)*180,'سلام',normalize('سلام'),None,0))
        with patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False), patch('modules.bio_link_service.fetch_about',new=AsyncMock(return_value=('empty',None))):
            self.service.submit(1,9,6,'سلام')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=6').fetchone()
        ev=json.loads(raw)
        self.assertEqual(level,'strong')
        self.assertTrue(ev['signals']['periodic'])
        self.assertTrue(ev['signals']['sustained_repeat'])
        self.assertFalse(ev['signals']['burst'])
        self.assertEqual(ev['repeat_count'],6)
        self.assertEqual(bot.notice_cleanup.ttls,[70])
        bot.admin_actions.mute_user.assert_not_awaited()

    async def test_irregular_slow_repetition_does_not_trigger_strong(self):
        store=self.service.store; now=time.time(); gen='g'
        with store.connect() as db:
            for mid,age in enumerate([1200,1000,700,380,60],1):
                db.execute('insert into vp_messages values(?,?,?,?,?,?,?,?,?)',('1',gen,'9',mid,now-age,'سلام','سلام',None,0))
        result=store.process(Job('1','9',6,'سلام',None,now,gen))
        self.assertNotEqual(result[0],'strong')
        self.assertFalse(result[2]['signals']['periodic'])

    def test_four_letter_word_normalization(self):
        from modules.virtual_protocols import message_words
        self.assertEqual(message_words('و من سلام، كِتاب! خُوبي 😊 سلام'), {'سلام','کتاب','خوبی'})

    async def test_one_repeated_word_is_recorded_without_automatic_action(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service.submit(1,9,1,'سلام دوستان')
            self.service.submit(1,9,2,'سلام همگی')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=2').fetchone()
        ev=json.loads(raw)
        self.assertEqual(level,'natural')
        self.assertIn({'word':'سلام','count':2},ev['repeated_words'])
        bot.client.send_message.assert_not_awaited()

    async def test_word_repetition_uses_real_24_hours_beyond_message_cap(self):
        store=self.service.store; now=time.time(); gen='g'
        with store.connect() as db:
            db.execute('insert into vp_word_hits values(?,?,?,?,?,?)',('1',gen,'9',1,now-86401,'سلام'))
            db.execute('insert into vp_word_hits values(?,?,?,?,?,?)',('1',gen,'9',2,now-86399,'سلام'))
            for mid in range(3,205):
                text='متن متفاوت '+str(mid)
                db.execute('insert into vp_messages values(?,?,?,?,?,?,?,?,?)',('1',gen,'9',mid,now-100,text,normalize(text),None,0))
        result=store.process(Job('1','9',205,'سلام همه دوستان',None,now,gen))
        self.assertIn({'word':'سلام','count':2},result[2]['repeated_words'])
        store.process(Job('1','9',205,'سلام همه دوستان',None,now,gen))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("select count(*) from vp_word_hits where message_id=205 and word='سلام'").fetchone()[0],1)

    async def test_repeated_greeting_to_different_people_warns_when_no_one_replies(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            for mid in range(1,5): self.service.submit(1,mid+20,mid,'درود دوستان')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
            for mid in range(5,9): self.service.submit(1,9,mid,'سلام',reply=mid-4)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=8').fetchone()
        ev=json.loads(raw)
        self.assertEqual(level,'strong')
        self.assertTrue(ev['signals']['repeat_multiple_users'])
        self.assertEqual(ev['repeated_reply_target_users'],['21','22','23','24'])
        self.assertEqual(bot.notice_cleanup.ttls,[70])

    async def test_human_receiving_real_replies_gets_conversation_protection(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service.submit(1,9,1,'سلام')
            for mid in range(2,5): self.service.submit(1,20+mid,mid,'درود دوستان',reply=1)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
            for mid in range(5,8): self.service.submit(1,9,mid,'سلام',reply=mid-3)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=7').fetchone()
        ev=json.loads(raw)
        self.assertNotEqual(level,'strong')
        self.assertTrue(ev['signals']['conversation'])
        self.assertGreaterEqual(ev['incoming_distinct_users'],1)
        bot.client.send_message.assert_not_awaited()

    async def test_slow_different_targets_beyond_message_cap(self):
        store=self.service.store; now=time.time(); gen='g'
        with store.connect() as db:
            for mid,age,target in [(1,3600,'21'),(2,1800,'22'),(3,600,'23')]:
                db.execute('insert into vp_phrase_hits values(?,?,?,?,?,?,?,?)',('1',gen,'9',mid,now-age,'سلام',100+mid,target))
            db.execute('insert into vp_messages values(?,?,?,?,?,?,?,?,?)',('1',gen,'24',300,now-10,'درود','درود',None,0))
            for mid in range(4,205):
                text='متن متفاوت شماره '+str(mid)
                db.execute('insert into vp_messages values(?,?,?,?,?,?,?,?,?)',('1',gen,'9',mid,now-5,text,normalize(text),None,0))
        result=store.process(Job('1','9',205,'سلام',300,now,gen))
        self.assertEqual(result[0],'strong')
        self.assertEqual(result[2]['repeat_count'],4)
        self.assertEqual(result[2]['repeated_reply_target_users'],['21','22','23','24'])
        self.assertFalse(result[2]['signals']['periodic'])
        self.assertIsNone(store.process(Job('1','9',1,'سلام',101,now,gen)))

    async def test_real_profile_api_shape_detects_kanalam_on_first_message(self):
        from splusthon.tl.types import User
        from splusthon.tl.functions.users import GetFullUserRequest
        from modules.profile_guard import service as profile_service
        profile_service._cache.clear(); profile_service._retry_after.clear()
        bot=self.fake_action_bot(); self.service.bot=bot
        bot.client.get_entity=AsyncMock(side_effect=ValueError('ID not cached'))
        await self.service.toggle(1,True)
        sender=User(id=9,user_type=None,access_hash=123,first_name='Test')
        event=SimpleNamespace(chat_id=1,sender_id=9,is_private=False,
            message=SimpleNamespace(id=1,message='درود دوستان',reply_to=None),
            get_sender=AsyncMock(return_value=sender))
        async def rpc(client, make_request, user_id, **kwargs):
            request=make_request()
            self.assertIsInstance(request,GetFullUserRequest)
            self.assertEqual(user_id,9)
            return SimpleNamespace(full_user=SimpleNamespace(about='کانالم',personal_channel_id=None),chats=[])
        with patch('modules.full_user_shared.get_full_user',side_effect=rpc) as fetch, patch('modules.profile_guard.service._throttle',new=AsyncMock()), patch('modules.profile_guard.storage.is_enabled',return_value=False), patch('modules.ad_spam_guard.storage.is_enabled',return_value=False), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            await intercept(self.service,event,bot_id=999)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        fetch.assert_awaited_once()
        event.get_sender.assert_awaited()
        self.assertEqual(bot.notice_cleanup.ttls,[70])
        self.assertIn('بیوگرافی',bot.client.send_message.await_args.args[1])
        with sqlite3.connect(self.db) as db:
            row=db.execute('select evidence from vp_evidence where message_id=1').fetchone()
        self.assertEqual(json.loads(row[0])['direct_filter']['source'],'profile_bio')
        profile_service._cache.clear(); profile_service._retry_after.clear()

    async def test_repeated_replies_to_bot_ignored_as_human_conversation(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        bot_event=SimpleNamespace(chat_id=1,sender_id=999,out=True,is_private=False,
            message=SimpleNamespace(id=100,message='پاسخ ربات',reply_to=None))
        await intercept(self.service,bot_event,bot_id=999)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            for mid,text in enumerate(['اوف سلام','چخبر','جون','خوبیی','کجای','سلام'],1):
                msg=SimpleNamespace(id=mid,message=text,reply_to=SimpleNamespace(reply_to_msg_id=100))
                event=SimpleNamespace(message=msg,chat_id=1,sender_id=9,is_private=False)
                await intercept(self.service,event,bot_id=999)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=6').fetchone()
        ev=json.loads(raw)
        self.assertEqual(level,'strong')
        self.assertTrue(ev['reply_to_own_bot'])
        self.assertTrue(ev['signals']['bot_reply_burst'])
        self.assertFalse(ev['signals']['conversation'])
        self.assertEqual(bot.notice_cleanup.ttls,[70])

    async def test_old_bot_target_resolved_from_actual_reply_without_history_scan(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        target=SimpleNamespace(id=100,sender_id=999,out=True)
        lookup=AsyncMock(return_value=target)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            for mid,text in enumerate(['اوف سلام','چخبر','جون','خوبیی','کجای','سلام'],1):
                msg=SimpleNamespace(id=mid,message=text,reply_to=SimpleNamespace(reply_to_msg_id=100),get_reply_message=lookup)
                await intercept(self.service,SimpleNamespace(message=msg,chat_id=1,sender_id=9,is_private=False),bot_id=999)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        lookup.assert_awaited_once()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=6').fetchone()
        self.assertEqual(level,'strong')
        self.assertTrue(json.loads(raw)['reply_to_own_bot'])
        self.assertEqual(bot.notice_cleanup.ttls,[70])

    async def test_reply_to_real_user_is_not_guessed_to_be_bot(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        lookup=AsyncMock(return_value=SimpleNamespace(id=100,sender_id=8,out=False))
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            for mid,text in enumerate(['اوف سلام','چخبر','جون','خوبیی','کجای','سلام'],1):
                msg=SimpleNamespace(id=mid,message=text,reply_to=SimpleNamespace(reply_to_msg_id=100),get_reply_message=lookup)
                await intercept(self.service,SimpleNamespace(message=msg,chat_id=1,sender_id=9,is_private=False),bot_id=999)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=6').fetchone()
        self.assertNotEqual(level,'strong')
        self.assertFalse(json.loads(raw)['reply_to_own_bot'])
        bot.client.send_message.assert_not_awaited()
        lookup.assert_awaited_once()

    async def test_disabling_cancels_inflight_reply_lookup(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        started=asyncio.Event()
        async def slow():
            started.set()
            await asyncio.sleep(100)
        msg=SimpleNamespace(id=1,message='درود دوستان',reply_to=SimpleNamespace(reply_to_msg_id=100),get_reply_message=slow)
        event=SimpleNamespace(message=msg,chat_id=1,sender_id=9,is_private=False)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]):
            await intercept(self.service,event,bot_id=999)
            await asyncio.wait_for(started.wait(),2)
            await self.service.toggle(1,False)
            await asyncio.wait_for(self.service.reply_queue.join(),2)
            await self.service.queue.join()
        bot.client.send_message.assert_not_awaited()
        self.assertEqual(len(self.service.pending),0)

    async def test_changed_bio_rechecked_after_short_cache(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        profiles=[{'status':'ok','about':None,'channel_title':None},{'status':'ok','about':'کانالم','channel_title':None}]
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=['کانالم']), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(side_effect=profiles)) as snapshot, patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            self.service.submit(1,9,1,'درود دوستان')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
            bot.client.send_message.assert_not_awaited()
            self.service.bio_recent[('1','9')] -= 6
            self.service.submit(1,9,2,'درود دوستان تازه')
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        self.assertEqual(snapshot.await_count,2)
        self.assertEqual(bot.notice_cleanup.ttls,[70])
        self.assertIn('بیوگرافی',bot.client.send_message.await_args.args[1])

    async def test_bot_reply_to_user_is_not_human_response(self):
        bot=self.fake_action_bot(); self.service.bot=bot
        await self.service.toggle(1,True)
        with patch('modules.profile_guard.filters.channel_names',return_value=[]), patch('modules.profile_guard.filters.bio_sentences',return_value=[]), patch('modules.group_words_storage.get_words',return_value=[]), patch('modules.profile_guard.service.snapshot',new=AsyncMock(return_value=None)), patch('modules.admin_tools.has_admin_permission',return_value=False), patch('modules.vip_storage.is_vip',return_value=False):
            for mid in range(1,5):
                self.service.remember_bot_message(1,100+mid)
                msg=SimpleNamespace(id=mid,message='سلام',reply_to=SimpleNamespace(reply_to_msg_id=100+mid))
                event=SimpleNamespace(message=msg,chat_id=1,sender_id=9,is_private=False)
                await intercept(self.service,event,bot_id=999)
                outgoing=SimpleNamespace(chat_id=1,sender_id=999,out=True,is_private=False,message=SimpleNamespace(id=200+mid,message='درود',reply_to=SimpleNamespace(reply_to_msg_id=mid)))
                await intercept(self.service,outgoing,bot_id=999)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            level,raw=db.execute('select level,evidence from vp_evidence where message_id=4').fetchone()
        ev=json.loads(raw)
        self.assertEqual(level,'strong')
        self.assertFalse(ev['signals']['conversation'])
        self.assertEqual(ev['incoming_distinct_users'],0)
        self.assertTrue(ev['signals']['bot_repeat'])
        self.assertEqual(bot.notice_cleanup.ttls,[70])

    async def test_simple_reply_registers_its_sent_id_for_protocol(self):
        from handlers.message_handler import _schedule_reply
        bot=self.fake_action_bot(); bot.virtual_protocols=self.service; self.service.bot=bot
        await self.service.toggle(1,True)
        event=SimpleNamespace(chat_id=1,message=SimpleNamespace(id=1),reply=AsyncMock(return_value=SimpleNamespace(id=125)))
        await _schedule_reply(bot,event,'سلام')
        self.assertIn(('1',self.service.groups['1'],125),self.service.bot_messages)
        await self.service.toggle(1,False)
        self.assertFalse(self.service.bot_messages)

    async def test_feedback_requires_new_strong_matching_activity(self):
        await self.service.toggle(1, True)
        text = 'عضو شو در کانال تبلیغات ما'
        for mid in range(1, 5): self.service.submit(1,9,mid,text,reply=33)
        await self.flush()
        with patch('modules.admin_tools.has_admin_permission', return_value=True):
            self.assertTrue(await self.service.mark_false_positive(1, 20, 4))
        await asyncio.sleep(.01)
        for mid in range(5, 11): self.service.submit(1,9,mid,'پیام متفاوت جدید شماره ' + str(mid),reply=33)
        await self.flush()
        gen = self.service.groups['1']
        j = Job('1','9',11,text,33,time.time(),gen)
        self.service.store.process(j)
        evidence = {'signals': {'repeat': True}}
        self.assertIsNone(self.service.store.reserve_stage(j,evidence))
        for mid in range(12,17):
            j = Job('1','9',mid,text,33,time.time(),gen)
            self.service.store.process(j)
        self.assertEqual(self.service.store.reserve_stage(j,evidence),1)

    async def test_native_admin_lookup_fail_closed(self):
        bot = SimpleNamespace(bot_account_id=999, client=SimpleNamespace(get_permissions=AsyncMock(side_effect=RuntimeError('network'))))
        self.service.bot = bot
        await self.service.toggle(1, True)
        with patch('modules.admin_tools.has_admin_permission', return_value=False), patch('modules.vip_storage.is_vip', return_value=False), patch('modules.bio_link_service.fetch_about', new=AsyncMock(return_value=('empty',None))):
            for mid in range(1, 5): self.service.submit(1,9,mid,'عضو شو در کانال تبلیغات ما',reply=33)
            await self.flush(); await self.service.bio_queue.join(); await self.service.action_queue.join()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_actions').fetchone()[0],0)

    async def test_existing_splusthon_bio_api_shape(self):
        try:
            from splusthon.tl.functions.users import GetFullUserRequest
            from splusthon.tl.types import InputPeerUser
        except ImportError:
            self.skipTest('SPlusthon is not installed')
        from modules.bio_link_service import fetch_about
        client = SimpleNamespace(get_input_entity=AsyncMock(return_value=InputPeerUser(9,123)))
        async def shared(client_arg, request_factory, user_id, **kwargs):
            request = request_factory()
            self.assertIsInstance(request, GetFullUserRequest)
            self.assertEqual(request.id.user_id, 9)
            return SimpleNamespace(full_user=SimpleNamespace(about='متن بیو'))
        with patch('modules.full_user_shared.get_full_user', side_effect=shared):
            status, about = await fetch_about(SimpleNamespace(client=client), 9)
        self.assertEqual((status, about), ('ok', 'متن بیو'))
        client.get_input_entity.assert_awaited_once_with(9)

    async def test_periodic_cleanup_is_bounded_and_respects_ttl(self):
        self.service.store.stage(Job('1','8',1,'متن',None,time.time()-121,'g'))
        self.service.store.maintain()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from vp_pending').fetchone()[0],0)

    def test_anti_channel_uses_existing_list(self):
        from modules.virtual_protocols import channel_signal
        with patch('modules.profile_guard.filters.channel_names', return_value=['کانال آزمایشی']), patch('modules.profile_guard.filters.bio_sentences', return_value=[]):
            self.assertTrue(channel_signal('این کانال آزمایشی است'))
            self.assertFalse(channel_signal('یک متن بی ارتباط'))

    def test_malformed_policy_is_safe(self):
        from modules.virtual_protocols import load_policy, WEIGHTS, THRESHOLDS
        policy = Path(self.tmp.name)/'bad.json'
        policy.write_text(json.dumps({'weights': 'bad', 'thresholds': {'review': 99}, 'federation': 'bad'}))
        weights, thresholds, federation = load_policy(policy)
        self.assertEqual(weights, WEIGHTS)
        self.assertEqual(thresholds, THRESHOLDS)
        self.assertEqual(federation, [])

if __name__ == '__main__':
    unittest.main()
