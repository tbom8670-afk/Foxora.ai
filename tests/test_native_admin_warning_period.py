"""Native-only warning threshold: five-minute delete, one formatted notice, release."""

import asyncio
import inspect
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold

from handlers import message_handler as handler
from modules import admin_storage, admin_tools, bio_link_storage, message_tracker, vip_storage
from modules import native_admin_warning_lock as warning_lock
from modules import warning_threshold
from core.bot_working_split_ok import SoroushAntiSpamBot

# Reuse the full-message integration harness rather than mocking the warning
# branch. Its fake detector, tracker, delete queue and moderation queue run the
# actual handler, including the registered-owner «حذف اخطار» command.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bio_link_guard import (  # noqa: E402
    FakeEvent, _make_bot, _setup_group,
)


def _u16_slice(text, entity):
    data = text.encode("utf-16-le")
    start, end = entity.offset * 2, (entity.offset + entity.length) * 2
    return data[start:end].decode("utf-16-le")


def _user(user_id=440044):
    return SimpleNamespace(id=user_id, username="oeuia", first_name="ادمین")


def test_reference_notice_uses_real_quotes_bold_and_utf16_offsets():
    text, entities = handler._native_admin_threshold_notice(_user())
    assert text == (
        "🪸 ادمین گرامی : @oeuia\n"
        "به دلیل کامل شدن تخلفات شما\n"
        "پیام های شما به مدت 5 دقیقه پاک میشوند\n\n"
        "⚠️ : برای رفع زودتر مالک یا ادمین ثبت شده ریپلای\n"
        "کنید روی پیام ادمین بنویسید\n"
        "حذف اخطار"
    )
    assert [type(item) for item in entities] == [
        MessageEntityBlockquote, MessageEntityBold, MessageEntityBlockquote
    ]
    assert [_u16_slice(text, item) for item in entities] == [
        "🪸 ادمین گرامی : @oeuia",
        "به دلیل کامل شدن تخلفات شما\nپیام های شما به مدت 5 دقیقه پاک میشوند",
        "⚠️ : برای رفع زودتر مالک یا ادمین ثبت شده ریپلای\n"
        "کنید روی پیام ادمین بنویسید\nحذف اخطار",
    ]
    assert entities[1].offset == len("🪸 ادمین گرامی : @oeuia\n".encode("utf-16-le")) // 2
    assert "**" not in text and text.count("حذف اخطار") == 1
    # The displayed name is an @username when available, never a raw ID.
    assert "440044" not in text
    unnamed = SimpleNamespace(id=123, first_name="محمد", last_name="رضا", username=None)
    assert handler._native_admin_threshold_notice(unnamed)[0].startswith(
        "🪸 ادمین گرامی : محمد رضا\n"
    )


def test_lock_is_per_bot_group_user_and_expires_without_refresh():
    bot = SimpleNamespace()
    other_bot = SimpleNamespace()
    chat, uid = -1_000_000_123_456, 54321
    short_chat = 123456
    assert warning_lock.begin(bot, chat, uid, trigger_message_id=700, now=1000.0)
    assert warning_lock.is_active(bot, short_chat, str(uid), now=1299.999)
    assert not warning_lock.is_active(other_bot, chat, uid, now=1001.0)
    assert not warning_lock.is_active(bot, chat, uid + 1, now=1001.0)
    assert not warning_lock.is_active(bot, -1_000_000_123_457, uid, now=1001.0)
    assert not warning_lock.begin(bot, short_chat, uid, trigger_message_id=701, now=1200.0)
    assert warning_lock.is_active(bot, chat, uid, now=1299.999)
    assert not warning_lock.is_active(bot, chat, uid, now=1300.0)
    assert warning_lock.begin(bot, chat, uid, trigger_message_id=705, now=1300.0)
    assert warning_lock.release(bot, short_chat, str(uid))
    assert not warning_lock.is_active(bot, chat, uid, now=1301.0)


def test_drop_after_threshold_including_media_and_edits(monkeypatch):
    monkeypatch.setattr(admin_tools, "has_admin_permission", lambda *_a, **_kw: False)
    monkeypatch.setattr(vip_storage, "is_vip", lambda *_a, **_kw: False)
    batches = []
    bot = SimpleNamespace(
        bot_account_id=9999,
        message_delete_queue=SimpleNamespace(
            enqueue=lambda chat, ids, **kw: batches.append((chat, list(ids), kw))
        ),
    )
    chat, user_id = -1_000_000_700_701, 56789
    assert warning_lock.begin(bot, chat, user_id, trigger_message_id=70)
    event = SimpleNamespace(
        chat_id=chat, sender_id=user_id, is_private=False,
        message=SimpleNamespace(id=70, message="متن آستانه"),
    )
    assert not warning_lock.drop_if_active(bot, event)
    event.message.id = 69  # Old edit or event delivered after the trigger.
    assert not warning_lock.drop_if_active(bot, event)
    event.message.id = 71
    event.message.message = ""  # captionless photo / sticker / voice
    event.message.file = object()
    event.message._input_chat = object()
    assert warning_lock.drop_if_active(bot, event)
    assert batches[0][1] == [71]
    assert batches[0][2]["rpc_peer"] is event.message._input_chat

    event.message.id = 72
    event.message.message = "قفل"  # Even management-looking text is dropped.
    assert warning_lock.drop_if_active(bot, event)
    assert batches[-1][1] == [72]
    event.is_private = True
    event.message.id = 73
    assert not warning_lock.drop_if_active(bot, event)
    assert len(batches) == 2
    event.is_private = False
    event.sender_id = None
    event.original_update = SimpleNamespace(from_id=SimpleNamespace(user_id=user_id))
    event.message.id = 74
    assert warning_lock.drop_if_active(bot, event)
    assert batches[-1][1] == [74]


def test_registered_admin_or_vip_can_escape_independent_lock(monkeypatch):
    permitted = {"admin": False, "vip": False}
    monkeypatch.setattr(
        admin_tools, "has_admin_permission",
        lambda *_a, **_kw: permitted["admin"],
    )
    monkeypatch.setattr(vip_storage, "is_vip", lambda *_a, **_kw: permitted["vip"])
    batches = []
    bot = SimpleNamespace(
        bot_account_id=9999,
        message_delete_queue=SimpleNamespace(
            enqueue=lambda _chat, ids, **_kw: batches.append(ids)
        ),
    )
    event = SimpleNamespace(
        chat_id=-1_000_000_700_702, sender_id=77, is_private=False,
        message=SimpleNamespace(id=101, message="پیام"),
    )
    warning_lock.begin(bot, event.chat_id, 77, trigger_message_id=100)
    permitted["admin"] = True
    assert not warning_lock.drop_if_active(bot, event)
    assert not warning_lock.is_active(bot, event.chat_id, 77)
    warning_lock.begin(bot, event.chat_id, 77, trigger_message_id=100)
    permitted["admin"] = False
    permitted["vip"] = True
    assert not warning_lock.drop_if_active(bot, event)
    assert not warning_lock.is_active(bot, event.chat_id, 77)
    assert batches == []


def _setup_scenario(tmp_path, monkeypatch, chat_id, *, threshold=3,
                    native_id=440044, action="ban"):
    # این فایل رفتار «قبلی» (فقط اخطار + دورهٔ حذف پنج‌دقیقه‌ای) را پوشش
    # می‌دهد؛ معافیت سراسری جدید عمداً خاموش است تا همان رفتار سنجیده شود.
    # رفتار پیش‌فرضِ جدید در tests/test_native_admin_exemption.py است.
    monkeypatch.setenv("BOT_NATIVE_ADMIN_EXEMPT", "0")
    _setup_group(tmp_path, monkeypatch, chat_id, owner_id=1001)
    bio_link_storage.set_enabled(chat_id, False)
    monkeypatch.setattr(warning_threshold, "_BASE", tmp_path)
    warning_threshold.set_threshold(chat_id, threshold)
    message_tracker.reset_all()
    bot, deleted, tracker, actions = _make_bot(tmp_path, monkeypatch)
    bot.config_manager = SimpleNamespace(
        get=lambda key, default=None: action if key == "action_on_threshold" else default
    )
    bot.permissions_checked = []
    bot.sent_notices = []

    async def get_permissions(chat, user):
        bot.permissions_checked.append((chat, getattr(user, "id", user)))
        return SimpleNamespace(is_admin=getattr(user, "id", user) == native_id)

    async def send_message(chat, text, **kwargs):
        bot.sent_notices.append((chat, text, kwargs))
        return SimpleNamespace(id=9000 + len(bot.sent_notices))

    async def get_messages(chat, ids=None):
        class Reply:
            async def get_sender(self):
                return _user()
        return Reply()

    bot.client.get_permissions = get_permissions
    bot.client.send_message = send_message
    bot.client.get_messages = get_messages
    bot.detector.is_spam = lambda text, _chat: (
        (True, "کلمه ممنوعه (آزمایشی)") if text.startswith("تخلف ")
        else (False, "")
    )
    return bot, deleted, tracker, actions


@pytest.mark.parametrize("action", ["ban", "mute"])
def test_automatic_warnings_start_once_then_reply_releases(tmp_path, monkeypatch, action):
    chat = -1_000_000_811_101 if action == "ban" else -1_000_000_811_109
    user = _user()
    bot, deleted, tracker, actions = _setup_scenario(
        tmp_path, monkeypatch, chat, action=action
    )

    async def scenario():
        for number in (1, 2):
            event = FakeEvent(chat, user, f"تخلف {number} متفاوت", 200 + number)
            await handler.handle_new_message(bot, event)
            await asyncio.sleep(0)
            assert tracker.get_count(chat, user.id) == number
        assert len(actions.warnings) == 2
        assert len(bot.permissions_checked) == 1  # No RPC per clean message.
        assert actions.punishes == actions.bans == actions.mutes == []
        assert bot.sent_notices == []

        trigger = FakeEvent(chat, user, "تخلف سوم متفاوت", 203)
        await handler.handle_new_message(bot, trigger)
        await asyncio.sleep(0)
        assert tracker.get_count(chat, user.id) == 0
        assert warning_lock.is_active(bot, chat, user.id)
        assert len(bot.sent_notices) == 1  # No generic third-warning message.
        assert len(actions.warnings) == 2
        assert actions.punishes == actions.bans == actions.mutes == []
        assert not bot.is_spam_locked((chat, user.id))
        assert not any(203 in ids for _chat, ids in deleted)  # Reply target remains.
        notice = bot.sent_notices[0]
        assert _u16_slice(notice[1], notice[2]["formatting_entities"][1]).startswith(
            "به دلیل کامل شدن تخلفات شما"
        )

        # No text filter or game/command path may consume later posts.
        after = FakeEvent(chat, user, "متن معمولی", 204)
        await handler.handle_new_message(bot, after)
        assert any(204 in ids for _chat, ids in deleted)
        after_media = FakeEvent(chat, user, "", 205)
        after_media.message.file = object()
        await handler.handle_new_message(bot, after_media)
        assert any(205 in ids for _chat, ids in deleted)
        assert len(bot.sent_notices) == 1
        assert tracker.get_count(chat, user.id) == 0

        stranger = SimpleNamespace(id=223344, username="other", first_name="غریبه")
        unauthorized = FakeEvent(chat, stranger, "حذف اخطار", 206)
        unauthorized.reply_to = SimpleNamespace(reply_to_msg_id=203)
        await handler.handle_new_message(bot, unauthorized)
        assert warning_lock.is_active(bot, chat, user.id)

        owner = SimpleNamespace(id=1001, username="owner", first_name="مالک")
        no_reply = FakeEvent(chat, owner, "حذف اخطار", 207)
        await handler.handle_new_message(bot, no_reply)
        assert warning_lock.is_active(bot, chat, user.id)
        release = FakeEvent(chat, owner, "حذف اخطار", 208)
        release.reply_to = SimpleNamespace(reply_to_msg_id=203)
        await handler.handle_new_message(bot, release)
        assert any("حذف پیام‌های این ادمین متوقف شد" in text for text, _ in release.replies)
        assert not warning_lock.is_active(bot, chat, user.id)
        assert tracker.get_count(chat, user.id) == 0

        previous = list(deleted)
        normal = FakeEvent(chat, user, "پیام سالم بعد از حذف اخطار", 209)
        await handler.handle_new_message(bot, normal)
        assert deleted == previous
        assert len(bot.sent_notices) == 1
        assert actions.punishes == actions.bans == actions.mutes == []

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["ban", "mute"])
def test_manual_warning_of_native_admin_does_not_mute_or_ban(tmp_path, monkeypatch, action):
    chat = -1_000_000_811_102 if action == "ban" else -1_000_000_811_110
    bot, deleted, tracker, actions = _setup_scenario(
        tmp_path, monkeypatch, chat, threshold=2, action=action
    )
    owner = SimpleNamespace(id=1001, username="owner", first_name="مالک")

    async def scenario():
        first = FakeEvent(chat, owner, "اخطار", 300)
        first.reply_to = SimpleNamespace(reply_to_msg_id=290)
        await handler.handle_new_message(bot, first)
        assert tracker.get_count(chat, 440044) == 1
        assert any("یک اخطار دستی دریافت کرد" in text for text, _ in first.replies)
        assert handler._cached_native_group_admin(bot, chat, 440044)
        assert len(bot.permissions_checked) == 1
        command = FakeEvent(chat, owner, "اخطار", 301)
        command.reply_to = SimpleNamespace(reply_to_msg_id=290)
        await handler.handle_new_message(bot, command)
        await asyncio.sleep(0)
        assert len(bot.sent_notices) == 1
        assert warning_lock.is_active(bot, chat, 440044)
        assert tracker.get_count(chat, 440044) == 0
        assert actions.punishes == actions.bans == actions.mutes == []
        assert command.replies == []  # No duplicate manual warning at threshold.

        native = FakeEvent(chat, _user(), "پیام بعدی", 302)
        await handler.handle_new_message(bot, native)
        assert any(302 in ids for _chat, ids in deleted)

    asyncio.run(scenario())


def test_registered_bot_admin_can_reply_to_end_period(tmp_path, monkeypatch):
    chat = -1_000_000_811_108
    bot, _deleted, tracker, _actions = _setup_scenario(tmp_path, monkeypatch, chat)
    admin_storage.add_admin(chat, 77881, username="trusted")
    target = _user()
    warning_lock.begin(bot, chat, target.id, trigger_message_id=500)
    admin = SimpleNamespace(id=77881, username="trusted", first_name="معتمد")

    async def scenario():
        reply = FakeEvent(chat, admin, "حذف اخطار", 501)
        reply.reply_to = SimpleNamespace(reply_to_msg_id=499)
        await handler.handle_new_message(bot, reply)
        assert not warning_lock.is_active(bot, chat, target.id)
        assert tracker.get_count(chat, target.id) == 0
        assert any("حذف پیام‌های این ادمین متوقف شد" in text for text, _ in reply.replies)

    asyncio.run(scenario())


def test_in_flight_warnings_after_role_rpc_do_not_restart_or_renotify(tmp_path, monkeypatch):
    chat = -1_000_000_811_111
    user = _user()
    bot, deleted, tracker, actions = _setup_scenario(
        tmp_path, monkeypatch, chat, threshold=1
    )
    original_get = bot.client.get_permissions
    two_checks_started = asyncio.Event()
    release_checks = asyncio.Event()
    calls = 0

    async def slow_get_permissions(chat_arg, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            two_checks_started.set()
        await release_checks.wait()
        return await original_get(chat_arg, target)

    bot.client.get_permissions = slow_get_permissions

    async def scenario():
        first = asyncio.create_task(handler.handle_new_message(
            bot, FakeEvent(chat, user, "تخلف اول", 701)
        ))
        second = asyncio.create_task(handler.handle_new_message(
            bot, FakeEvent(chat, user, "تخلف دوم", 702)
        ))
        await asyncio.wait_for(two_checks_started.wait(), timeout=2)
        release_checks.set()
        await asyncio.gather(first, second)
        assert len(bot.sent_notices) == 1
        assert warning_lock.is_active(bot, chat, user.id)
        assert tracker.get_count(chat, user.id) == 0
        assert actions.warnings == []
        assert actions.punishes == actions.bans == actions.mutes == []
        assert any(702 in ids for _chat, ids in deleted)

    asyncio.run(scenario())


def test_group_overflow_uses_native_period_without_text():
    chat, user_id = -1_000_000_811_103, 4600
    batches = []
    bot = SimpleNamespace(
        bot_account_id=9999,
        message_delete_queue=SimpleNamespace(
            enqueue=lambda chat_id, ids, **_kw: batches.append((chat_id, ids))
        ),
        logger=SimpleNamespace(log_info=lambda *_a: None, log_error=lambda *_a: None),
    )
    warning_lock.begin(bot, chat, user_id, trigger_message_id=500)
    event = FakeEvent(chat, _user(user_id), "", 501)
    SoroushAntiSpamBot._overflow_message(bot, event)
    assert batches == [(chat, [501])]

    source = inspect.getsource(SoroushAntiSpamBot.run)
    assert source.index("if native_admin_warning_lock.drop_if_active(self, event):") < (
        source.index("priority, kind = classify_priority(raw_text, event)")
    )


def test_time_expiry_and_early_release_do_not_modify_generic_lock():
    class Tracker:
        counts = {1: 3}
        def reset_count(self, *_a):
            self.counts.pop(1, None)

    chat, user_id = -1_000_000_811_104, 444
    bot = SimpleNamespace(tracker=Tracker(), spam_lock={(chat, user_id): 55})
    beginning = time.monotonic()
    assert warning_lock.begin(bot, chat, user_id, trigger_message_id=100, now=beginning)
    assert warning_lock.cleanup_expired(bot, now=beginning + 299.9) == 0
    assert warning_lock.cleanup_expired(bot, now=beginning + 300) == 1
    assert not warning_lock.is_active(bot, chat, user_id)
    assert bot.spam_lock == {(chat, user_id): 55}
    assert warning_lock.begin(bot, chat, user_id, trigger_message_id=102)
    assert warning_lock.release(bot, chat, user_id)
    assert bot.spam_lock == {(chat, user_id): 55}


def test_edited_post_also_cleans_its_registered_bot_reply(monkeypatch):
    from modules import bot_reply_registry
    monkeypatch.setattr(admin_tools, "has_admin_permission", lambda *_a, **_kw: False)
    monkeypatch.setattr(vip_storage, "is_vip", lambda *_a, **_kw: False)
    bot_reply_registry.clear()
    chat, uid = -1_000_000_811_105, 250
    batches = []
    bot = SimpleNamespace(
        bot_account_id=9999,
        message_delete_queue=SimpleNamespace(
            enqueue=lambda _chat, ids, **_kw: batches.append(list(ids))
        ),
    )
    warning_lock.begin(bot, chat, uid, trigger_message_id=100)
    bot_reply_registry.register(chat, 101, 501)
    event = SimpleNamespace(
        chat_id=chat, sender_id=uid, is_private=False, is_edit=True,
        message=SimpleNamespace(id=101, message="ویرایش"),
    )
    try:
        assert warning_lock.drop_if_active(bot, event)
        assert batches == [[101, 501]]
        assert bot_reply_registry.peek_reply_ids(chat, 101) == []
    finally:
        bot_reply_registry.clear()


def test_notice_enqueued_immediately_once_even_with_an_outgoing_worker(tmp_path, monkeypatch):
    bot, _deleted, _tracker, _actions = _make_bot(tmp_path, monkeypatch)
    chat, user = -1_000_000_811_106, _user()
    queued = []
    sent = []

    def enqueue_notice(chat_id, factory, *, priority, on_done):
        queued.append((chat_id, factory, priority, on_done))
        return True

    async def send_message(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return SimpleNamespace(id=9001)

    bot.outgoing_sender = SimpleNamespace(enqueue_notice=enqueue_notice)
    bot.client.send_message = send_message

    async def scenario():
        assert await handler._begin_native_admin_warning_period(
            bot, chat, user.id, user, trigger_message_id=100
        )
        first_deadline = bot._native_admin_warning_locks[("811106", str(user.id))][0]
        assert len(queued) == 1 and queued[0][2] == 0
        assert sent == []  # Scheduled synchronously, not waiting on a warning gate.
        assert not await handler._begin_native_admin_warning_period(
            bot, chat, user.id, user, trigger_message_id=101
        )
        assert len(queued) == 1
        assert bot._native_admin_warning_locks[("811106", str(user.id))][0] == first_deadline
        await queued[0][1]()
        assert len(sent) == 1
        assert warning_lock.release(bot, chat, user.id)
        assert await handler._begin_native_admin_warning_period(
            bot, chat, user.id, user, trigger_message_id=102
        )
        assert len(queued) == 2  # A genuinely new lifecycle gets one new notice.

    asyncio.run(scenario())


def test_manual_warning_of_regular_user_keeps_original_punishment(tmp_path, monkeypatch):
    chat = -1_000_000_811_107
    bot, _deleted, _tracker, actions = _setup_scenario(
        tmp_path, monkeypatch, chat, threshold=1, native_id=11111
    )
    owner = SimpleNamespace(id=1001, username="owner", first_name="مالک")

    async def scenario():
        command = FakeEvent(chat, owner, "اخطار", 401)
        command.reply_to = SimpleNamespace(reply_to_msg_id=390)
        await handler.handle_new_message(bot, command)
        await asyncio.sleep(0.02)
        assert any("یک اخطار دستی دریافت کرد" in text for text, _ in command.replies)
        assert actions.punishes == [(chat, 440044)]
        assert not any("ادمین گرامی" in text for _chat, text, _kw in bot.sent_notices)
        assert not warning_lock.is_active(bot, chat, 440044)

    asyncio.run(scenario())
