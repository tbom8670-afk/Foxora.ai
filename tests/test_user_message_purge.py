"""🧹 تست قابلیت «پاک کامل» — پاکسازی همهٔ پیام‌های یک کاربر با ریپلای.

پوشش:
  ۱. تشخیص دستور (فارسی/انگلیسی، نیم‌فاصله، پیشوند) و رد دستورهای هم‌خانواده
  ۲. جمع‌آوری شناسه‌ها: ردیاب حافظه‌ای ∪ اسکن ``iter_messages``، پرش از
     پیام سیستمی/ربات/دیگران، کف تاریخ، FloodWait و fallback از ``from_user``
  ۳. حذف دسته‌ای: صف موجود پروژه با ``priority=0`` و مسیر مستقیم با
     دسته‌های ۱۰۰تایی + ``revoke=True`` + retry
  ۴. مسیریابی واقعی هندلر: مجوز admins.json/مالک، نبود ریپلای، و اینکه
     «پاک کامل» توسط بلوک «پاک + عدد» بلعیده نمی‌شود
  ۵. قالب دقیق تأیید نهایی: «پیام های کاربر پاک شد تعداد : 𝟒»
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import handlers.message_handler as mh
from handlers.message_handler import handle_new_message
from modules import admin_storage, admin_tools, group_storage, message_tracker
from modules import user_message_purge as purge
from modules.group_dispatch import PRIORITY_ADMIN, classify_priority


class FakeLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))

    def log_warning(self, *args, **kwargs):
        pass


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"FloodWait {seconds}")
        self.seconds = seconds


def _msg(message_id, sender_id, *, action=None, date=None):
    return SimpleNamespace(
        id=message_id, sender_id=sender_id, action=action, date=date
    )


# ---------------------------------------------------------------------------
# ۱) تشخیص دستور + قالب تأیید + مسیریابی dispatch
# ---------------------------------------------------------------------------

def test_command_matching_persian_and_english():
    for text in (
        "پاک کامل",
        "پاک‌کامل",          # نیم‌فاصله
        "پاک  کامل",          # فاصلهٔ تکراری
        "!پاک کامل",
        "/پاک کامل",
        "purge user",
        "PURGE",
        "  پاک کامل  ",
    ):
        assert purge.is_purge_command(text), text
    for text in ("پاک", "پاک 10", "پاک لیست سکوت", "پاکسازی خودکار", "سلام", ""):
        assert not purge.is_purge_command(text), text


def test_confirmation_text_exact_format():
    assert purge.confirmation_text(4) == "پیام های کاربر پاک شد تعداد : 𝟒"
    assert purge.confirmation_text(0) == "پیام های کاربر پاک شد تعداد : \U0001d7ce"
    assert purge.confirmation_text(123) == "پیام های کاربر پاک شد تعداد : \U0001d7cf\U0001d7d0\U0001d7d1"


def test_dispatch_routes_purge_to_admin_lane():
    priority, kind = classify_priority("پاک کامل")
    assert priority == PRIORITY_ADMIN and kind == "admin"
    priority, _kind = classify_priority("پاک‌کامل")
    assert priority == PRIORITY_ADMIN


def test_scan_limit_env_clamp(monkeypatch):
    monkeypatch.delenv("FOXORA_PURGE_SCAN_LIMIT", raising=False)
    assert purge.scan_limit() == purge.DEFAULT_SCAN_LIMIT
    monkeypatch.setenv("FOXORA_PURGE_SCAN_LIMIT", "10")
    assert purge.scan_limit() == 100
    monkeypatch.setenv("FOXORA_PURGE_SCAN_LIMIT", "999999")
    assert purge.scan_limit() == 20000
    monkeypatch.setenv("FOXORA_PURGE_SCAN_LIMIT", "نامعتبر")
    assert purge.scan_limit() == purge.DEFAULT_SCAN_LIMIT


# ---------------------------------------------------------------------------
# ۲) جمع‌آوری شناسه‌ها از تاریخچه
# ---------------------------------------------------------------------------

def test_collect_merges_tracker_history_and_skips_service_bot_others():
    async def scenario():
        message_tracker.reset_all()
        chat_id, target_id = -1001, 42
        message_tracker.add_message(chat_id, target_id, 16, "ردیاب")

        class Client:
            # فقط (target, limit) — مثل فیک‌های قدیمی پروژه؛ ماژول باید
            # خودش از kwargs اضافه (from_user/wait_time) عقب بنشیند.
            async def iter_messages(self, target, limit):
                for message in (
                    _msg(10, target_id),
                    _msg(11, target_id),
                    _msg(12, target_id, action=object()),  # سیستمی
                    _msg(13, 9999),                          # خود ربات
                    _msg(14, 7),                             # کاربر دیگر
                    _msg(15, target_id),
                    _msg(15, target_id),                     # تکراری
                    _msg(99, target_id),                     # پیام دستور (حذف‌شده از لیست)
                ):
                    yield message

        bot = SimpleNamespace(
            client=Client(), logger=FakeLogger(), bot_account_id=9999
        )
        ids, scanned = await purge.collect_user_message_ids(
            bot, chat_id, target_id,
            seed_ids=(10,), exclude_ids=(99,), since=False,
        )
        return ids, scanned

    ids, scanned = asyncio.run(scenario())
    assert ids == {10, 11, 15, 16}
    assert scanned == 8
    message_tracker.reset_all()


def test_collect_since_stops_at_activation_floor():
    async def scenario():
        since = datetime(2026, 1, 10, tzinfo=timezone.utc)

        class Client:
            async def iter_messages(self, target, limit=None, **kwargs):
                for message in (
                    _msg(30, 42, date=datetime(2026, 1, 12, tzinfo=timezone.utc)),
                    _msg(29, 42, date=datetime(2026, 1, 11, tzinfo=timezone.utc)),
                    _msg(28, 42, date=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                    _msg(27, 42, date=datetime(2025, 12, 1, tzinfo=timezone.utc)),
                ):
                    yield message

        bot = SimpleNamespace(client=Client(), logger=FakeLogger(), bot_account_id=1)
        return await purge.collect_user_message_ids(
            bot, -1002, 42, since=since
        )

    ids, _scanned = asyncio.run(scenario())
    assert ids == {30, 29}


def test_collect_falls_back_when_from_user_rejected():
    async def scenario():
        calls = []

        class Client:
            async def iter_messages(self, target, limit=None, **kwargs):
                calls.append(kwargs)
                if kwargs.get("from_user") is not None:
                    raise RuntimeError("SEARCH not supported")
                yield _msg(50, 42)
                yield _msg(51, 7)

        bot = SimpleNamespace(client=Client(), logger=FakeLogger(), bot_account_id=1)
        ids, _ = await purge.collect_user_message_ids(bot, -1003, 42, since=False)
        return ids, calls

    ids, calls = asyncio.run(scenario())
    assert ids == {50}
    assert any(c.get("from_user") is not None for c in calls)
    assert any(c.get("from_user") is None for c in calls)


def test_collect_retries_after_floodwait():
    async def scenario():
        attempts = {"n": 0}

        class Client:
            async def iter_messages(self, target, limit=None, **kwargs):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise FloodWaitError(1)
                yield _msg(60, 42)

        bot = SimpleNamespace(client=Client(), logger=FakeLogger(), bot_account_id=1)
        ids, _ = await purge.collect_user_message_ids(bot, -1004, 42, since=False)
        return ids, attempts["n"]

    ids, attempts = asyncio.run(scenario())
    assert ids == {60}
    assert attempts == 2


# ---------------------------------------------------------------------------
# ۳) حذف دسته‌ای
# ---------------------------------------------------------------------------

def test_delete_direct_batches_of_100_with_revoke():
    async def scenario():
        calls = []

        class Client:
            async def delete_messages(self, chat_id, ids, revoke=False):
                calls.append((list(ids), revoke))

        bot = SimpleNamespace(
            client=Client(), logger=FakeLogger(), message_delete_queue=None
        )
        deleted, remaining = await purge.delete_ids_in_batches(
            bot, -1005, list(range(1, 251))
        )
        return deleted, remaining, calls

    deleted, remaining, calls = asyncio.run(scenario())
    assert deleted == 250 and remaining == []
    assert [len(ids) for ids, _revoke in calls] == [100, 100, 50]
    assert all(revoke is True for _ids, revoke in calls)


def test_delete_direct_retries_floodwait_batch():
    async def scenario():
        state = {"failed_once": False}
        calls = []

        class Client:
            async def delete_messages(self, chat_id, ids, revoke=True):
                calls.append(list(ids))
                if not state["failed_once"]:
                    state["failed_once"] = True
                    raise FloodWaitError(1)

        bot = SimpleNamespace(
            client=Client(), logger=FakeLogger(), message_delete_queue=None
        )
        deleted, remaining = await purge.delete_ids_in_batches(bot, -1006, [1, 2])
        return deleted, remaining, calls

    deleted, remaining, calls = asyncio.run(scenario())
    assert deleted == 2 and remaining == []
    assert calls == [[1, 2], [1, 2]]


def test_delete_prefers_existing_queue_priority0():
    async def scenario():
        recorded = {}

        class Queue:
            def enqueue(self, chat_id, ids, *, priority=1):
                recorded["chat_id"] = chat_id
                recorded["ids"] = list(ids)
                recorded["priority"] = priority
                future = asyncio.get_running_loop().create_future()
                future.set_result((2, [3]))
                return future

        bot = SimpleNamespace(
            client=SimpleNamespace(), logger=FakeLogger(),
            message_delete_queue=Queue(),
        )
        deleted, remaining = await purge.delete_ids_in_batches(bot, -1007, [3, 1, 2])
        return deleted, remaining, recorded

    deleted, remaining, recorded = asyncio.run(scenario())
    assert (deleted, remaining) == (2, [3])
    assert recorded == {"chat_id": -1007, "ids": [1, 2, 3], "priority": 0}


# ---------------------------------------------------------------------------
# ۴) مسیر واقعی هندلر (handle_new_message)
# ---------------------------------------------------------------------------

class _HandlerUser:
    def __init__(self, uid, username=None, name="کاربر"):
        self.id = uid
        self.first_name = name
        self.last_name = None
        self.username = username


class _HandlerEvent:
    def __init__(self, chat_id, sender, text, *, reply_to_id=None, mid=900):
        self.chat_id = chat_id
        self.sender = sender
        self.sender_id = sender.id
        self.is_private = False
        self.out = False
        self._chat = SimpleNamespace(id=chat_id, title="گروه تست پاک کامل")
        self.message = SimpleNamespace(
            id=mid, message=text, caption="", reply_to_msg_id=reply_to_id,
            reply_to=None, entities=None, file=None, media=None,
            fwd_from=None, edit_date=None,
        )
        self.reply_to = (
            SimpleNamespace(reply_to_msg_id=reply_to_id) if reply_to_id else None
        )
        self.replies = []

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return self._chat

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(id=5000 + len(self.replies))

    async def respond(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(id=6000 + len(self.replies))


class _HandlerClient:
    def __init__(self, reply_sender, history):
        self._reply_sender = reply_sender
        self._history = history
        self.iter_calls = []

    async def get_messages(self, chat_id, ids=None, limit=None):
        sender = self._reply_sender
        return SimpleNamespace(
            id=ids, sender_id=getattr(sender, "id", None),
            get_sender=_sender_getter(sender),
        )

    async def iter_messages(self, target, limit=None, **kwargs):
        self.iter_calls.append(kwargs)
        for message in self._history:
            yield message

    async def get_entity(self, target):
        return self._reply_sender

    async def delete_messages(self, *args, **kwargs):
        return None


def _sender_getter(sender):
    async def _get_sender():
        return sender

    return _get_sender


class _HandlerQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, chat_id, ids, *, priority=1):
        self.calls.append((chat_id, list(ids), priority))
        future = asyncio.get_running_loop().create_future()
        future.set_result((len(ids), []))
        return future


def _handler_bot(client):
    queue = _HandlerQueue()
    bot = SimpleNamespace(
        client=client,
        logger=FakeLogger(),
        bot_account_id=999999,
        admin_actions=SimpleNamespace(bot_account_id=999999),
        moderation_queue=SimpleNamespace(enqueue=lambda *a, **k: True),
        message_delete_queue=queue,
        outgoing_sender=None,
        notice_cleanup=None,
        punished_users=set(),
        spam_burst_messages={}, spam_burst_users=set(), spammer_messages={},
        rejoin_spam_state={}, group_timer_tasks={}, reply_input_peer_cache={},
        config_manager=SimpleNamespace(get=lambda _k, default=None: default),
        detector=SimpleNamespace(
            is_spam=lambda *a, **k: (False, None),
            check_message=lambda *a, **k: (False, None),
            check_banned_words=lambda *a, **k: (False, None),
        ),
        tracker=SimpleNamespace(
            muted_users={}, banned_users={},
            get_count=lambda *a, **k: 0, increment=lambda *a, **k: 1,
            decrement=lambda *a, **k: 0, reset_count=lambda *a, **k: None,
            should_punish=lambda *a, **k: False,
        ),
        is_spam_locked=lambda *a, **k: False,
        clear_spam_lock=lambda *a, **k: None,
        clear_released_user_state=lambda *a, **k: None,
    )
    return bot, queue


def _isolate_handler_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    group_storage._cache = None
    group_storage._cache_mtime = None
    monkeypatch.setattr(admin_storage, "FILE", tmp_path / "admins.json")
    admin_storage._cache = None
    admin_storage._cache_mtime = None
    monkeypatch.setattr(admin_tools, "log_action", lambda *a, **k: None)
    monkeypatch.setattr(mh, "add_deleted_count", lambda *a, **k: None)
    message_tracker.reset_all()


async def _wait_replies(event, needle, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if any(needle in str(reply) for reply in event.replies):
            return True
        await asyncio.sleep(0.02)
    return any(needle in str(reply) for reply in event.replies)


def test_handler_purge_success_exact_confirmation(tmp_path, monkeypatch):
    _isolate_handler_storage(tmp_path, monkeypatch)
    chat_id = -100777001
    mh.DELETE_COMMAND_COOLDOWNS.pop(chat_id, None)
    owner = _HandlerUser(7001, username="purge_owner", name="مالک")
    target = _HandlerUser(7002, username="purge_target", name="هدف")
    group_storage.activate_group(chat_id, "گروه تست پاک کامل")
    group_storage.set_group_owner(chat_id, owner.id)

    history = [_msg(101, target.id), _msg(102, 7003), _msg(103, target.id),
               _msg(104, target.id), _msg(105, target.id)]
    client = _HandlerClient(target, history)
    bot, queue = _handler_bot(client)

    async def scenario():
        event = _HandlerEvent(chat_id, owner, "پاک کامل", reply_to_id=101, mid=900)
        await handle_new_message(bot, event)
        ok = await _wait_replies(event, "پیام های کاربر پاک شد")
        return event, ok

    event, ok = asyncio.run(scenario())
    assert ok, event.replies
    # دقیقاً قالب خواسته‌شده و دقیقاً ۴ پیام کاربر هدف (نه کاربر دیگر).
    assert "پیام های کاربر پاک شد تعداد : 𝟒" in event.replies
    assert not any("استفاده: پاک + عدد" in str(r) for r in event.replies)
    assert queue.calls and queue.calls[0][1] == [101, 103, 104, 105]
    assert queue.calls[0][2] == 0  # priority دستی ادمین


def test_handler_purge_denied_for_unregistered_user(tmp_path, monkeypatch):
    _isolate_handler_storage(tmp_path, monkeypatch)
    chat_id = -100777002
    mh.DELETE_COMMAND_COOLDOWNS.pop(chat_id, None)
    owner = _HandlerUser(7011, username="purge_owner2")
    stranger = _HandlerUser(7012, username="purge_stranger")
    target = _HandlerUser(7013, username="purge_target2")
    group_storage.activate_group(chat_id, "گروه تست پاک کامل ۲")
    group_storage.set_group_owner(chat_id, owner.id)

    client = _HandlerClient(target, [_msg(201, target.id)])
    bot, queue = _handler_bot(client)

    async def scenario():
        event = _HandlerEvent(
            chat_id, stranger, "پاک کامل", reply_to_id=201, mid=901
        )
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        return event

    event = asyncio.run(scenario())
    assert any(
        "فقط ادمین های ثبت شده مجاز هستند" in str(r) for r in event.replies
    )
    assert queue.calls == []


def test_handler_purge_requires_reply(tmp_path, monkeypatch):
    _isolate_handler_storage(tmp_path, monkeypatch)
    chat_id = -100777003
    mh.DELETE_COMMAND_COOLDOWNS.pop(chat_id, None)
    owner = _HandlerUser(7021, username="purge_owner3")
    target = _HandlerUser(7022, username="purge_target3")
    group_storage.activate_group(chat_id, "گروه تست پاک کامل ۳")
    group_storage.set_group_owner(chat_id, owner.id)

    client = _HandlerClient(target, [_msg(301, target.id)])
    bot, queue = _handler_bot(client)

    async def scenario():
        event = _HandlerEvent(chat_id, owner, "پاک کامل", reply_to_id=None, mid=902)
        await handle_new_message(bot, event)
        await asyncio.sleep(0.05)
        return event

    event = asyncio.run(scenario())
    assert any("باید روی پیام کاربر ریپلای کنید" in str(r) for r in event.replies)
    assert queue.calls == []
