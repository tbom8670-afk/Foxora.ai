# -*- coding: utf-8 -*-
"""تست‌های جامع قابلیت «محدودیت پیام» — پوشش هر ۱۲ سناریوی الزامی + قالب‌های UI.

این مجموعه شامل تست‌های واحد، یکپارچه با ``handle_new_message``، همزمانی بالا،
پایداری SQLite پس از ری‌استارت، جداسازی صف مستقل و بررسی دقیق آفست‌های UTF-16
برای ``MessageEntityBold`` و ``MessageEntityBlockquote`` است.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from splusthon.tl import functions, types
from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold

from handlers import message_handler
from modules import (
    admin_storage,
    admin_tools,
    banned_storage,
    group_dispatch,
    group_storage,
    manual_mute_storage,
    message_dedup,
    owner_check,
    runtime_db,
)
from modules.admin_actions import AdminActions
from modules.manage_features import (
    message_limit,
    message_limit_commands,
    peers,
    texts,
)
from modules.moderation_queue import ModerationQueue


CHAT_ID = -1000077001122
BOT_ID = 900001
GLOBAL_OWNER_ID = 100001
GROUP_OWNER_ID = 100002
NATIVE_CREATOR_ID = 100003
REG_ADMIN_ID = 200001
RESTRICTED_REG_ADMIN_ID = 200002
EDITABLE_NATIVE_ADMIN_ID = 300001
LOCKED_NATIVE_ADMIN_ID = 300002
NORMAL_USER_ID = 400001
OTHER_USER_ID = 400002
AUTO_MUTED_USER_ID = 400003


def _slice_u16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


def _make_user(
    uid: int,
    username: str | None = None,
    first_name: str = "کاربر",
    *,
    access_hash: int = 777,
    deleted: bool = False,
    participant=None,
):
    return SimpleNamespace(
        id=uid,
        access_hash=access_hash,
        username=username,
        first_name=first_name,
        last_name="",
        bot=False,
        deleted=deleted,
        participant=participant,
    )


class FakeSoroushClient:
    """شبیه‌ساز واقعی کلاینت سروش‌پلاس برای EditBannedRequest و نقش‌های گروه."""

    def __init__(self):
        self.edit_banned_calls: list[dict] = []
        self.delete_calls: list[tuple] = []
        self.sent_messages: list[dict] = []
        self.admin_rows: list = []
        self.participants_by_id: dict[int, object] = {}
        self.supports_get_participant = False
        self.fail_mute_for: set[int] = set()
        self.fail_unmute_for: set[int] = set()
        self.messages_by_id: dict[int, object] = {}
        self.entities_by_username: dict[str, object] = {}
        self.entities_by_id: dict[int, object] = {}
        self.mute_delay: float = 0.0

    async def get_input_entity(self, target):
        if hasattr(target, "channel_id") or hasattr(target, "user_id"):
            return target
        if isinstance(target, types.Channel):
            return types.InputPeerChannel(int(target.id), int(target.access_hash or 0))
        if hasattr(target, "id") and hasattr(target, "access_hash"):
            uid = int(target.id)
            if uid < 0:
                return types.InputPeerChannel(abs(uid) - 1000000000000, int(target.access_hash))
            return types.InputPeerUser(uid, int(target.access_hash))
        if isinstance(target, int):
            if target < 0:
                raw = abs(target)
                cid = raw - 1000000000000 if str(raw).startswith("100") else raw
                return types.InputPeerChannel(cid, 55555)
            return types.InputPeerUser(target, 66666)
        raise ValueError(f"cannot resolve {target!r}")

    async def get_entity(self, token):
        if isinstance(token, int) and token in self.entities_by_id:
            return self.entities_by_id[token]
        clean = str(token).lstrip("@")
        if clean.isdigit() and int(clean) in self.entities_by_id:
            return self.entities_by_id[int(clean)]
        if clean in self.entities_by_username:
            return self.entities_by_username[clean]
        raise ValueError(f"entity @{clean} not found")

    async def get_messages(self, _chat, ids=None):
        msg_id = int(ids[0]) if isinstance(ids, list) else int(ids)
        return self.messages_by_id.get(msg_id)

    async def get_participants(self, _peer, filter=None, limit=200):  # noqa: A002
        if filter is not None and "Admins" in type(filter).__name__:
            return list(self.admin_rows[:limit])
        return list(self.admin_rows[:limit])

    async def delete_messages(self, peer, ids, revoke=True):
        self.delete_calls.append((peer, list(ids), revoke))

    async def send_message(self, peer, text, reply_to=None, formatting_entities=None):
        entry = {
            "peer": peer,
            "text": text,
            "reply_to": reply_to,
            "entities": list(formatting_entities or []),
        }
        self.sent_messages.append(entry)
        return entry

    async def __call__(self, request):
        if isinstance(request, functions.channels.EditBannedRequest):
            if self.mute_delay > 0:
                await asyncio.sleep(self.mute_delay)
            uid = getattr(request.participant, "user_id", None) or getattr(
                request.participant, "id", None
            )
            uid = int(uid)
            is_muting = bool(getattr(request.banned_rights, "send_messages", False)) or bool(
                getattr(request.banned_rights, "view_messages", False)
            )
            if is_muting and uid in self.fail_mute_for:
                raise RuntimeError("USER_ADMIN_INVALID: cannot restrict this admin")
            if (not is_muting) and uid in self.fail_unmute_for:
                raise RuntimeError("CHAT_ADMIN_REQUIRED: unmute failed")
            self.edit_banned_calls.append(
                {
                    "uid": uid,
                    "rights": request.banned_rights,
                    "send_messages": bool(request.banned_rights.send_messages),
                    "view_messages": bool(request.banned_rights.view_messages),
                    "embed_links": bool(request.banned_rights.embed_links),
                }
            )
            return SimpleNamespace(ok=True)

        if isinstance(request, functions.channels.GetParticipantRequest):
            if not self.supports_get_participant:
                raise RuntimeError("RPCError 400: NOT_SUPPORTED")
            uid = getattr(request.participant, "user_id", None) or getattr(
                request.participant, "id", None
            )
            p_obj = self.participants_by_id.get(int(uid))
            if p_obj is None:
                raise RuntimeError("USER_NOT_PARTICIPANT")
            return SimpleNamespace(participant=p_obj)

        raise AssertionError(f"RPC غیرمنتظره: {request!r}")


class FakeEvent:
    """رویداد پیام سروش‌پلاس با پشتیبانی کامل از ریپلای، ویرایش و انواع رسانه."""

    _next_id = 1000

    def __init__(
        self,
        *,
        client: FakeSoroushClient,
        sender,
        text: str = "",
        chat_id: int = CHAT_ID,
        reply_to_msg_id: int | None = None,
        reply_sender=None,
        reply_message_obj=None,
        media=None,
        is_edit: bool = False,
        is_private: bool = False,
        action=None,
        msg_id: int | None = None,
    ):
        FakeEvent._next_id += 1
        self.id = msg_id if msg_id is not None else FakeEvent._next_id
        self.client = client
        self.chat_id = chat_id
        self.sender = sender
        self.sender_id = getattr(sender, "id", None)
        self.is_private = is_private
        self.is_group = not is_private
        self.is_channel = not is_private
        self.out = False
        self.replies: list[dict] = []
        self.chat = types.Channel(
            id=abs(chat_id) - 1000000000000,
            title="گروه تست فوکسورا",
            photo=None,
            date=None,
            creator=False,
            broadcast=False,
            megagroup=True,
            access_hash=55555,
            admin_rights=types.ChatAdminRights(ban_users=True, delete_messages=True),
        )
        self.reply_to = (
            SimpleNamespace(reply_to_msg_id=reply_to_msg_id)
            if reply_to_msg_id is not None
            else None
        )
        self.reply_to_msg_id = reply_to_msg_id
        self.is_reply = reply_to_msg_id is not None or reply_sender is not None or reply_message_obj is not None
        self.reply_to_sender = reply_sender
        self._reply_message_obj = reply_message_obj

        if action is not None:
            # شبیه‌سازی MessageService (فاقد فیلد متنی یا دارای action)
            self.message = SimpleNamespace(id=self.id, action=action)
        else:
            self.message = SimpleNamespace(
                id=self.id,
                message=text,
                media=media,
                photo=(media if getattr(media, "kind", "") == "photo" else None),
                video=(media if getattr(media, "kind", "") == "video" else None),
                voice=(media if getattr(media, "kind", "") == "voice" else None),
                document=(media if getattr(media, "kind", "") == "file" else None),
                sticker=(media if getattr(media, "kind", "") == "sticker" else None),
                gif=(media if getattr(media, "kind", "") == "gif" else None),
                fwd_from=None,
                forward=None,
                edit_date=(1700000000 if is_edit else None),
                reply_to=self.reply_to,
                reply_to_msg_id=reply_to_msg_id,
                action=None,
            )

        # ذخیره در بانک پیام‌های کلاینت برای دستوراتی که get_messages می‌زنند
        stored_msg = SimpleNamespace(
            id=self.id,
            sender=sender,
            sender_id=self.sender_id,
            message=text,
            action=action,
            get_sender=self._make_get_sender(sender),
        )
        client.messages_by_id[self.id] = stored_msg
        if self.sender_id is not None:
            client.entities_by_id[int(self.sender_id)] = sender
        if getattr(sender, "username", None):
            client.entities_by_username[sender.username.lstrip("@")] = sender

    @staticmethod
    def _make_get_sender(user_obj):
        async def _getter():
            return user_obj
        return _getter

    @property
    def raw_text(self) -> str:
        return getattr(self.message, "message", "") or ""

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return self.chat

    async def get_reply_message(self):
        if self._reply_message_obj is not None:
            return self._reply_message_obj
        if self.reply_to_msg_id is not None:
            return self.client.messages_by_id.get(self.reply_to_msg_id)
        return None

    async def reply(self, text, formatting_entities=None, **_kwargs):
        entry = {
            "text": text,
            "entities": list(formatting_entities or []),
        }
        self.replies.append(entry)
        self.client.sent_messages.append(entry)
        return entry


class DummyTracker:
    def __init__(self):
        self.muted_users = {}
        self.banned_users = set()
        self._counts = {}

    def get_count(self, chat_id, user_id):
        return self._counts.get((chat_id, user_id), 0)

    def reset_count(self, chat_id, user_id):
        self._counts.pop((chat_id, user_id), None)

    def add_message(self, *_a, **_kw):
        return 0


class DummyLogger:
    def __init__(self):
        self.infos: list[str] = []
        self.errors: list[str] = []

    def log_info(self, msg):
        self.infos.append(str(msg))

    def log_error(self, msg):
        self.errors.append(str(msg))

    def log_warning(self, msg):
        self.infos.append(str(msg))

    def log_spam(self, *_a, **_kw):
        pass

    def log_action(self, *_a, **_kw):
        pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """محیط کاملاً ایزوله با SQLite واقعی روی دیسک موقت و صف‌های مستقل."""
    # ۱) ایزوله‌سازی کامل SQLite پایدار (runtime_db)
    runtime_db.close()
    db_path = tmp_path / "bot.sqlite3"
    monkeypatch.setattr(runtime_db, "DB_FILE", db_path)
    message_limit.reset_runtime_caches()

    # ۲) ایزوله‌سازی فایل‌های پیکربندی مالک، گروه، ادمین، سکوت دستی و بن
    owner_file = tmp_path / "owner.json"
    owner_runtime_file = tmp_path / "owner_runtime.json"
    owner_file.write_text(
        f'{{"user_id": {GLOBAL_OWNER_ID}, "owner_id": {GLOBAL_OWNER_ID}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(owner_check, "DEPLOYMENT_FILE", owner_file)
    monkeypatch.setattr(owner_check, "RUNTIME_FILE", owner_runtime_file)
    monkeypatch.setattr(owner_check, "FILE", owner_file)
    monkeypatch.setattr(owner_check, "_CACHE_SIGNATURE", None)
    monkeypatch.setattr(owner_check, "_CACHE_OWNER", None)

    groups_file = tmp_path / "groups.json"
    monkeypatch.setattr(group_storage, "FILE", groups_file)
    group_storage.activate_group(CHAT_ID, "گروه تست فوکسورا")
    group_storage.set_group_owner(CHAT_ID, GROUP_OWNER_ID)

    admins_file = tmp_path / "admins.json"
    logs_file = tmp_path / "admin_logs.json"
    monkeypatch.setattr(admin_storage, "FILE", admins_file)
    monkeypatch.setattr(admin_tools, "_ADMIN_LOG_FILE", logs_file)
    admin_storage.add_admin(CHAT_ID, REG_ADMIN_ID, username="manager_ali")
    admin_storage.add_admin(CHAT_ID, RESTRICTED_REG_ADMIN_ID, username="sub_admin")

    mm_file = tmp_path / "manual_mutes.json"
    monkeypatch.setattr(manual_mute_storage, "FILE", mm_file)

    ban_file = tmp_path / "banned_users.json"
    monkeypatch.setattr(banned_storage, "FILE", ban_file)

    # پاک‌سازی کش dedup بین تست‌ها
    if hasattr(message_dedup, "reset"):
        message_dedup.reset()

    client = FakeSoroushClient()
    logger = DummyLogger()

    # ثبت ادمین‌های بومی سروش‌پلاس (سازنده، ربات، ادمین قابل محدودسازی، ادمین غیرقابل محدودسازی)
    creator_user = _make_user(
        NATIVE_CREATOR_ID,
        "native_creator",
        "سازنده اصلی",
        participant=types.ChannelParticipantCreator(
            user_id=NATIVE_CREATOR_ID,
            admin_rights=types.ChatAdminRights(ban_users=True, delete_messages=True),
        ),
    )
    bot_user = _make_user(
        BOT_ID,
        "foxora_bot",
        "روباه",
        participant=types.ChannelParticipantAdmin(
            user_id=BOT_ID,
            promoted_by=NATIVE_CREATOR_ID,
            date=None,
            admin_rights=types.ChatAdminRights(ban_users=True, delete_messages=True),
            can_edit=True,
        ),
    )
    editable_native_admin = _make_user(
        EDITABLE_NATIVE_ADMIN_ID,
        "editable_admin",
        "ادمین قابل محدود",
        participant=types.ChannelParticipantAdmin(
            user_id=EDITABLE_NATIVE_ADMIN_ID,
            promoted_by=BOT_ID,
            date=None,
            admin_rights=types.ChatAdminRights(delete_messages=True),
            can_edit=True,
        ),
    )
    locked_native_admin = _make_user(
        LOCKED_NATIVE_ADMIN_ID,
        "senior_admin",
        "ادمین ارشد",
        participant=types.ChannelParticipantAdmin(
            user_id=LOCKED_NATIVE_ADMIN_ID,
            promoted_by=NATIVE_CREATOR_ID,
            date=None,
            admin_rights=types.ChatAdminRights(ban_users=True, add_admins=True),
            can_edit=False,
        ),
    )
    client.admin_rows = [
        creator_user,
        bot_user,
        editable_native_admin,
        locked_native_admin,
    ]

    mod_queue = ModerationQueue(logger)
    admin_actions = AdminActions(
        client,
        logger,
        {},
        peer_cache={},
        bot_account_id=BOT_ID,
    )

    bot = SimpleNamespace(
        client=client,
        logger=logger,
        config_manager={},
        admin_actions=admin_actions,
        moderation_queue=mod_queue,
        bot_account_id=BOT_ID,
        reply_input_peer_cache={},
        tracker=DummyTracker(),
        punished_users=set(),
        spam_burst_users=set(),
        spam_burst_messages={},
        rejoin_spam_state={},
        user_messages={},
        repeat_messages={},
        flood_messages={},
        spammer_messages={},
        clear_spam_lock=lambda *_a, **_kw: None,
        is_spam_locked=lambda *_a, **_kw: False,
    )

    users = {
        "global_owner": _make_user(GLOBAL_OWNER_ID, "global_boss", "مالک اصلی"),
        "group_owner": _make_user(GROUP_OWNER_ID, "group_owner", "مالک گروه"),
        "native_creator": creator_user,
        "reg_admin": _make_user(REG_ADMIN_ID, "manager_ali", "علی مدیر"),
        "restricted_reg_admin": _make_user(
            RESTRICTED_REG_ADMIN_ID, "sub_admin", "ادمین فرعی"
        ),
        "editable_native_admin": editable_native_admin,
        "locked_native_admin": locked_native_admin,
        "normal": _make_user(NORMAL_USER_ID, "reza_member", "رضا"),
        "other": _make_user(OTHER_USER_ID, "sara_member", "سارا"),
        "auto_muted": _make_user(AUTO_MUTED_USER_ID, "auto_spammer", "کاربر خودکار"),
    }
    for u in users.values():
        message_limit.remember_user_entity(bot, u)

    yield SimpleNamespace(
        bot=bot,
        client=client,
        logger=logger,
        users=users,
        db_path=db_path,
        mod_queue=mod_queue,
    )

    asyncio.run(mod_queue.close())
    runtime_db.close()
    message_limit.reset_runtime_caches()


# ---------------------------------------------------------------------------
#  بررسی دقیق قالب‌های ۴گانه اسکرین‌شات + طبقه‌بندی لاین ادمین
# ---------------------------------------------------------------------------
def test_exact_screenshot_templates_and_utf16_entities():
    """بررسی مو‌به‌مو متن، بولد و نقل‌قول هر چهار قالب مطابق اسکرین‌شات کارفرما."""
    # قالب ۱: ثبت محدودیت پیام
    t1, e1 = texts.message_limit_set(username="@reza_member", limit=4)
    assert t1 == (
        "محدودیت تعداد پیام تنظیم شد\n\n"
        "🔐 کاربر : @reza_member\n"
        "🔐 محدودیت ارسال : 4 پیام"
    )
    bolds_1 = [_slice_u16(t1, e.offset, e.length) for e in e1 if isinstance(e, MessageEntityBold)]
    quotes_1 = [_slice_u16(t1, e.offset, e.length) for e in e1 if isinstance(e, MessageEntityBlockquote)]
    assert bolds_1 == [t1]
    assert quotes_1 == ["محدودیت تعداد پیام تنظیم شد"]

    # قالب ۲: رسیدن به سقف مجاز
    t2, e2 = texts.message_limit_reached(username="@reza_member", limit=4)
    assert t2 == (
        "🔐 کاربر : @reza_member\n"
        "شما به محدویت ارسال\n"
        "« 4 » پیام رسیدید دستررسی ارسال پیام برای شما\n"
        "بسته میشود"
    )
    bolds_2 = [_slice_u16(t2, e.offset, e.length) for e in e2 if isinstance(e, MessageEntityBold)]
    quotes_2 = [_slice_u16(t2, e.offset, e.length) for e in e2 if isinstance(e, MessageEntityBlockquote)]
    assert quotes_2 == ["🔐 کاربر : @reza_member"]
    assert bolds_2 == [
        "شما به محدویت ارسال\n"
        "« 4 » پیام رسیدید دستررسی ارسال پیام برای شما\n"
        "بسته میشود"
    ]

    # قالب ۳: جلوگیری از باز شدن با «آزاد» یا «رفع سکوت»
    t3, e3 = texts.message_limit_locked_notice()
    assert t3 == (
        "این کاربر قفل محدودیت پیام دارد\n"
        "روی پیامش ریپلای کنید بنویسید\n"
        "🔐 »» لغو محدودیت پیام"
    )
    bolds_3 = [_slice_u16(t3, e.offset, e.length) for e in e3 if isinstance(e, MessageEntityBold)]
    quotes_3 = [_slice_u16(t3, e.offset, e.length) for e in e3 if isinstance(e, MessageEntityBlockquote)]
    assert bolds_3 == [t3]
    assert quotes_3 == ["🔐 »» لغو محدودیت پیام"]

    # قالب ۴: لغو محدودیت پیام
    t4, e4 = texts.message_limit_cancelled()
    assert t4 == "« 🔐 » محدودیت لغو شد"
    bolds_4 = [_slice_u16(t4, e.offset, e.length) for e in e4 if isinstance(e, MessageEntityBold)]
    quotes_4 = [_slice_u16(t4, e.offset, e.length) for e in e4 if isinstance(e, MessageEntityBlockquote)]
    assert bolds_4 == ["« 🔐 » محدودیت لغو شد"]
    assert quotes_4 == []

    # طبقه‌بندی در لاین مدیریتی دیسپچر
    assert group_dispatch.classify_priority("محدودیت پیام 4")[1] == group_dispatch.LANE_ADMIN
    assert group_dispatch.classify_priority("لغو محدودیت پیام")[1] == group_dispatch.LANE_ADMIN
    assert group_dispatch.classify_priority("لغو محدودیت")[1] == group_dispatch.LANE_ADMIN


# ---------------------------------------------------------------------------
#  سناریوی ۱ و ۲: ثبت محدودیت برای کاربر عادی + شمارش انواع پیام و میوت در پیام Nام
# ---------------------------------------------------------------------------
def test_1_and_2_set_limit_and_auto_mute_after_exact_allowed_messages(env):
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        manager = env.users["reg_admin"]

        # پیام اولیهٔ کاربر عادی که مدیر رویش ریپلای می‌کند
        seed_ev = FakeEvent(client=client, sender=normal, text="سلام بچه‌ها")
        await message_handler.handle_new_message(bot, seed_ev)

        # ۱) ثبت محدودیت پیام ۴ توسط مدیر مجاز
        cmd_ev = FakeEvent(
            client=client,
            sender=manager,
            text="محدودیت پیام 4",
            reply_to_msg_id=seed_ev.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, cmd_ev)

        rec = message_limit.get_record(CHAT_ID, normal.id)
        assert rec is not None
        assert rec["allowed_count"] == 4
        assert rec["sent_count"] == 0
        assert rec["status"] == message_limit.STATUS_COUNTING
        assert len(cmd_ev.replies) == 1
        exp_t1, _ = texts.message_limit_set(username="@reza_member", limit=4)
        assert cmd_ev.replies[0]["text"] == exp_t1

        # ۲) ارسال ۴ پیام از انواع مختلف (متن، عکس بدون کپشن، ویس، استیکر)
        ev1 = FakeEvent(client=client, sender=normal, text="پیام اول متنی")
        await message_handler.handle_new_message(bot, ev1)
        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 1
        assert client.edit_banned_calls == []

        ev2 = FakeEvent(
            client=client,
            sender=normal,
            text="",
            media=SimpleNamespace(kind="photo"),
        )
        await message_handler.handle_new_message(bot, ev2)
        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 2
        assert client.edit_banned_calls == []

        ev3 = FakeEvent(
            client=client,
            sender=normal,
            text="",
            media=SimpleNamespace(kind="voice"),
        )
        await message_handler.handle_new_message(bot, ev3)
        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 3
        assert client.edit_banned_calls == []

        # پیام چهارم (آخرین پیام مجاز) -> بلافاصله بعد از آن میوت واقعی API انجام می‌شود
        ev4 = FakeEvent(
            client=client,
            sender=normal,
            text="",
            media=SimpleNamespace(kind="sticker"),
        )
        await message_handler.handle_new_message(bot, ev4)

        rec_after = message_limit.get_record(CHAT_ID, normal.id)
        assert rec_after["sent_count"] == 4
        assert rec_after["status"] == message_limit.STATUS_MUTED
        assert rec_after["muted_by_limit"] is True
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True

        # اثبات استفاده از میوت واقعی API (نه پاک کردن پیام‌ها!)
        assert len(client.edit_banned_calls) == 1
        assert client.edit_banned_calls[0]["uid"] == normal.id
        assert client.edit_banned_calls[0]["send_messages"] is True
        assert client.delete_calls == []

        # بررسی ارسال دقیق قالب ۲
        exp_t2, _ = texts.message_limit_reached(username="@reza_member", limit=4)
        assert len(ev4.replies) == 1
        assert ev4.replies[0]["text"] == exp_t2

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۳: بی‌تأثیر بودن پیام‌های سایر کاربران و ویرایش پیام‌ها روی شمارنده
# ---------------------------------------------------------------------------
def test_3_other_users_and_message_edits_do_not_affect_counter(env):
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        other = env.users["other"]
        manager = env.users["reg_admin"]

        seed_ev = FakeEvent(client=client, sender=normal, text="پیام اولیه")
        set_ev = FakeEvent(
            client=client,
            sender=manager,
            text="محدودیت پیام 3",
            reply_to_msg_id=seed_ev.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, set_ev)

        # کاربر محدودشده ۱ پیام می‌فرستد (1/3)
        m1 = FakeEvent(client=client, sender=normal, text="پیام ۱ من")
        await message_handler.handle_new_message(bot, m1)
        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 1

        # کاربر دیگر ۵ پیام مختلف می‌فرستد -> نباید روی شمارنده اثر بگذارد
        for idx in range(5):
            other_ev = FakeEvent(client=client, sender=other, text=f"پیام کاربر دیگر {idx}")
            await message_handler.handle_new_message(bot, other_ev)
        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 1

        # خود کاربر محدودشده پیام قبلی‌اش را ویرایش می‌کند -> نباید شمرده شود
        edited_ev = FakeEvent(
            client=client,
            sender=normal,
            text="پیام ۱ من (ویرایش‌شده)",
            is_edit=True,
            msg_id=m1.id,
        )
        await message_handler.handle_new_message(bot, edited_ev)
        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 1
        assert client.edit_banned_calls == []

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۴ و ۵: قفل بودن در برابر «آزاد» و «رفع سکوت»، استثنای میوت خودکار ربات،
#  و باز شدن کامل با «لغو محدودیت پیام» بدون تداخل با سایر محدودیت‌ها
# ---------------------------------------------------------------------------
def test_4_and_5_block_azad_and_rafe_sokoot_but_allow_cancel_and_bot_automute_exception(env):
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        auto_user = env.users["auto_muted"]
        manager = env.users["reg_admin"]

        # تنظیم محدودیت ۱ پیام و رسیدن به سقف میوت
        seed = FakeEvent(client=client, sender=normal, text="شروع")
        await message_handler.handle_new_message(
            bot,
            FakeEvent(
                client=client,
                sender=manager,
                text="محدودیت پیام 1",
                reply_to_msg_id=seed.id,
                reply_sender=normal,
            ),
        )
        hit = FakeEvent(client=client, sender=normal, text="تک پیام مجاز")
        await message_handler.handle_new_message(bot, hit)
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True
        assert len(client.edit_banned_calls) == 1

        exp_locked, exp_locked_entities = texts.message_limit_locked_notice()

        # ۴-الف) تلاش برای باز کردن با «آزاد» -> باید مسدود شود و قالب ۳ ارسال گردد
        azad_ev = FakeEvent(
            client=client,
            sender=manager,
            text="آزاد",
            reply_to_msg_id=hit.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, azad_ev)
        await asyncio.sleep(0.05)
        assert len(client.edit_banned_calls) == 1  # هیچ آن‌میوتی رخ نداده
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True
        assert len(azad_ev.replies) == 1
        assert azad_ev.replies[0]["text"] == exp_locked
        assert len(azad_ev.replies[0]["entities"]) == len(exp_locked_entities)

        # ۴-ب) تلاش برای باز کردن با «رفع سکوت» (با ریپلای) -> باید مسدود شود و قالب ۳ بدهد
        rafe_ev = FakeEvent(
            client=client,
            sender=manager,
            text="رفع سکوت",
            reply_to_msg_id=hit.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, rafe_ev)
        await asyncio.sleep(0.05)
        assert len(client.edit_banned_calls) == 1
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True
        assert len(rafe_ev.replies) == 1
        assert rafe_ev.replies[0]["text"] == exp_locked

        # ۴-ج) تلاش برای باز کردن با «رفع سکوت @username» -> باز هم مسدود شود!
        rafe_uname_ev = FakeEvent(
            client=client,
            sender=manager,
            text="رفع سکوت @reza_member",
        )
        await message_handler.handle_new_message(bot, rafe_uname_ev)
        await asyncio.sleep(0.05)
        assert len(client.edit_banned_calls) == 1
        assert rafe_uname_ev.replies[0]["text"] == exp_locked

        # ۴-د) استثنای مهم: کاربری که با سیستم میوت خودکار خود ربات میوت شده (نه محدودیت پیام)
        # باید همچنان با دستور «آزاد» باز شود!
        auto_seed = FakeEvent(client=client, sender=auto_user, text="پیام اسپم قبلی")
        bot.punished_users.add(f"{CHAT_ID}:{auto_user.id}")
        bot.tracker.muted_users[f"{CHAT_ID}:{auto_user.id}"] = 9999999999.0
        azad_auto_ev = FakeEvent(
            client=client,
            sender=manager,
            text="آزاد",
            reply_to_msg_id=auto_seed.id,
            reply_sender=auto_user,
        )
        await message_handler.handle_new_message(bot, azad_auto_ev)
        await asyncio.sleep(0.1)
        assert any(
            c["uid"] == auto_user.id and c["send_messages"] is False
            for c in client.edit_banned_calls
        )

        # ۵-الف) باز شدن کاربر قفل‌شده فقط با «لغو محدودیت پیام» و حذف کامل شمارنده
        cancel_ev = FakeEvent(
            client=client,
            sender=manager,
            text="لغو محدودیت پیام",
            reply_to_msg_id=hit.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, cancel_ev)
        assert message_limit.get_record(CHAT_ID, normal.id) is None
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is False
        exp_t4, _ = texts.message_limit_cancelled()
        assert len(cancel_ev.replies) == 1
        assert cancel_ev.replies[0]["text"] == exp_t4
        assert any(
            c["uid"] == normal.id and c["send_messages"] is False
            for c in client.edit_banned_calls
        )

        # پس از لغو، کاربر بدون محدودیت پیام می‌فرستد و دوباره میوت نمی‌شود
        mute_calls_before = len(client.edit_banned_calls)
        for i in range(4):
            free_msg = FakeEvent(client=client, sender=normal, text=f"پیام آزاد {i}")
            await message_handler.handle_new_message(bot, free_msg)
        assert len(client.edit_banned_calls) == mute_calls_before

        # ۵-ب) حفظ سکوت دستی قبلی: اگر کاربر هم سکوت دستی داشته باشد و هم محدودیتش لغو شود،
        # نباید به اشتباه سکوت دستی‌اش در API باز شود!
        manual_mute_storage.add_manual_mute(CHAT_ID, other_uid := env.users["other"].id, username="sara_member")
        await message_limit.set_limit(
            bot, CHAT_ID, other_uid, 1, actor_id=manager.id, entity=env.users["other"]
        )
        await message_limit.observe_message(
            bot,
            FakeEvent(client=client, sender=env.users["other"], text="تک پیام"),
            CHAT_ID,
            other_uid,
            sender=env.users["other"],
        )
        calls_snapshot = len(client.edit_banned_calls)
        await message_limit.cancel_limit(
            bot, CHAT_ID, other_uid, actor_id=manager.id, entity=env.users["other"]
        )
        assert message_limit.get_record(CHAT_ID, other_uid) is None
        # هیچ درخواست unmute به سرور نرفته چون کاربر سکوت دستی فعال دارد!
        assert len(client.edit_banned_calls) == calls_snapshot

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۶: تست روی ادمین قابل محدودسازی (بومی و ثبت‌شده) و ادمین غیرقابل محدودسازی
# ---------------------------------------------------------------------------
def test_6_restrictable_admins_vs_unrestrictable_native_admin(env):
    async def _run():
        bot = env.bot
        client = env.client
        owner_user = env.users["group_owner"]
        editable_admin = env.users["editable_native_admin"]
        locked_admin = env.users["locked_native_admin"]
        reg_sub_admin = env.users["restricted_reg_admin"]

        # ۶-الف) ادمین ثبت‌شدهٔ ربات قابل محدودسازی است
        s1 = FakeEvent(client=client, sender=reg_sub_admin, text="پیام ادمین ثبت‌شده")
        c1 = FakeEvent(
            client=client,
            sender=owner_user,
            text="محدودیت پیام 2",
            reply_to_msg_id=s1.id,
            reply_sender=reg_sub_admin,
        )
        await message_handler.handle_new_message(bot, c1)
        assert message_limit.has_active_counter(CHAT_ID, reg_sub_admin.id) is True
        await message_handler.handle_new_message(
            bot, FakeEvent(client=client, sender=reg_sub_admin, text="اول")
        )
        await message_handler.handle_new_message(
            bot, FakeEvent(client=client, sender=reg_sub_admin, text="دوم")
        )
        assert message_limit.is_muted_by_limit(CHAT_ID, reg_sub_admin.id) is True

        # ۶-ب) ادمین بومی گروه که ربات اجازهٔ ویرایشش را دارد (can_edit=True)
        s2 = FakeEvent(client=client, sender=editable_admin, text="پیام ادمین بومی قابل محدود")
        c2 = FakeEvent(
            client=client,
            sender=owner_user,
            text="محدودیت پیام 1",
            reply_to_msg_id=s2.id,
            reply_sender=editable_admin,
        )
        await message_handler.handle_new_message(bot, c2)
        assert message_limit.has_active_counter(CHAT_ID, editable_admin.id) is True
        await message_handler.handle_new_message(
            bot, FakeEvent(client=client, sender=editable_admin, text="پیام سقف")
        )
        assert message_limit.is_muted_by_limit(CHAT_ID, editable_admin.id) is True

        # ۶-ج) ادمین ارشد گروه که قابل محدودسازی توسط ربات نیست (can_edit=False)
        s3 = FakeEvent(client=client, sender=locked_admin, text="پیام ادمین غیرقابل محدود")
        c3 = FakeEvent(
            client=client,
            sender=owner_user,
            text="محدودیت پیام 2",
            reply_to_msg_id=s3.id,
            reply_sender=locked_admin,
        )
        await message_handler.handle_new_message(bot, c3)
        assert message_limit.get_record(CHAT_ID, locked_admin.id) is None
        assert c3.replies == []  # هیچ پیام جعلی/تایید اشتباهی ارسال نمی‌شود

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۷ و ۸: معافیت قطعی مالک اصلی ربات و مالک گروه در هر سه مرحله
# ---------------------------------------------------------------------------
def test_7_and_8_strict_exemption_for_global_owner_and_group_owners_in_all_stages(env):
    async def _run():
        bot = env.bot
        client = env.client
        manager = env.users["reg_admin"]

        exempt_targets = [
            env.users["global_owner"],    # ۷) مالک اصلی ثبت‌شده ربات
            env.users["group_owner"],     # ۸-الف) مالک ثبت‌شده گروه
            env.users["native_creator"],  # ۸-ب) سازنده بومی گروه در سروش‌پلاس
        ]

        for protected in exempt_targets:
            # مرحله ۱: تلاش مدیر برای ثبت محدودیت با ریپلای روی مالک -> رد کامل
            seed = FakeEvent(client=client, sender=protected, text="پیام مالک")
            cmd = FakeEvent(
                client=client,
                sender=manager,
                text="محدودیت پیام 2",
                reply_to_msg_id=seed.id,
                reply_sender=protected,
            )
            await message_handler.handle_new_message(bot, cmd)
            assert message_limit.get_record(CHAT_ID, protected.id) is None
            assert cmd.replies == []

            # شبیه‌سازی حالت تغییر مالکیت پس از ثبت قبلی: تزریق مستقیم ردیف در DB
            with runtime_db.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO message_limits (
                        group_id, user_id, allowed_count, sent_count, status,
                        muted_by_limit, epoch, username, display_name,
                        created_at, updated_at
                    ) VALUES (?, ?, 1, 0, 'counting', 0, 1, 'owner', '@owner', 1.0, 1.0)
                    """,
                    (str(CHAT_ID), str(protected.id)),
                )
            message_limit.recover(bot, env.logger)

            # مرحله ۲: ارسال پیام توسط مالک -> نباید شمرده شود و ردیف قدیمی پاک می‌شود
            msg = FakeEvent(client=client, sender=protected, text="پیام جدید مالک")
            triggered = await message_limit.observe_message(
                bot, msg, CHAT_ID, protected.id, sender=protected, logger=env.logger
            )
            assert triggered is False
            assert message_limit.get_record(CHAT_ID, protected.id) is None
            assert client.edit_banned_calls == []

            # مرحله ۳: حتی اگر _execute_auto_mute مستقیماً فراخوانی شود -> هرگز میوت نمی‌کند
            with runtime_db.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO message_limits (
                        group_id, user_id, allowed_count, sent_count, status,
                        muted_by_limit, epoch, username, display_name,
                        created_at, updated_at
                    ) VALUES (?, ?, 1, 1, 'pending_mute', 0, 9, 'owner', '@owner', 1.0, 1.0)
                    """,
                    (str(CHAT_ID), str(protected.id)),
                )
            message_limit.recover(bot, env.logger)
            muted = await message_limit._execute_auto_mute(
                bot, CHAT_ID, protected.id, 9, sender=protected, logger=env.logger
            )
            assert muted is False
            assert message_limit.get_record(CHAT_ID, protected.id) is None
            assert client.edit_banned_calls == []

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۹: تست عددهای نامعتبر، صفر، منفی و ورودی خراب
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_command",
    [
        "محدودیت پیام",
        "محدودیت پیام 0",
        "محدودیت پیام -1",
        "محدودیت پیام -999",
        "محدودیت پیام 2.5",
        "محدودیت پیام چهار",
        "محدودیت پیام abc",
        "محدودیت پیام 4 پیام",
    ],
)
def test_9_invalid_zero_negative_and_malformed_numbers_rejected(env, bad_command):
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        manager = env.users["reg_admin"]

        seed = FakeEvent(client=client, sender=normal, text="سلام")
        ev = FakeEvent(
            client=client,
            sender=manager,
            text=bad_command,
            reply_to_msg_id=seed.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, ev)
        assert message_limit.get_record(CHAT_ID, normal.id) is None
        assert ev.replies == []

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۱۰: ارسال همزمان چند پیام پشت‌سرهم بدون race condition و بدون دوبار میوت شدن
# ---------------------------------------------------------------------------
def test_10_high_concurrency_burst_triggers_exactly_one_mute(env):
    async def _run():
        bot = env.bot
        client = env.client
        client.mute_delay = 0.03  # شبیه‌سازی تأخیر شبکه واقعی هنگام هجوم همزمان پیام‌ها
        normal = env.users["normal"]
        manager = env.users["reg_admin"]

        await message_limit.set_limit(
            bot, CHAT_ID, normal.id, 5, actor_id=manager.id, entity=normal, logger=env.logger
        )

        burst_events = [
            FakeEvent(client=client, sender=normal, text=f"پیام همزمان شماره {i}")
            for i in range(15)
        ]

        await asyncio.gather(
            *(
                message_limit.observe_message(
                    bot,
                    ev,
                    CHAT_ID,
                    normal.id,
                    sender=normal,
                    logger=env.logger,
                )
                for ev in burst_events
            )
        )
        await message_limit.QUEUE.drain()

        rec = message_limit.get_record(CHAT_ID, normal.id)
        assert rec is not None
        assert rec["sent_count"] == 5
        assert rec["status"] == message_limit.STATUS_MUTED
        assert rec["muted_by_limit"] is True

        # دقیقاً یک بار RPC میوت و دقیقاً یک بار پیام سقف ارسال شده است!
        assert len(client.edit_banned_calls) == 1
        reached_msgs = [
            m for m in client.sent_messages if "شما به محدویت ارسال" in m.get("text", "")
        ]
        assert len(reached_msgs) == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۱۱: ری‌استارت ربات وسط شمارش و پس از میوت + بازیابی از SQLite پایدار
# ---------------------------------------------------------------------------
def test_11_persistence_and_recovery_across_bot_restarts(env):
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        manager = env.users["reg_admin"]

        # تنظیم سقف ۴ پیام و ارسال ۲ پیام قبل از ری‌استارت
        await message_limit.set_limit(
            bot, CHAT_ID, normal.id, 4, actor_id=manager.id, entity=normal, logger=env.logger
        )
        for i in range(2):
            ev = FakeEvent(client=client, sender=normal, text=f"قبل از ری‌استارت {i}")
            await message_handler.handle_new_message(bot, ev)

        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 2

        # شبیه‌سازی ری‌استارت کامل پروسه ربات: بستن اتصال SQLite و پاک شدن کل حافظه RAM
        runtime_db.close()
        message_limit.reset_runtime_caches()

        state = message_limit.recover(bot, env.logger)
        assert state["counting"] == 1
        assert message_limit.get_record(CHAT_ID, normal.id)["sent_count"] == 2

        # ارسال ۲ پیام بعدی پس از ری‌استارت -> دقیقاً روی پیام چهارم میوت می‌شود
        ev3 = FakeEvent(client=client, sender=normal, text="پیام سوم بعد ری‌استارت")
        await message_handler.handle_new_message(bot, ev3)
        assert client.edit_banned_calls == []

        ev4 = FakeEvent(client=client, sender=normal, text="پیام چهارم بعد ری‌استارت")
        await message_handler.handle_new_message(bot, ev4)
        assert len(client.edit_banned_calls) == 1
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True

        # ری‌استارت دوم در حالت میوت‌شده -> قفل «آزاد»/«رفع سکوت» همچنان پابرجاست!
        runtime_db.close()
        message_limit.reset_runtime_caches()
        state2 = message_limit.recover(bot, env.logger)
        assert state2["muted"] == 1
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True

        rafe_after_restart = FakeEvent(
            client=client,
            sender=manager,
            text="رفع سکوت",
            reply_to_msg_id=ev4.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, rafe_after_restart)
        assert len(client.edit_banned_calls) == 1  # باز نشده!
        assert rafe_after_restart.replies[0]["text"] == texts.message_limit_locked_notice()[0]

        # لغو با دستور خلاصهٔ راهنما «لغو محدودیت» پس از ری‌استارت
        cancel_short = FakeEvent(
            client=client,
            sender=manager,
            text="لغو محدودیت",
            reply_to_msg_id=ev4.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, cancel_short)
        assert message_limit.get_record(CHAT_ID, normal.id) is None
        assert cancel_short.replies[0]["text"] == texts.message_limit_cancelled()[0]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  سناریوی ۱۲: اجرای دستور بدون ریپلای، روی پیام نامعتبر یا توسط کاربر بدون دسترسی
# ---------------------------------------------------------------------------
def test_12_no_reply_invalid_target_and_unauthorized_caller(env):
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        other = env.users["other"]
        manager = env.users["reg_admin"]

        seed = FakeEvent(client=client, sender=normal, text="پیام معمولی")

        # ۱۲-الف) کاربر عادیِ بدون دسترسی سعی می‌کند محدودیت بگذارد -> رد می‌شود
        unauth_set = FakeEvent(
            client=client,
            sender=other,
            text="محدودیت پیام 2",
            reply_to_msg_id=seed.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, unauth_set)
        assert message_limit.get_record(CHAT_ID, normal.id) is None
        assert unauth_set.replies == []

        # ۱۲-ب) مدیر مجاز بدون ریپلای دستور می‌زند -> رد می‌شود
        no_reply_cmd = FakeEvent(
            client=client,
            sender=manager,
            text="محدودیت پیام 3",
        )
        await message_handler.handle_new_message(bot, no_reply_cmd)
        assert no_reply_cmd.replies == []

        # ۱۲-ج) مدیر روی پیام اکانت حذف‌شده (Deleted Account) ریپلای می‌کند -> رد می‌شود
        deleted_user = _make_user(555999, None, "", deleted=True)
        del_seed = FakeEvent(client=client, sender=deleted_user, text="قدیمی")
        del_cmd = FakeEvent(
            client=client,
            sender=manager,
            text="محدودیت پیام 3",
            reply_to_msg_id=del_seed.id,
            reply_sender=deleted_user,
        )
        await message_handler.handle_new_message(bot, del_cmd)
        assert message_limit.get_record(CHAT_ID, deleted_user.id) is None
        assert del_cmd.replies == []

        # ۱۲-د) مدیر روی پیام سرویس (ورود عضو) ریپلای می‌کند -> رد می‌شود
        svc_seed = FakeEvent(
            client=client,
            sender=normal,
            action=SimpleNamespace(kind="join"),
        )
        svc_cmd = FakeEvent(
            client=client,
            sender=manager,
            text="محدودیت پیام 3",
            reply_to_msg_id=svc_seed.id,
        )
        await message_handler.handle_new_message(bot, svc_cmd)
        assert message_limit.get_record(CHAT_ID, normal.id) is None
        assert svc_cmd.replies == []

        # ۱۲-هـ) اگر کاربر محدودشده خودش بنویسد «لغو محدودیت پیام»، هم رد می‌شود و هم جزو
        # پیام‌های ارسالی‌اش شمرده می‌شود (امکان دور زدن شمارنده وجود ندارد)!
        await message_limit.set_limit(
            bot, CHAT_ID, normal.id, 1, actor_id=manager.id, entity=normal
        )
        bypass_try = FakeEvent(
            client=client,
            sender=normal,
            text="لغو محدودیت پیام",
            reply_to_msg_id=seed.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, bypass_try)
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True
        assert len(client.edit_banned_calls) == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  تست‌های تکمیلی موارد لبه‌ای (بخش ۷ دستور کارفرما)
# ---------------------------------------------------------------------------
def test_edge_multi_group_isolation_and_leave_rejoin_and_no_username(env):
    """جداسازی کامل بین دو گروه مختلف، حفظ شمارنده پس از لفت/عضویت مجدد و کاربر بدون یوزرنیم."""
    async def _run():
        bot = env.bot
        client = env.client
        manager = env.users["reg_admin"]
        chat2 = -1000088002233
        group_storage.activate_group(chat2, "گروه دوم")
        group_storage.set_group_owner(chat2, GROUP_OWNER_ID)
        admin_storage.add_admin(chat2, REG_ADMIN_ID, username="manager_ali")

        # کاربر بدون یوزرنیم که بعداً نامش را تغییر می‌دهد و یوزرنیم می‌گذارد
        nameless = _make_user(499001, None, "امین")
        message_limit.remember_user_entity(bot, nameless)

        await message_limit.set_limit(
            bot, CHAT_ID, nameless.id, 2, actor_id=manager.id, entity=nameless
        )
        await message_limit.set_limit(
            bot, chat2, nameless.id, 5, actor_id=manager.id, entity=nameless
        )

        # در گروه اول ۱ پیام می‌فرستد، سپس لفت می‌دهد و با نام جدید برمی‌گردد
        await message_handler.handle_new_message(
            bot, FakeEvent(client=client, sender=nameless, text="پیام ۱ گروه ۱", chat_id=CHAT_ID)
        )
        # در گروه دوم ۳ پیام می‌فرستد -> نباید روی گروه اول اثر بگذارد
        for idx in range(3):
            await message_handler.handle_new_message(
                bot,
                FakeEvent(client=client, sender=nameless, text=f"گروه ۲ - {idx}", chat_id=chat2),
            )
        assert message_limit.get_record(CHAT_ID, nameless.id)["sent_count"] == 1
        assert message_limit.get_record(chat2, nameless.id)["sent_count"] == 3

        # تغییر نام و افزودن یوزرنیم پس از بازگشت به گروه اول
        renamed = _make_user(499001, "amin_new", "امین جدید")
        ev_final = FakeEvent(client=client, sender=renamed, text="پیام ۲ گروه ۱", chat_id=CHAT_ID)
        await message_handler.handle_new_message(bot, ev_final)

        assert message_limit.is_muted_by_limit(CHAT_ID, nameless.id) is True
        assert message_limit.is_muted_by_limit(chat2, nameless.id) is False
        assert "@amin_new" in ev_final.replies[0]["text"]

    asyncio.run(_run())


def test_edge_reapply_limit_and_partial_rights_restoration_and_bot_self(env):
    """جایگزینی تمیز محدودیت قبلی، بازگردانی حقوق جزئی قبلی در لغو، و معافیت خود ربات."""
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        manager = env.users["reg_admin"]

        # ۱) خود ربات هرگز محدود نمی‌شود
        bot_entity = _make_user(BOT_ID, "foxora_bot", "روباه")
        with pytest.raises(message_limit.ProtectedTargetError):
            await message_limit.set_limit(
                bot, CHAT_ID, BOT_ID, 2, actor_id=manager.id, entity=bot_entity
            )

        # ۲) ثبت مجدد محدودیت روی کاربری که از قبل میوت محدودیت پیام داشته
        await message_limit.set_limit(
            bot, CHAT_ID, normal.id, 1, actor_id=manager.id, entity=normal
        )
        await message_handler.handle_new_message(
            bot, FakeEvent(client=client, sender=normal, text="رسیدن به سقف اول")
        )
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True

        # اعمال سقف جدید ۳ پیام (ارقام فارسی هم پشتیبانی می‌شود!)
        seed = FakeEvent(client=client, sender=normal, text="قدیمی")
        reset_cmd = FakeEvent(
            client=client,
            sender=manager,
            text="محدودیت پیام ۳",
            reply_to_msg_id=seed.id,
            reply_sender=normal,
        )
        await message_handler.handle_new_message(bot, reset_cmd)
        rec = message_limit.get_record(CHAT_ID, normal.id)
        assert rec["allowed_count"] == 3
        assert rec["sent_count"] == 0
        assert rec["muted_by_limit"] is False
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is False

        # ۳) بازگردانی حقوق جزئی قبلی (مثلاً embed_links=True) هنگام لغو محدودیت
        client.supports_get_participant = True
        other = env.users["other"]
        client.participants_by_id[other.id] = types.ChannelParticipantBanned(
            peer=types.PeerUser(user_id=other.id),
            kicked_by=BOT_ID,
            date=None,
            banned_rights=types.ChatBannedRights(
                until_date=None,
                send_messages=False,
                embed_links=True,
            ),
        )
        await message_limit.set_limit(
            bot, CHAT_ID, other.id, 1, actor_id=manager.id, entity=other
        )
        await message_limit.observe_message(
            bot,
            FakeEvent(client=client, sender=other, text="تک پیام"),
            CHAT_ID,
            other.id,
            sender=other,
        )
        await message_limit.cancel_limit(
            bot, CHAT_ID, other.id, actor_id=manager.id, entity=other
        )
        last_call = client.edit_banned_calls[-1]
        assert last_call["uid"] == other.id
        assert last_call["send_messages"] is False
        assert last_call["embed_links"] is True

    asyncio.run(_run())


def test_edge_bot_without_ban_permission_and_api_mute_failure_and_pending_recovery(env):
    """عدم دسترسی ban_users ربات، خطای API هنگام میوت، پاک لیست سکوت و بازیابی pending_mute."""
    async def _run():
        bot = env.bot
        client = env.client
        normal = env.users["normal"]
        manager = env.users["reg_admin"]

        # ۱) اگر ربات حق ban_users نداشته باشد، ثبت محدودیت رد می‌شود
        no_ban_chat = types.Channel(
            id=abs(CHAT_ID) - 1000000000000,
            title="گروه بدون حق بن",
            photo=None,
            date=None,
            creator=False,
            broadcast=False,
            megagroup=True,
            access_hash=55555,
            admin_rights=types.ChatAdminRights(ban_users=False, delete_messages=True),
        )
        with pytest.raises(message_limit.UnrestrictableTargetError):
            await message_limit.set_limit(
                bot, CHAT_ID, normal.id, 2, actor_id=manager.id, entity=normal, chat=no_ban_chat
            )

        # ۲) اگر هنگام اجرای میوت خودکار سرور خطا بدهد، به دروغ میوت‌شدن ثبت نمی‌شود
        client.fail_mute_for.add(normal.id)
        await message_limit.set_limit(
            bot, CHAT_ID, normal.id, 1, actor_id=manager.id, entity=normal
        )
        await message_limit.observe_message(
            bot,
            FakeEvent(client=client, sender=normal, text="پیام منجر به خطای سرور"),
            CHAT_ID,
            normal.id,
            sender=normal,
            logger=env.logger,
        )
        rec = message_limit.get_record(CHAT_ID, normal.id)
        assert rec["status"] == message_limit.STATUS_MUTE_FAILED
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is False
        client.fail_mute_for.discard(normal.id)

        # ۳) شبیه‌سازی کرش دقیقاً در وضعیت pending_mute و تکمیل آن در recover_pending
        bot.admin_actions.circuit_breaker.record_success(CHAT_ID)
        with runtime_db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE message_limits SET status = 'pending_mute', epoch = 55 WHERE group_id = ? AND user_id = ?",
                (group_storage.normalize_group_id(CHAT_ID), str(normal.id)),
            )
        message_limit.reset_runtime_caches()
        message_limit.recover(bot, env.logger)
        await message_limit.recover_pending(bot, env.logger)
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True

        # ۴) دستور «پاک لیست سکوت» نباید کاربر قفل‌شده با محدودیت پیام را باز کند!
        manual_mute_storage.add_manual_mute(CHAT_ID, normal.id, username="reza_member")
        calls_before = len(client.edit_banned_calls)
        clear_ev = FakeEvent(client=client, sender=manager, text="پاک لیست سکوت")
        await message_handler.handle_new_message(bot, clear_ev)
        assert len(client.edit_banned_calls) == calls_before
        assert message_limit.is_muted_by_limit(CHAT_ID, normal.id) is True

    asyncio.run(_run())

