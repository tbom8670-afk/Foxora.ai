# -*- coding: utf-8 -*-
"""تست‌های جامع واحد و یکپارچه برای قابلیت «خاموشی گروه» (قفل خودکار زمان‌بندی‌شده).

پوشش کامل تمام موارد خواسته‌شده:
1. تطابق دقیق قالب‌های تصویری کارفرما و آفست‌های UTF-16 برای Bold و Blockquote.
2. اجرای دستور منحصراً توسط مالک ثبت‌شدهٔ اصلی ربات و رد سایر نقش‌ها (مالک گروه، ادمین گروه، ادمین ثبت‌شده، کاربر عادی) بدون هاردکد شناسه.
3. دریافت و تجزیهٔ زمان در تمام قالب‌ها (ساعت:دقیقه:ثانیه، ساعتی، دقیقه‌ای، ثانیه‌ای، ترکیبی، ارقام فارسی) + رد صفر/منفی/نامعتبر/بیش‌ازحد طولانی.
4. اجرای دقیق در timestamp تعیین‌شده مستقل از ترافیک پیام‌ها و تک‌تسک بودن زمان‌بند مرکزی.
5. قفل واقعی گروه از طریق GroupActions موجود با حفظ کامل دسترسی‌های قبلی گروه (رسانه/لینک/گیف و اسنپ‌شات بازگشتی).
6. عملکرد مستقل چند زمان‌بندی هم‌زمان در گروه‌های مختلف و جایگزینی امن زمان‌بندی قبلی یک گروه.
7. بازیابی زمان‌بندی از دیتابیس پس از ری‌استارت (حفظ زمان باقی‌مانده + اجرای فوری سررسیدهای گذشته در زمان خاموشی).
8. جلوگیری از اجرای تکراری قفل برای یک زمان‌بندی.
9. مدیریت گروهی که از قبل قفل است (عدم ارسال درخواست قفل تکراری).
10. مدیریت خطاهای موقت و دائمی API (تلاش مجدد محدود + عدم جایگزینی با حذف پیام).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from splusthon.tl import functions, types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from handlers import message_handler
from modules import (
    admin_storage,
    admin_tools,
    group_actions as group_actions_mod,
    group_dispatch,
    group_storage,
    owner_check,
    runtime_db,
)
from modules.group_actions import GroupActions, clone_banned_rights, text_is_banned
from modules.manage_features import (
    group_shutdown,
    group_shutdown_commands,
    peers,
    texts,
)


GLOBAL_OWNER_ID = 910001
GROUP_OWNER_ID = 810002
NATIVE_ADMIN_ID = 710003
REG_ADMIN_ID = 610004
NORMAL_USER_ID = 510005
BOT_ID = 111999

CHAT_1 = -1000055001111
CHAT_2 = -1000055002222
CHAT_3 = -1000055003333


def _slice_u16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


def _make_user(uid: int, username: str | None, first_name: str):
    return SimpleNamespace(
        id=uid,
        access_hash=uid * 13,
        username=username,
        first_name=first_name,
        last_name="",
        bot=False,
        deleted=False,
    )


class DummyLogger:
    def __init__(self):
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def log_info(self, msg: str):
        self.infos.append(str(msg))

    def log_error(self, msg: str):
        self.errors.append(str(msg))

    def log_warning(self, msg: str):
        self.warnings.append(str(msg))

    def log_debug(self, msg: str):
        pass

    def log_action(self, *args, **kwargs):
        pass


class FakeGroupLockClient:
    """کلاینت شبیه‌ساز SPlusthon با پیاده‌سازی واقعی GetFullChannel و EditChatDefaultBannedRights."""

    def __init__(self):
        self.group_rights: dict[int, types.ChatBannedRights] = {}
        self.edit_default_rights_calls: list[dict] = []
        self.sent_messages: list[dict] = []
        self.deleted_messages: list[tuple] = []
        self.fail_next_lock_for: dict[int, Exception] = {}
        self.always_fail_lock_for: dict[int, Exception] = {}
        self.ignore_lock_effect_for: set[int] = set()
        self._outgoing_sender_bot = None

    def _channel_id_of(self, target) -> int:
        if hasattr(target, "channel_id"):
            return int(target.channel_id)
        if isinstance(target, types.Channel):
            return int(target.id)
        ival = int(target)
        if ival <= -1_000_000_000_000:
            return abs(ival) - 1_000_000_000_000
        return abs(ival)

    def ensure_group(self, chat_id: int, **initial_banned_flags):
        cid = self._channel_id_of(chat_id)
        self.group_rights[cid] = clone_banned_rights(None, **initial_banned_flags)
        return cid

    def current_rights(self, chat_id: int) -> types.ChatBannedRights:
        cid = self._channel_id_of(chat_id)
        if cid not in self.group_rights:
            self.group_rights[cid] = clone_banned_rights(None)
        return self.group_rights[cid]

    async def get_input_entity(self, target):
        if isinstance(target, types.InputPeerChannel):
            return target
        cid = self._channel_id_of(target)
        return types.InputPeerChannel(channel_id=cid, access_hash=cid * 7 + 99)

    async def get_entity(self, target):
        cid = self._channel_id_of(target)
        return types.Channel(
            id=cid,
            title=f"گروه {cid}",
            photo=None,
            date=None,
            creator=False,
            broadcast=False,
            megagroup=True,
            access_hash=cid * 7 + 99,
            default_banned_rights=self.current_rights(cid),
        )

    async def send_message(self, target, text: str, formatting_entities=None, **kwargs):
        cid = self._channel_id_of(target)
        record = {
            "channel_id": cid,
            "chat_id": -(1_000_000_000_000 + cid),
            "text": text,
            "entities": list(formatting_entities or []),
        }
        self.sent_messages.append(record)
        return SimpleNamespace(id=len(self.sent_messages) + 5000, message=text)

    async def delete_messages(self, target, message_ids, **kwargs):
        self.deleted_messages.append((target, list(message_ids)))
        return SimpleNamespace(pts_count=len(message_ids))

    async def __call__(self, request):
        req_name = type(request).__name__
        if req_name in ("GetFullChannelRequest", "GetChannelsRequest"):
            ch = getattr(request, "channel", None)
            if ch is None:
                ch_list = getattr(request, "id", [])
                ch = ch_list[0]
            cid = self._channel_id_of(ch)
            chat_obj = types.Channel(
                id=cid,
                title=f"گروه {cid}",
                photo=None,
                date=None,
                creator=False,
                broadcast=False,
                megagroup=True,
                access_hash=cid * 7 + 99,
                default_banned_rights=self.current_rights(cid),
            )
            return SimpleNamespace(chats=[chat_obj], users=[])

        if req_name == "EditChatDefaultBannedRightsRequest":
            cid = self._channel_id_of(request.peer)
            chat_id = -(1_000_000_000_000 + cid)
            if chat_id in self.fail_next_lock_for:
                exc = self.fail_next_lock_for.pop(chat_id)
                raise exc
            if chat_id in self.always_fail_lock_for:
                raise self.always_fail_lock_for[chat_id]

            new_rights = request.banned_rights
            self.edit_default_rights_calls.append(
                {
                    "chat_id": chat_id,
                    "channel_id": cid,
                    "send_messages": bool(getattr(new_rights, "send_messages", False)),
                    "send_plain": bool(getattr(new_rights, "send_plain", False)),
                    "send_gifs": bool(getattr(new_rights, "send_gifs", False)),
                    "embed_links": bool(getattr(new_rights, "embed_links", False)),
                    "rights": new_rights,
                }
            )
            if chat_id not in self.ignore_lock_effect_for:
                self.group_rights[cid] = clone_banned_rights(new_rights)
            return SimpleNamespace(chats=[], users=[])

        raise NotImplementedError(f"Unhandled TL request: {req_name}")


class FakeEvent:
    _seq = 2000

    def __init__(self, *, client: FakeGroupLockClient, sender, text: str, chat_id: int = CHAT_1):
        FakeEvent._seq += 1
        self.id = FakeEvent._seq
        self.client = client
        self.chat_id = chat_id
        self.sender = sender
        self.sender_id = sender.id
        self.raw_text = text
        self.text = text
        self.is_private = False
        self.reply_to = None
        cid = abs(chat_id) - 1_000_000_000_000
        self.input_chat = types.InputPeerChannel(channel_id=cid, access_hash=cid * 7 + 99)
        self.chat = types.Channel(
            id=cid,
            title=f"گروه {cid}",
            photo=None,
            date=None,
            creator=False,
            broadcast=False,
            megagroup=True,
            access_hash=cid * 7 + 99,
            admin_rights=types.ChatAdminRights(
                ban_users=True, delete_messages=True, change_info=True
            ),
        )
        self.message = SimpleNamespace(
            id=self.id,
            message=text,
            text=text,
            sender_id=self.sender_id,
            sender=sender,
            reply_to=None,
            media=None,
            action=None,
            fwd_from=None,
            entities=[],
        )
        self.replies: list[dict] = []

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return self.chat

    async def reply(self, text: str, formatting_entities=None, **kwargs):
        rec = {
            "text": text,
            "entities": list(formatting_entities or []),
        }
        self.replies.append(rec)
        return SimpleNamespace(id=self.id + 50000, message=text)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    runtime_db.close()
    db_path = tmp_path / "shutdown_test.sqlite3"
    monkeypatch.setattr(runtime_db, "DB_FILE", db_path)

    owner_file = tmp_path / "owner.json"
    runtime_owner_file = tmp_path / "runtime_owner.json"
    owner_payload = {"user_id": GLOBAL_OWNER_ID, "username": "main_owner"}
    owner_file.write_text(json.dumps(owner_payload), encoding="utf-8")
    runtime_owner_file.write_text(json.dumps(owner_payload), encoding="utf-8")
    monkeypatch.setattr(owner_check, "DEPLOYMENT_FILE", owner_file)
    monkeypatch.setattr(owner_check, "RUNTIME_FILE", runtime_owner_file)
    monkeypatch.setattr(owner_check, "FILE", owner_file)
    owner_check._CACHE_SIGNATURE = None
    owner_check._CACHE_OWNER = None

    lock_state_file = tmp_path / "group_message_lock_state.json"
    monkeypatch.setattr(group_actions_mod, "_LOCK_STATE_FILE", lock_state_file)
    monkeypatch.setattr(admin_storage, "FILE", tmp_path / "admins.json")
    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    monkeypatch.setattr(admin_tools, "_ADMIN_LOG_FILE", tmp_path / "admin_log.json")

    for cid in (CHAT_1, CHAT_2, CHAT_3):
        group_storage.activate_group(cid, f"گروه تست {cid}")
        group_storage.set_group_owner(cid, GROUP_OWNER_ID)
        admin_storage.add_admin(cid, REG_ADMIN_ID, username="reg_admin_user")

    peers.clear_caches()
    group_shutdown.set_clock_for_tests(None)
    group_shutdown.reset_runtime_caches()
    group_shutdown_commands.clear_all_awaiting()

    logger = DummyLogger()
    client = FakeGroupLockClient()
    for cid in (CHAT_1, CHAT_2, CHAT_3):
        client.ensure_group(cid)

    actions = GroupActions(client, logger)
    bot = SimpleNamespace(
        client=client,
        logger=logger,
        bot_account_id=BOT_ID,
        group_actions=actions,
        reply_input_peer_cache={},
        native_group_admin_cache={},
        punished_users=set(),
        spam_lock={},
        is_spam_locked=lambda _k: False,
        clear_spam_lock=lambda _k: None,
        config_manager={},
        detector=SimpleNamespace(
            is_spam=lambda *_a, **_k: (False, ""),
            check_message=lambda *_a, **_k: (False, ""),
        ),
        tracker=SimpleNamespace(
            muted_users={},
            banned_users={},
            warnings={},
            increment_message=lambda *_a, **_k: None,
        ),
    )
    client._outgoing_sender_bot = bot

    users = {
        "global_owner": _make_user(GLOBAL_OWNER_ID, "main_owner", "مالک اصلی"),
        "group_owner": _make_user(GROUP_OWNER_ID, "grp_owner", "مالک گروه"),
        "native_admin": _make_user(NATIVE_ADMIN_ID, "nat_admin", "ادمین گروه"),
        "reg_admin": _make_user(REG_ADMIN_ID, "reg_admin_user", "ادمین ثبت‌شده"),
        "normal": _make_user(NORMAL_USER_ID, "normal_member", "کاربر عادی"),
    }

    yield SimpleNamespace(
        bot=bot,
        client=client,
        logger=logger,
        users=users,
        owner_file=owner_file,
        tmp_path=tmp_path,
    )

    try:
        asyncio.run(group_shutdown.stop_scheduler())
    except Exception:
        pass
    group_shutdown.set_clock_for_tests(None)
    group_shutdown.reset_runtime_caches()
    group_shutdown_commands.clear_all_awaiting()
    peers.clear_caches()
    runtime_db.close()


# ---------------------------------------------------------------------------
#  ۱) تست دقیق هر ۳ قالب اسکرین‌شات کارفرما + آفست‌های UTF-16
# ---------------------------------------------------------------------------
def test_templates_exact_screenshot_and_utf16_entities():
    # قالب ۱: درخواست ساعت قفل خودکار
    t1, e1 = texts.group_shutdown_prompt()
    expected_t1 = (
        "⚙️ : قفل خودکار گروه و خاموشی فعالیت\n"
        "لطفا ساعت قفل خودکار رو به صورت زیر بنویسید\n"
        "دستورات قابل قبول ✓\n"
        "مثال »» 3:10:10\n"
        "مثال »» 20 دقیقه\n"
        "مثال »» 3 ساعت\n"
        "گروه در همان ساعت / دقیقه / ثانیه قفل خواهد شد"
    )
    assert t1 == expected_t1
    bolds_1 = [e for e in e1 if isinstance(e, types.MessageEntityBold)]
    quotes_1 = [e for e in e1 if isinstance(e, types.MessageEntityBlockquote)]
    assert len(bolds_1) == 1
    assert _slice_u16(t1, bolds_1[0].offset, bolds_1[0].length) == expected_t1
    assert len(quotes_1) == 1
    assert _slice_u16(t1, quotes_1[0].offset, quotes_1[0].length) == "⚙️ : قفل خودکار گروه و خاموشی فعالیت"

    # قالب ۲: تأیید ثبت قفل خودکار
    t2, e2 = texts.group_shutdown_scheduled()
    assert t2 == "🕓 : قفل خودکار فعالیت در گروه تنظیم شد"
    assert len(e2) == 1 and isinstance(e2[0], types.MessageEntityBold)
    assert _slice_u16(t2, e2[0].offset, e2[0].length) == t2

    # قالب ۳: اعلان پایان زمان فعالیت و انجام قفل خودکار
    t3, e3 = texts.group_shutdown_executed()
    expected_t3 = (
        "🔐 قفل خودکار انجام شد ✓\n"
        "زمان فعالیت در این گروه به پایان رسید"
    )
    assert t3 == expected_t3
    bolds_3 = [e for e in e3 if isinstance(e, types.MessageEntityBold)]
    quotes_3 = [e for e in e3 if isinstance(e, types.MessageEntityBlockquote)]
    assert len(bolds_3) == 1
    assert _slice_u16(t3, bolds_3[0].offset, bolds_3[0].length) == "🔐 قفل خودکار انجام شد ✓"
    assert len(quotes_3) == 1
    assert _slice_u16(t3, quotes_3[0].offset, quotes_3[0].length) == "🔐 قفل خودکار انجام شد ✓"


# ---------------------------------------------------------------------------
#  ۲) تست سطح دسترسی: فقط مالک اصلی ثبت‌شدهٔ ربات (بدون هاردکد)
# ---------------------------------------------------------------------------
def test_only_global_registered_owner_allowed_and_all_others_rejected(env):
    async def _run():
        bot = env.bot
        client = env.client
        # علامت‌گذاری native_admin در کش ادمین بومی گروه
        bot.native_group_admin_cache[(group_storage.normalize_group_id(CHAT_1), str(NATIVE_ADMIN_ID))] = (
            time.monotonic() + 300.0,
            True,
        )

        # همهٔ نقش‌های غیر از مالک اصلی ربات باید رد شوند:
        for role_key in ("group_owner", "native_admin", "reg_admin", "normal"):
            actor = env.users[role_key]
            ev = FakeEvent(client=client, sender=actor, text="خاموشی گروه")
            await message_handler.handle_new_message(bot, ev)
            assert ev.replies == [], f"نقش {role_key} نباید اجازهٔ شروع خاموشی گروه داشته باشد"
            assert group_shutdown_commands.is_awaiting(CHAT_1, actor.id) is False
            assert group_shutdown.get_schedule(CHAT_1) is None

            # تلاش مستقیم به صورت خطی هم باید رد شود
            ev_inline = FakeEvent(client=client, sender=actor, text="خاموشی گروه 10 دقیقه")
            await message_handler.handle_new_message(bot, ev_inline)
            assert ev_inline.replies == []
            assert group_shutdown.get_schedule(CHAT_1) is None

        # مالک اصلی ثبت‌شدهٔ ربات مجاز است
        g_owner = env.users["global_owner"]
        ev_ok = FakeEvent(client=client, sender=g_owner, text="خاموشی گروه")
        await message_handler.handle_new_message(bot, ev_ok)
        assert len(ev_ok.replies) == 1
        assert ev_ok.replies[0]["text"] == texts.group_shutdown_prompt()[0]
        assert group_shutdown_commands.is_awaiting(CHAT_1, g_owner.id) is True

        # اثبات عدم وابستگی به شناسهٔ هاردکدشده: اگر مالک ثبت‌شده در owner.json به مالک گروه تغییر کند
        new_owner_data = {"user_id": GROUP_OWNER_ID, "username": "grp_owner"}
        env.owner_file.write_text(json.dumps(new_owner_data), encoding="utf-8")
        owner_check._CACHE_SIGNATURE = None
        owner_check._CACHE_OWNER = None

        assert group_shutdown.is_authorized_owner(GLOBAL_OWNER_ID) is False
        assert group_shutdown.is_authorized_owner(GROUP_OWNER_ID) is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  ۳) تست تجزیهٔ انواع فرمت‌های زمان و رد مقادیر نامعتبر/صفر/منفی/بیش‌ازحد طولانی
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw_input,expected_seconds",
    [
        ("3:32:20", 3 * 3600 + 32 * 60 + 20),
        ("03:30:00", 3 * 3600 + 30 * 60),
        ("0:00:45", 45),
        ("02:15", 2 * 3600 + 15 * 60),
        ("2 ساعت", 7200),
        ("30 دقیقه", 1800),
        ("45 ثانیه", 45),
        ("۳ ساعت و ۳۲ دقیقه و ۲۰ ثانیه", 3 * 3600 + 32 * 60 + 20),
        ("2 ساعت 15 دقیقه", 2 * 3600 + 15 * 60),
        ("1 ساعت، 5 ثانیه", 3605),
        ("10 دقیقه و 30 ثانیه", 630),
    ],
)
def test_duration_parsing_valid_formats(raw_input, expected_seconds):
    assert group_shutdown.parse_duration_seconds(raw_input) == expected_seconds


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "   ",
        "0",
        "-5",
        "0:00:00",
        "0 ساعت",
        "0 دقیقه و 0 ثانیه",
        "-2 ساعت",
        "1:-10:00",
        "2:75:00",
        "1:00:99",
        "1:2:3:4",
        "2.5 ساعت",
        "دو ساعت",
        "2 ساعت و 3 ساعت",
        "30 دقیقه اضافی",
        "سلام خوبی",
    ],
)
def test_duration_parsing_rejects_invalid_zero_and_negative(bad_input):
    with pytest.raises(group_shutdown.InvalidDurationError):
        group_shutdown.parse_duration_seconds(bad_input)


def test_duration_parsing_rejects_unreasonably_long_values():
    with pytest.raises(group_shutdown.DurationTooLongError):
        group_shutdown.parse_duration_seconds("1000 ساعت")
    with pytest.raises(group_shutdown.DurationTooLongError):
        group_shutdown.parse_duration_seconds("900:00:00")


# ---------------------------------------------------------------------------
#  ۴) اجرای دقیق در timestamp تعیین‌شده + قفل واقعی با حفظ دسترسی‌های قبلی گروه
# ---------------------------------------------------------------------------
def test_end_to_end_exact_timestamp_and_real_lock_preserving_group_rights(env):
    async def _run():
        bot = env.bot
        client = env.client
        owner = env.users["global_owner"]
        other_admin = env.users["reg_admin"]

        fake_now = [1_700_000_000.0]
        group_shutdown.set_clock_for_tests(lambda: fake_now[0])

        # فرض: گروه باز است اما از قبل ارسال گیف و پیش‌نمایش لینک در آن بسته بوده است
        client.ensure_group(
            CHAT_1,
            send_messages=False,
            send_plain=False,
            send_gifs=True,
            embed_links=True,
        )

        # گام ۱: مالک اصلی «خاموشی گروه» می‌فرستد (مسیر LANE_ADMIN)
        prio, lane = group_dispatch.classify_priority("خاموشی گروه")
        assert (prio, lane) == (group_dispatch.PRIORITY_ADMIN, group_dispatch.LANE_ADMIN)

        ev1 = FakeEvent(client=client, sender=owner, text="خاموشی گروه")
        await message_handler.handle_new_message(bot, ev1)
        assert len(ev1.replies) == 1
        assert ev1.replies[0]["text"] == texts.group_shutdown_prompt()[0]

        # در حین انتظار مالک، پیام ادمین دیگر نباید زمان‌بندی بسازد
        ev_noise = FakeEvent(client=client, sender=other_admin, text="45 ثانیه")
        await message_handler.handle_new_message(bot, ev_noise)
        assert group_shutdown.get_schedule(CHAT_1) is None
        assert group_shutdown_commands.is_awaiting(CHAT_1, owner.id) is True

        # ورودی صفر/نامعتبر توسط مالک نباید زمان‌بندی بسازد ولی وضعیت انتظار را باز نگه می‌دارد
        ev_bad = FakeEvent(client=client, sender=owner, text="0:00:00")
        await message_handler.handle_new_message(bot, ev_bad)
        assert group_shutdown.get_schedule(CHAT_1) is None
        assert group_shutdown_commands.is_awaiting(CHAT_1, owner.id) is True

        # گام ۲: مالک مقدار «3:32:20» وارد می‌کند (۱۲۷۴۰ ثانیه)
        ev2 = FakeEvent(client=client, sender=owner, text="3:32:20")
        p2, l2 = group_dispatch.classify_priority("3:32:20", ev2)
        assert (p2, l2) == (group_dispatch.PRIORITY_ADMIN, group_dispatch.LANE_ADMIN)

        await message_handler.handle_new_message(bot, ev2)
        assert len(ev2.replies) == 1
        assert ev2.replies[0]["text"] == texts.group_shutdown_scheduled()[0]
        assert group_shutdown_commands.is_awaiting(CHAT_1, owner.id) is False

        rec = group_shutdown.get_schedule(CHAT_1)
        assert rec is not None
        assert rec["owner_id"] == str(GLOBAL_OWNER_ID)
        assert rec["duration_seconds"] == 12740
        assert rec["shutdown_ts"] == pytest.approx(1_700_000_000.0 + 12740.0)
        assert rec["status"] == group_shutdown.STATUS_SCHEDULED
        assert rec["lock_status"] == group_shutdown.LOCK_PENDING

        # ۱ ثانیه مانده به زمان تعیین‌شده -> هنوز نباید قفل شود!
        fake_now[0] = 1_700_000_000.0 + 12739.0
        await group_shutdown.run_due_once(bot, env.logger)
        assert text_is_banned(client.current_rights(CHAT_1)) is False
        assert client.edit_default_rights_calls == []

        # دقیقاً در لحظهٔ رسیدن به timestamp تعیین‌شده -> قفل واقعی اجرا می‌شود
        fake_now[0] = 1_700_000_000.0 + 12740.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        assert len(client.edit_default_rights_calls) == 1
        lock_call = client.edit_default_rights_calls[0]
        assert lock_call["chat_id"] == CHAT_1
        assert lock_call["send_messages"] is True
        assert lock_call["send_plain"] is True
        # دسترسی‌های قبلی گروه (send_gifs و embed_links) دست‌نخورده حفظ شده‌اند
        assert lock_call["send_gifs"] is True
        assert lock_call["embed_links"] is True
        assert text_is_banned(client.current_rights(CHAT_1)) is True

        rec_after = group_shutdown.get_schedule(CHAT_1)
        assert rec_after["status"] == group_shutdown.STATUS_EXECUTED
        assert rec_after["lock_status"] == group_shutdown.LOCK_LOCKED
        assert rec_after["executed_at"] == pytest.approx(fake_now[0])

        # اعلان پایان فعالیت با قالب دقیق اسکرین‌شات در گروه ارسال شده است
        assert len(client.sent_messages) == 1
        sent = client.sent_messages[0]
        exp_exec_text, _ = texts.group_shutdown_executed()
        assert sent["chat_id"] == CHAT_1
        assert sent["text"] == exp_exec_text

        # اگر بعداً گروه با سیستم موجود unlock_group باز شود، محدودیت‌های قبلی گیف/لینک دقیقاً برمی‌گردند
        await bot.group_actions.unlock_group(CHAT_1)
        unlocked_rights = client.current_rights(CHAT_1)
        assert text_is_banned(unlocked_rights) is False
        assert unlocked_rights.send_gifs is True
        assert unlocked_rights.embed_links is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  ۵) چند زمان‌بندی هم‌زمان در گروه‌های مختلف + تک‌تسک بودن زمان‌بند مرکزی
# ---------------------------------------------------------------------------
def test_concurrent_multi_group_schedules_and_single_central_scheduler(env):
    async def _run():
        bot = env.bot
        client = env.client

        fake_now = [2_000_000_000.0]
        group_shutdown.set_clock_for_tests(lambda: fake_now[0])

        await group_shutdown.schedule_shutdown(bot, CHAT_1, GLOBAL_OWNER_ID, "10 ثانیه")
        await group_shutdown.schedule_shutdown(bot, CHAT_2, GLOBAL_OWNER_ID, "25 ثانیه")
        await group_shutdown.schedule_shutdown(bot, CHAT_3, GLOBAL_OWNER_ID, "25 ثانیه")

        # اطمینان از اینکه فقط یک تسک زمان‌بند مرکزی ساخته شده (نه sleep به ازای هر گروه!)
        sched_tasks = [
            t for t in asyncio.all_tasks() if t.get_name() == "foxora-group-shutdown-scheduler"
        ]
        assert len(sched_tasks) == 1

        # در ثانیهٔ +10 فقط گروه ۱ قفل می‌شود و گروه ۲ و ۳ باز می‌مانند
        fake_now[0] = 2_000_000_010.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        assert text_is_banned(client.current_rights(CHAT_1)) is True
        assert text_is_banned(client.current_rights(CHAT_2)) is False
        assert text_is_banned(client.current_rights(CHAT_3)) is False

        # در ثانیهٔ +25 هر دو گروه ۲ و ۳ به‌طور مستقل قفل می‌شوند
        fake_now[0] = 2_000_000_025.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        assert text_is_banned(client.current_rights(CHAT_2)) is True
        assert text_is_banned(client.current_rights(CHAT_3)) is True
        assert len(client.sent_messages) == 3

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  ۶) بازیابی پس از ری‌استارت (سررسید گذشته در زمان خاموشی + زمان باقی‌مانده)
# ---------------------------------------------------------------------------
def test_persistence_and_recovery_after_bot_restart(env):
    async def _run():
        bot = env.bot
        client = env.client

        fake_now = [3_000_000_000.0]
        group_shutdown.set_clock_for_tests(lambda: fake_now[0])

        # گروه ۱: ۳۰ ثانیه (در زمان خاموش بودن ربات منقضی می‌شود)
        # گروه ۲: ۱۲۰ ثانیه (پس از ری‌استارت هنوز ۷۵ ثانیه باقی دارد)
        await group_shutdown.schedule_shutdown(bot, CHAT_1, GLOBAL_OWNER_ID, "30 ثانیه")
        await group_shutdown.schedule_shutdown(bot, CHAT_2, GLOBAL_OWNER_ID, "2 دقیقه")

        # شبیه‌سازی خاموشی کامل ربات و عبور ۴۵ ثانیه در زمان خاموشی
        await group_shutdown.stop_scheduler()
        group_shutdown.reset_runtime_caches()
        fake_now[0] = 3_000_000_045.0

        state = group_shutdown.recover(bot, env.logger)
        assert state["active"] == 2
        assert state["overdue"] == 1

        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        # گروه ۱ بلافاصله پس از بالا آمدن قفل شده، ولی گروه ۲ هنوز باز است!
        assert text_is_banned(client.current_rights(CHAT_1)) is True
        assert group_shutdown.get_schedule(CHAT_1)["status"] == group_shutdown.STATUS_EXECUTED
        assert text_is_banned(client.current_rights(CHAT_2)) is False
        assert group_shutdown.get_schedule(CHAT_2)["status"] == group_shutdown.STATUS_SCHEDULED

        # وقتی ساعت به +120 می‌رسد، گروه ۲ دقیقاً سر وقت قفل می‌شود
        fake_now[0] = 3_000_000_120.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()
        assert text_is_banned(client.current_rights(CHAT_2)) is True
        assert group_shutdown.get_schedule(CHAT_2)["status"] == group_shutdown.STATUS_EXECUTED

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  ۷) جلوگیری از قفل تکراری، جایگزینی زمان‌بندی فعال و گروه از قبل قفل‌شده
# ---------------------------------------------------------------------------
def test_no_duplicate_lock_schedule_replacement_and_already_locked_group(env):
    async def _run():
        bot = env.bot
        client = env.client

        fake_now = [4_000_000_000.0]
        group_shutdown.set_clock_for_tests(lambda: fake_now[0])

        # الف) ثبت زمان جدید روی گروهی که زمان‌بندی فعال دارد -> جایگزینی بدون تداخل
        r1 = await group_shutdown.schedule_shutdown(bot, CHAT_1, GLOBAL_OWNER_ID, "10 ثانیه")
        r2 = await group_shutdown.schedule_shutdown(bot, CHAT_1, GLOBAL_OWNER_ID, "1 ساعت")
        assert r2["schedule_id"] == r1["schedule_id"] + 1

        # در ثانیهٔ +10 زمان‌بندی قبلی باطل شده و گروه نباید قفل شود!
        fake_now[0] = 4_000_000_010.0
        await group_shutdown.run_due_once(bot, env.logger)
        assert text_is_banned(client.current_rights(CHAT_1)) is False

        # در ثانیهٔ +3600 فقط یک بار قفل می‌شود، حتی اگر چند بار run_due_once فراخوانی شود
        fake_now[0] = 4_000_003_600.0
        await asyncio.gather(
            group_shutdown.run_due_once(bot, env.logger),
            group_shutdown.run_due_once(bot, env.logger),
            group_shutdown.run_due_once(bot, env.logger),
        )
        await group_shutdown.QUEUE.drain()
        assert len(client.edit_default_rights_calls) == 1
        assert len(client.sent_messages) == 1

        # ب) گروه ۲ از قبل قفل است -> در لحظهٔ رسیدن زمان هیچ RPC قفل تکراری ارسال نمی‌شود
        client.ensure_group(CHAT_2, send_messages=True, send_plain=True)
        calls_before = len(client.edit_default_rights_calls)
        await group_shutdown.schedule_shutdown(bot, CHAT_2, GLOBAL_OWNER_ID, "20 ثانیه")
        fake_now[0] += 20.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        assert len(client.edit_default_rights_calls) == calls_before
        rec2 = group_shutdown.get_schedule(CHAT_2)
        assert rec2["status"] == group_shutdown.STATUS_EXECUTED
        assert rec2["lock_status"] == group_shutdown.LOCK_ALREADY_LOCKED

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  ۸) مدیریت خطاهای API (تلاش مجدد خطای موقت + عدم جایگزینی با حذف پیام در خطای دائمی)
# ---------------------------------------------------------------------------
def test_api_error_handling_transient_retry_and_permanent_refusal(env):
    async def _run():
        bot = env.bot
        client = env.client

        fake_now = [5_000_000_000.0]
        group_shutdown.set_clock_for_tests(lambda: fake_now[0])

        # الف) خطای موقت شبکه در تلاش اول -> زمان‌بندی در وضعیت retry_pending می‌ماند و در تلاش دوم قفل می‌کند
        client.fail_next_lock_for[CHAT_1] = ConnectionError("temporary network reset")
        await group_shutdown.schedule_shutdown(bot, CHAT_1, GLOBAL_OWNER_ID, "15 ثانیه")
        fake_now[0] += 15.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        rec_retry = group_shutdown.get_schedule(CHAT_1)
        assert rec_retry["status"] == group_shutdown.STATUS_RETRY_PENDING
        assert rec_retry["attempts"] == 1
        assert text_is_banned(client.current_rights(CHAT_1)) is False

        fake_now[0] = rec_retry["next_attempt_ts"] + 0.1
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        rec_ok = group_shutdown.get_schedule(CHAT_1)
        assert rec_ok["status"] == group_shutdown.STATUS_EXECUTED
        assert rec_ok["lock_status"] == group_shutdown.LOCK_LOCKED
        assert text_is_banned(client.current_rights(CHAT_1)) is True

        # ب) خطای دائمی/عدم اعمال واقعی قفل توسط API -> گزارش خطا و ثبت failed، بدون حذف پیام‌ها!
        client.ignore_lock_effect_for.add(CHAT_2)
        await group_shutdown.schedule_shutdown(bot, CHAT_2, GLOBAL_OWNER_ID, "10 ثانیه")
        fake_now[0] += 10.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()

        rec_fail = group_shutdown.get_schedule(CHAT_2)
        assert rec_fail["status"] == group_shutdown.STATUS_FAILED
        assert rec_fail["lock_status"] == group_shutdown.LOCK_FAILED
        assert "did not take effect" in (rec_fail["last_error"] or "")
        assert client.deleted_messages == [], "هرگز نباید قفل گروه با حذف پیام جایگزین شود"
        assert any("GROUP_SHUTDOWN" in err for err in env.logger.errors)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  ۹) تست اجرای واقعی با ساعت حقیقی سیستم (بدون هیچ پیامی در گروه)
# ---------------------------------------------------------------------------
def test_realtime_autonomous_execution_without_any_group_messages(env):
    async def _run():
        bot = env.bot
        client = env.client

        started = time.time()
        await group_shutdown.schedule_shutdown(bot, CHAT_1, GLOBAL_OWNER_ID, "1 ثانیه")
        assert text_is_banned(client.current_rights(CHAT_1)) is False

        # بدون ارسال هیچ پیام دیگری، زمان‌بند مرکزی خودش باید سرِ ثانیهٔ ۱ گروه را قفل کند
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if text_is_banned(client.current_rights(CHAT_1)):
                break
            await asyncio.sleep(0.05)

        elapsed = time.time() - started
        assert text_is_banned(client.current_rights(CHAT_1)) is True
        assert 0.9 <= elapsed < 2.5
        assert group_shutdown.get_schedule(CHAT_1)["status"] == group_shutdown.STATUS_EXECUTED

    asyncio.run(_run())


# ---------------------------------------------------------------------------
#  ۱۰) تست لغو زمان‌بندی، عبور دستورات دیگر حین انتظار و بازیابی با کش خالی
# ---------------------------------------------------------------------------
def test_cancel_passthrough_and_cold_cache_recovery(env):
    async def _run():
        bot = env.bot
        client = env.client
        owner = env.users["global_owner"]
        grp_owner = env.users["group_owner"]

        fake_now = [6_000_000_000.0]
        group_shutdown.set_clock_for_tests(lambda: fake_now[0])

        # الف) ارسال دستور مدیریتی دیگر در حین انتظار باید وضعیت انتظار را ببندد و تداخلی ایجاد نکند
        ev_start = FakeEvent(client=client, sender=owner, text="خاموشی گروه")
        await message_handler.handle_new_message(bot, ev_start)
        assert group_shutdown_commands.is_awaiting(CHAT_1, owner.id) is True

        handled = await group_shutdown_commands.handle(
            bot,
            FakeEvent(client=client, sender=owner, text="لیست ادمینی"),
            CHAT_1,
            owner.id,
            owner,
            "لیست ادمینی",
            env.logger,
        )
        assert handled is False
        assert group_shutdown_commands.is_awaiting(CHAT_1, owner.id) is False

        # ب) ثبت زمان‌بندی خطی و عدم امکان لغو توسط مالک گروه (غیر از مالک اصلی ربات)
        ev_inline = FakeEvent(client=client, sender=owner, text="خاموشی گروه 2 ساعت")
        await message_handler.handle_new_message(bot, ev_inline)
        assert len(ev_inline.replies) == 1
        assert ev_inline.replies[0]["text"] == texts.group_shutdown_scheduled()[0]
        assert group_shutdown.get_schedule(CHAT_1)["duration_seconds"] == 7200

        ev_bad_cancel = FakeEvent(client=client, sender=grp_owner, text="لغو خاموشی گروه")
        await message_handler.handle_new_message(bot, ev_bad_cancel)
        assert group_shutdown.get_schedule(CHAT_1)["status"] == group_shutdown.STATUS_SCHEDULED

        # مالک اصلی لغو می‌کند -> پس از رسیدن به زمان، گروه قفل نمی‌شود
        ev_ok_cancel = FakeEvent(client=client, sender=owner, text="لغو خاموشی گروه")
        await message_handler.handle_new_message(bot, ev_ok_cancel)
        assert group_shutdown.get_schedule(CHAT_1)["status"] == group_shutdown.STATUS_CANCELLED

        fake_now[0] += 7200.0
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()
        assert text_is_banned(client.current_rights(CHAT_1)) is False

        # ج) بازیابی از دیتابیس با کش کاملاً خالی (reply_input_peer_cache خالی)
        await group_shutdown.schedule_shutdown(bot, CHAT_3, GLOBAL_OWNER_ID, "45 ثانیه")
        await group_shutdown.stop_scheduler()
        group_shutdown.reset_runtime_caches()
        bot.reply_input_peer_cache.clear()
        peers.clear_caches()

        fake_now[0] += 50.0
        group_shutdown.recover(bot, env.logger)
        await group_shutdown.run_due_once(bot, env.logger)
        await group_shutdown.QUEUE.drain()
        assert text_is_banned(client.current_rights(CHAT_3)) is True
        assert group_shutdown.get_schedule(CHAT_3)["status"] == group_shutdown.STATUS_EXECUTED

    asyncio.run(_run())

