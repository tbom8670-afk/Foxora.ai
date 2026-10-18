"""تست کامل قابلیت «فیلتر اسم»، «لغو اسم» و «لیست فیلتر اسم» در گروه.

تضمین می‌کند:
  ۱. دستورات «فیلتر اسم»، «لغو اسم» و «لیست فیلتر اسم» برای ادمین/مالک کار می‌کنند
     و برای کاربر عادی مسدودند.
  ۲. نمایش «لیست فیلتر اسم» عنوان Bold و آیتم‌های Spoiler تولید می‌کند.
  ۳. هنگام اجرای «فیلتر اسم علی»، اعضای حاضر در گروه که نامشان «علی» است
     طبق مجازات گروه (سکوت یا اخراج) مجازات می‌شوند (به‌جز مالک/ادمین/VIP).
  ۴. وقتی کاربری با نام فیلترشده در گروه پیام می‌فرستد، پیامش حذف شده و
     طبق مجازات فعلی گروه (سکوت یا اخراج) با اعلان استاندارد Bold + Quote
     سکوت دائم یا اخراج می‌شود.
  ۵. پس از «لغو اسم علی»، کاربران با نام «علی» دیگر مجازات نمی‌شوند.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from splusthon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntitySpoiler,
)

from handlers.message_handler import handle_new_message
from modules import (
    admin_storage,
    banned_storage,
    group_names_storage,
    group_storage,
    manual_mute_storage,
    punishment_mode,
    user_history,
    vip_storage,
)
from modules.group_words_commands import handle_group_word_command


class FakeReplyEvent:
    def __init__(self, chat_id, sender, text, msg_id=101):
        self.chat_id = chat_id
        self.sender_id = getattr(sender, "id", None)
        self._sender = sender
        self.is_private = False
        self.out = False
        self.chat = SimpleNamespace(id=chat_id, title="گروه تست فیلتر اسم")
        self.message = SimpleNamespace(
            id=msg_id,
            message=text,
            reply_to_msg_id=None,
            entities=None,
            file=None,
            media=None,
            fwd_from=None,
            edit_date=None,
        )
        self.replies = []

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return self.chat

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(id=900 + len(self.replies))


class ImmediateModerationQueue:
    def enqueue(self, chat_id, action, user_id=None, timeout_seconds=45, operation=None, on_success=None, on_failure=None):
        async def _run():
            try:
                res = await operation()
                if on_success:
                    await on_success(res)
            except Exception as err:
                if on_failure:
                    await on_failure(err)

        asyncio.get_running_loop().create_task(_run())
        return True


def _isolate_storages(tmp_path, monkeypatch):
    monkeypatch.setattr(group_names_storage, "FILE", tmp_path / "group_filtered_names.json")
    group_names_storage._cache = None
    group_names_storage._cache_mtime = None

    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    group_storage._cache = None
    group_storage._cache_mtime = None

    monkeypatch.setattr(admin_storage, "FILE", tmp_path / "admins.json")
    admin_storage._cache = None
    admin_storage._cache_mtime = None

    monkeypatch.setattr(vip_storage, "FILE", tmp_path / "vip_users.json")
    vip_storage._cache = None
    vip_storage._cache_mtime = None

    monkeypatch.setattr(punishment_mode, "_BASE", tmp_path)
    monkeypatch.setattr(punishment_mode, "_FILE", tmp_path / "punishment_mode.json")
    punishment_mode._cache = None
    punishment_mode._cache_mtime = None

    monkeypatch.setattr(manual_mute_storage, "FILE", tmp_path / "manual_mutes.json")
    manual_mute_storage._cache = None
    manual_mute_storage._cache_mtime = None

    monkeypatch.setattr(banned_storage, "FILE", tmp_path / "banned_users.json")
    banned_storage._cache = None
    banned_storage._cache_mtime = None

    if hasattr(user_history, "FILE"):
        monkeypatch.setattr(user_history, "FILE", tmp_path / "user_history.json")
    elif hasattr(user_history, "_FILE"):
        monkeypatch.setattr(user_history, "_FILE", tmp_path / "user_history.json")


def test_group_names_storage_add_remove_and_matching(tmp_path, monkeypatch):
    _isolate_storages(tmp_path, monkeypatch)
    chat_id = -10077001

    assert group_names_storage.get_names(chat_id) == []
    assert group_names_storage.add_name(chat_id, "علی") is True
    # ثبت تکراری رد می‌شود
    assert group_names_storage.add_name(chat_id, "علی") is False
    assert group_names_storage.add_name(chat_id, "عسل") is True
    assert group_names_storage.get_names(chat_id) == ["علی", "عسل"]

    # تطبیق نام
    assert (
        group_names_storage.find_matching_filtered_name(
            SimpleNamespace(first_name="علی", last_name="رضایی"), group_id=chat_id
        )
        == "علی"
    )
    assert (
        group_names_storage.find_matching_filtered_name(
            SimpleNamespace(first_name="علیرضا", last_name="محمدی"), group_id=chat_id
        )
        == "علی"
    )
    assert (
        group_names_storage.find_matching_filtered_name(
            SimpleNamespace(first_name="عسسسل", last_name=""), group_id=chat_id
        )
        == "عسل"
    )
    assert (
        group_names_storage.find_matching_filtered_name(
            SimpleNamespace(first_name="مریم", last_name="احمدی"), group_id=chat_id
        )
        is None
    )

    # حذف نام
    assert group_names_storage.remove_name(chat_id, "علی") is True
    assert group_names_storage.remove_name(chat_id, "علی") is False
    assert group_names_storage.get_names(chat_id) == ["عسل"]


def test_name_filter_commands_and_spoiler_list_and_immediate_scan(tmp_path, monkeypatch):
    _isolate_storages(tmp_path, monkeypatch)
    chat_id = -10077002
    owner_id = 1001
    group_storage.activate_group(chat_id, "گروه تست")
    group_storage.set_group_owner(chat_id, owner_id)

    admin_user = SimpleNamespace(id=owner_id, first_name="مدیر", last_name="", username="owner_u")
    normal_user = SimpleNamespace(id=2002, first_name="کاربر", last_name="", username="normal_u")
    ali_member = SimpleNamespace(id=3003, first_name="علی", last_name="اکبری", username="ali_u")
    vip_ali = SimpleNamespace(id=3004, first_name="علی", last_name="وی‌آی‌پی", username="vip_ali")
    vip_storage.add_vip(chat_id, vip_ali.id)

    muted_ids = []
    banned_ids = []

    class FakeAdminActions:
        bot_account_id = 9999

        async def mute_user(self, cid, uid, duration=None, **kwargs):
            muted_ids.append(uid)
            return True

        async def ban_user(self, cid, uid, reason="", **kwargs):
            banned_ids.append(uid)
            return True

    fake_bot = SimpleNamespace(
        bot_account_id=9999,
        admin_actions=FakeAdminActions(),
        client=SimpleNamespace(
            get_participants=lambda _chat: asyncio.sleep(0, result=[admin_user, ali_member, vip_ali])
        ),
    )

    async def _scenario():
        # ۱. کاربر غیرادمین اجازه ندارد
        ev_denied = FakeReplyEvent(chat_id, normal_user, "فیلتر اسم علی")
        handled = await handle_group_word_command(
            fake_bot, ev_denied, "فیلتر اسم علی", chat_id, normal_user.id, normal_user.username
        )
        assert handled is True
        assert "فقط مالک یا ادمین" in ev_denied.replies[-1][0]
        assert group_names_storage.get_names(chat_id) == []

        # ۲. حالت مجازات گروه روی «سکوت»: اجرای «فیلتر اسم علی» باید علیِ عادی را سکوت کند ولی VIP را نه
        punishment_mode.set_mode(chat_id, punishment_mode.MODE_MUTE)
        ev_add = FakeReplyEvent(chat_id, admin_user, "فیلتر اسم علی")
        handled = await handle_group_word_command(
            fake_bot, ev_add, "فیلتر اسم علی", chat_id, admin_user.id, admin_user.username
        )
        assert handled is True
        assert "فیلتر اسم ثبت شد" in ev_add.replies[-1][0]
        assert "سکوت" in ev_add.replies[-1][0]
        assert muted_ids == [ali_member.id]
        assert banned_ids == []

        # ۳. افزودن «عسل» در حالت اخراج
        punishment_mode.set_mode(chat_id, punishment_mode.MODE_BAN)
        ev_add2 = FakeReplyEvent(chat_id, admin_user, "فیلتر اسم عسل")
        await handle_group_word_command(
            fake_bot, ev_add2, "فیلتر اسم عسل", chat_id, admin_user.id, admin_user.username
        )
        assert group_names_storage.get_names(chat_id) == ["علی", "عسل"]

        # ۴. نمایش لیست فیلتر اسم با اسپویلر
        ev_list = FakeReplyEvent(chat_id, admin_user, "لیست فیلتر اسم")
        await handle_group_word_command(
            fake_bot, ev_list, "لیست فیلتر اسم", chat_id, admin_user.id, admin_user.username
        )
        list_text, list_kwargs = ev_list.replies[-1]
        entities = list_kwargs.get("formatting_entities", [])
        assert "📋 لیست فیلتر اسم:" in list_text
        assert "علی" in list_text and "عسل" in list_text
        assert any(isinstance(e, MessageEntityBold) for e in entities)
        spoiler_entities = [e for e in entities if isinstance(e, MessageEntitySpoiler)]
        assert len(spoiler_entities) == 2

        # ۵. لغو اسم
        ev_rem = FakeReplyEvent(chat_id, admin_user, "لغو اسم علی")
        await handle_group_word_command(
            fake_bot, ev_rem, "لغو اسم علی", chat_id, admin_user.id, admin_user.username
        )
        assert "حذف شد" in ev_rem.replies[-1][0]
        assert group_names_storage.get_names(chat_id) == ["عسل"]

    asyncio.run(_scenario())


def test_incoming_message_from_filtered_name_user_punishes_by_group_mode(tmp_path, monkeypatch):
    _isolate_storages(tmp_path, monkeypatch)
    chat_id = -10077003
    owner_id = 1001
    group_storage.activate_group(chat_id, "گروه تست")
    group_storage.set_group_owner(chat_id, owner_id)
    group_names_storage.add_name(chat_id, "علی")

    deleted_batches = []
    banned_calls = []
    muted_calls = []

    class FakeAdminActions:
        bot_account_id = 9999

        async def ban_user(self, cid, uid, reason="", user=None, chat=None):
            banned_calls.append((cid, uid, reason))
            return True

        async def mute_user(self, cid, uid, duration=None, user=None, chat=None):
            muted_calls.append((cid, uid))
            return True

    spam_lock = {}
    fake_bot = SimpleNamespace(
        bot_account_id=9999,
        punished_users=set(),
        spam_lock=spam_lock,
        is_spam_locked=lambda key: bool(spam_lock.get(key)),
        set_spam_lock=lambda key, value=True: spam_lock.__setitem__(key, value),
        clear_spam_lock=lambda key: spam_lock.pop(key, None),
        delete_notice_lock={},
        bot_sent_messages=[],
        reply_input_peer_cache={},
        moderation_queue=ImmediateModerationQueue(),
        admin_actions=FakeAdminActions(),
        message_delete_queue=SimpleNamespace(
            enqueue=lambda cid, ids, **_kw: deleted_batches.append((cid, list(ids)))
        ),
        outgoing_sender=None,
        config_manager=SimpleNamespace(get=lambda _k, default=None: default),
        detector=SimpleNamespace(
            is_spam=lambda *_a, **_kw: (False, None),
            has_public_username=lambda *_a, **_kw: False,
            check_banned_words=lambda *_a, **_kw: (False, None),
        ),
        flood_detector=SimpleNamespace(check_flood=lambda *_a, **_kw: (False, [])),
        activity_tracker=SimpleNamespace(track_message=lambda *_a, **_kw: None),
        message_tracker=SimpleNamespace(
            track_message=lambda *_a, **_kw: None,
            get_user_messages=lambda *_a, **_kw: [],
            clear_user_messages=lambda *_a, **_kw: None,
        ),
        tracker=SimpleNamespace(
            get_count=lambda *_a: 0,
            add_violation=lambda *_a: 1,
            is_banned=lambda *_a: False,
            is_muted=lambda *_a: False,
        ),
        logger=SimpleNamespace(
            log_info=lambda *_a, **_kw: None,
            log_error=lambda *_a, **_kw: None,
            log_deleted_message=lambda **_kw: None,
        ),
        check_group_word_commands=lambda ev, txt, cid, uid: handle_group_word_command(
            fake_bot, ev, txt, cid, uid
        ),
    )

    async def _scenario():
        # الف) حالت اخراج (پیش‌فرض): کاربر با نام «علی» پیام می‌دهد -> حذف پیام + اخراج
        punishment_mode.set_mode(chat_id, punishment_mode.MODE_BAN)
        ali_user = SimpleNamespace(id=5001, first_name="علی", last_name="رضایی", username="ali5001")
        ev1 = FakeReplyEvent(chat_id, ali_user, "سلام به همه", msg_id=701)
        await handle_new_message(fake_bot, ev1)
        await asyncio.sleep(0.05)

        assert any(701 in ids for _cid, ids in deleted_batches)
        assert len(banned_calls) == 1
        assert banned_calls[0][1] == 5001
        assert len(ev1.replies) == 1
        notice_text, notice_kwargs = ev1.replies[0]
        assert "نام فیلترشده («علی») اخراج شد" in notice_text
        ents = notice_kwargs.get("formatting_entities", [])
        assert any(isinstance(e, MessageEntityBold) for e in ents)
        assert any(isinstance(e, MessageEntityBlockquote) for e in ents)

        # ب) حالت سکوت گروه: کاربر جدید با نام «علی» پیام می‌دهد -> حذف پیام + سکوت دائم
        punishment_mode.set_mode(chat_id, punishment_mode.MODE_MUTE)
        ali_user_2 = SimpleNamespace(id=5002, first_name="علی", last_name="حسینی", username="ali5002")
        ev2 = FakeReplyEvent(chat_id, ali_user_2, "پیام دوم", msg_id=702)
        await handle_new_message(fake_bot, ev2)
        await asyncio.sleep(0.05)

        assert any(702 in ids for _cid, ids in deleted_batches)
        assert len(muted_calls) == 1
        assert muted_calls[0][1] == 5002
        assert len(ev2.replies) == 1
        notice2_text, _ = ev2.replies[0]
        assert "نام فیلترشده («علی») سکوت دائم شد" in notice2_text

        # ج) کاربر VIP با نام «علی» معاف است و مجازات نمی‌شود
        vip_ali = SimpleNamespace(id=5003, first_name="علی", last_name="ممتاز", username="vip5003")
        vip_storage.add_vip(chat_id, vip_ali.id)
        ev3 = FakeReplyEvent(chat_id, vip_ali, "سلام من VIP هستم", msg_id=703)
        await handle_new_message(fake_bot, ev3)
        await asyncio.sleep(0.05)

        assert not any(703 in ids for _cid, ids in deleted_batches)
        assert len(muted_calls) == 1
        assert len(banned_calls) == 1

    asyncio.run(_scenario())
