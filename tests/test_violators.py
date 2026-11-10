# -*- coding: utf-8 -*-
"""🧪 تست قابلیت «متخلف» ( modules/manage_features/violators ).

اجرا:

    PYTHONPATH=. python3 tests/test_violators.py

پوشش (مطابق سناریوهای خواستهٔ کارفرما):

۱.  ثبت موفق + ساختارِ دقیقِ storage + حذفِ پیامِ ریپلای‌شده
۲.  ریپلای روی مالکِ ثبت‌شده ⇒ رد («هدف محافظت‌شده»)
۳.  ریپلای روی سازندهٔ گروه (ChannelParticipantCreator) ⇒ رد
۴.  ریپلای روی خود ربات ⇒ رد
۵.  متخلفِ ادمینِ گروه (native) ⇒ پیام‌هایش حذف می‌شود
۶.  متخلفِ ادمینِ ثبت‌شده که «لغو متخلف» می‌زند ⇒ پیام حذف، دستور پردازش نمی‌شود
۷.  پیام ویرایش‌شده ⇒ حذف
۸.  رسانه و استیکر (بدون متن) ⇒ حذف
۹.  ری‌استارت ربات ⇒ رکوردها می‌مانند و ایندکس بازسازی می‌شود
۱۰. ربات بدون حقِ حذف ⇒ ثبت نمی‌شود + پیام روشن
۱۱. لغو متخلف ⇒ کاربر دوباره می‌تواند پیام بدهد
۱۲. گروه‌های دیگر تأثیر نمی‌بینند
۱۳. سازندهٔ ناشناس (خطای فنی) ⇒ ثبت نمی‌شود
۱۴. قالب‌ها: Bold + نقل‌قول با آفستِ UTF-16
۱۵. لیست ادمینی: ردیف تازه + سلامتِ بلوک‌های قدیمی («پاک لیست اخراج» …)
۱۶. آمار: شمارندهٔ جدا (نه «حذف شده»، نه اسپم)
۱۷. لغوِ رکورد «اختصاصی» هنگام ثبتِ متخلف
۱۸. دفاع دوم در اجرا: رکوردِ اشتباهِ مالک/سازنده حذف و پیام حفظ می‌شود
۱۹. بدون ریپلای ⇒ پیام راهنما؛ ادمینِ ثبت‌نشده ⇒ بدون مجوز
۲۰. ادمین بی‌نام (هویتِ گروه) قابل شناسایی نیست ⇒ رد می‌شود (محدودیت مستند)
۲۱. دسته‌بندیِ حذف: چند پیامِ هم‌زمان ⇒ یک RPC با peer صحیحِ کانال
۲۲. از قبل متخلف / متخلف نیست / حذفِ ناموفق فقط لاگ می‌شود
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_INSTANCE", "test")
os.environ.setdefault("BOT_HOME", tempfile.mkdtemp(prefix="foxora-violators-"))

from splusthon.tl.types import (ChatAdminRights, ChatBannedRights,   # noqa: E402
                                MessageEntityBlockquote, MessageEntityBold)

from modules import admin_storage, group_stats, group_storage  # noqa: E402
from modules.manage_features import (access, exclusive,  # noqa: E402
                                     violator_commands, violators)
from modules.manage_features import queue as mqueue  # noqa: E402
from modules.manage_features import storage, texts  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES = []

CHAT_ID = -(10 ** 12) - 501           # شکلِ کانالِ سروش‌پلاس
CHAT_SHORT = 501
CHAT2_ID = -(10 ** 12) - 777
CREATOR_ID = 4242                     # سازندهٔ گروه (chatParticipantCreator)
OWNER_ID = 4343                       # مالکِ ثبت‌شدهٔ گروه (در ربات)
REGISTERED_ADMIN_ID = 515             # ادمینِ ثبت‌شدهٔ ربات
NATIVE_ADMIN_ID = 616                 # ادمینِ خودِ سروش (ثبت‌نشده در ربات)
TARGET_ID = 7001                      # کاربرِ عادیِ هدف
BOT_ID = 999


def check(title, condition, extra=None):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {title}")
    else:
        FAILED += 1
        FAILURES.append(f"{title} :: {extra!r}")
        print(f"  ❌ {title}   {extra!r}")


def run(coro):
    return asyncio.run(coro)


def u16(text):
    return len(str(text).encode("utf-16-le")) // 2


def slice16(text, offset, length):
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le")


# ---------------------------------------------------------------------------
#  فیک‌های واقع‌نما (همان شکلِ RPCهای واقعیِ استفاده‌شده)
# ---------------------------------------------------------------------------
class FakeLogger:
    def __init__(self):
        self.infos, self.errors = [], []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))


class FakeUser:
    def __init__(self, user_id, username=None, first_name="کاربر"):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = None


class FakeInputPeer:
    """InputPeerChannelِ ساختگی (channel_id + access_hash)."""

    def __init__(self, channel_id, access_hash=12345):
        self.channel_id = channel_id
        self.access_hash = access_hash


class FakeChat:
    def __init__(self, chat_id, creator_id=CREATOR_ID):
        self.chat_id = chat_id
        self.creator_id = creator_id
        #: ادمین‌های nativeِ گروه (user_id → ChatAdminRights)
        self.admins = {}
        self.members = set()
        self.deleted = []
        self.delete_targets = []
        #: آیا ربات حقِ حذف پیام دارد؟
        self.bot_can_delete = True
        #: وضعیت قفلِ گروه (برای تعامل با «اختصاصی»)
        self.locked = False
        self.default_banned_rights = ChatBannedRights(until_date=None)
        #: آیا ربات حقِ «افزودن ادمین» دارد؟ (مسیرِ «اختصاصی»)
        self.bot_can_add_admins = False
        #: GetParticipant چند بار خطای گذرا بدهد
        self.participant_read_fails = 0
        #: فهرست اعضا (get_participants) خطا بدهد
        self.participants_list_fails = False

    def snapshot(self):
        return ChatBannedRights(until_date=None)


class FakeClient:
    def __init__(self, chat, users=None):
        self.chat = chat
        self.users = users or {}
        self.calls = []
        self.sent = []
        #: نویسندهٔ پیامِ ریپلای‌شده (پیش‌فرض: کاربرِ عادی)
        self.reply_sender_id = TARGET_ID
        self.delete_calls = 0
        #: آیا resolveِ یوزرنیم کار می‌کند؟ (برای آزمونِ «یوزرنیم پیدا نشد»)
        self.entity_lookup_works = True

    # ---- خودِ ربات ----
    async def get_me(self):
        return FakeUser(BOT_ID, "foxora_bot")

    def _bot_rights(self):
        flags = ("change_info", "post_messages", "edit_messages",
                 "delete_messages", "ban_users", "invite_users", "pin_messages",
                 "add_admins", "anonymous", "manage_call", "other",
                 "manage_topics")
        return ChatAdminRights(**{
            flag: (flag == "delete_messages" and self.chat.bot_can_delete)
            or (flag == "add_admins" and self.chat.bot_can_add_admins)
            for flag in flags})

    def _participant_for(self, user_id):
        if user_id == BOT_ID:
            return type("ChannelParticipantAdmin", (), {
                "banned_rights": ChatBannedRights(until_date=None),
                "admin_rights": self._bot_rights(),
                "user_id": user_id})()
        if user_id == self.chat.creator_id:
            return type("ChannelParticipantCreator", (), {
                "banned_rights": ChatBannedRights(until_date=None),
                "admin_rights": self._bot_rights(),
                "rank": "",
                "user_id": user_id})()
        if user_id in self.chat.admins:
            return type("ChannelParticipantAdmin", (), {
                "banned_rights": ChatBannedRights(until_date=None),
                "admin_rights": self.chat.admins[user_id],
                "user_id": user_id})()
        return type("ChannelParticipant", (), {
            "banned_rights": ChatBannedRights(until_date=None),
            "user_id": user_id})()

    async def __call__(self, request, *args, **kwargs):
        name = type(request).__name__
        self.calls.append(name)
        if name in {"GetFullChannelRequest", "GetChannelsRequest"}:
            rights = self.chat.default_banned_rights
            chat = type("Chat", (), {
                "id": CHAT_SHORT,
                "default_banned_rights": rights,
            })()
            return type("Full", (), {
                "chats": [chat],
                "full_chat": type("FC", (), {
                    "participants_count": 3,
                    "default_banned_rights": rights,
                })(),
            })()
        if name == "GetParticipantRequest":
            if self.chat.participant_read_fails:
                self.chat.participant_read_fails -= 1
                raise ConnectionError("temporary read failure")
            participant = getattr(request, "participant", None)
            user_id = getattr(participant, "user_id", None)
            if user_id is None:
                user_id = getattr(participant, "id", None)
            return type("ChannelParticipantResult", (), {
                "participant": self._participant_for(user_id),
                "user_id": user_id})()
        return True

    async def get_participants(self, chat_id, limit=None, *, filter=None,
                               **_kwargs):
        self.calls.append("get_participants")
        if self.chat.participants_list_fails:
            raise ConnectionError("participants list unavailable")
        rows = []
        member_ids = set(self.chat.members) | {BOT_ID, self.chat.creator_id}
        # سازنده اول (TL: chatParticipantCreator)، بعد بقیه
        ordered = [self.chat.creator_id] + sorted(
            member_ids - {self.chat.creator_id})
        for user_id in ordered:
            user = self.users.get(user_id) or FakeUser(user_id)
            row = type("ParticipantRow", (), {
                "id": user_id,
                "user_id": user_id,
                "username": getattr(user, "username", None),
                "participant": self._participant_for(user_id),
                "is_admin": user_id in self.chat.admins
                or user_id == BOT_ID,
                "is_creator": user_id == self.chat.creator_id,
            })()
            rows.append(row)
        return rows

    async def get_messages(self, chat_id, ids=None, **_kwargs):
        self.calls.append("get_messages")
        sender = self.users.get(self.reply_sender_id) or \
            FakeUser(self.reply_sender_id)
        return type("Message", (), {
            "id": ids,
            "get_sender": _sender_getter(sender),
        })()

    async def get_entity(self, target, *_args, **_kwargs):
        self.calls.append("get_entity")
        if isinstance(target, int):
            return self.users.get(target)
        text = str(target).strip().lstrip("@").lower()
        if not self.entity_lookup_works:
            raise ValueError(f"cannot find entity {target!r}")
        for user in self.users.values():
            if str(getattr(user, "username", "") or "").lower() == text:
                return user
        # رفتار واقعیِ SPlusthon: یوزرنیمِ ناشناخته ValueError می‌دهد.
        raise ValueError(f"cannot find entity {target!r}")

    async def send_message(self, chat_id, text, formatting_entities=None,
                           **_kwargs):
        sent_id = 500000 + len(self.sent)
        self.sent.append((chat_id, text, list(formatting_entities or []),
                          sent_id))
        return type("Message", (), {"id": sent_id, "message": text,
                                    "peer_id": chat_id})()

    async def delete_messages(self, entity, ids, revoke=True, **_kwargs):
        ids = [int(value) for value in ids]
        self.delete_calls += 1
        self.chat.delete_targets.append(entity)
        self.chat.deleted.extend(ids)
        return type("Affected", (), {"pts_count": len(ids),
                                     "deleted_ids": ids})()


def _sender_getter(user):
    async def _get_sender(*_args, **_kwargs):
        return user
    return _get_sender


class FakeBot:
    def __init__(self, chat, users=None):
        self.chat = chat
        self.client = FakeClient(chat, users)
        self.logger = FakeLogger()
        self.bot_account_id = BOT_ID
        self.reply_input_peer_cache = {}


class FakeEvent:
    is_private = False

    def __init__(self, chat_id, user, text="", message_id=8001,
                 reply_to_id=None, is_edit=False, is_media=False,
                 is_service=False, reply_sender=None):
        self.chat_id = chat_id
        self.sender = user
        self.sender_id = getattr(user, "id", None)
        self.is_edit = is_edit
        self.message = type("Msg", (), {
            "id": message_id,
            "message": text,
            "caption": "" if is_media else None,
            "file": object() if is_media else None,
            "edit_date": 111 if is_edit else None,
            "action": object() if is_service else None,
        })()
        self.reply_to = (type("Reply", (), {
            "reply_to_msg_id": reply_to_id,
            "reply_to_message": None,
        })() if reply_to_id else None)
        if reply_sender is not None:
            self._bot_cached_reply_sender = reply_sender
        self.replies = []

    async def reply(self, text, formatting_entities=None, **_kwargs):
        self.replies.append((text, list(formatting_entities or [])))
        return type("Sent", (), {"id": 9000 + len(self.replies)})()


def fresh(chat_id=CHAT_ID):
    """ری‌ست کامل: storage + حافظه + صف‌ها + ادمین‌ها."""
    storage.reset_all()
    violators.reset_all()
    violator_commands.reset_all()
    exclusive.forget_group(chat_id)
    for module_queue in (violators.QUEUE, violator_commands.QUEUE):
        mqueue.reset_queue(module_queue)
    group_storage.set_group_owner(chat_id, OWNER_ID)
    admin_storage.add_admin(chat_id, REGISTERED_ADMIN_ID, "reg_admin")
    return make_bot(chat_id)


def make_bot(chat_id=CHAT_ID, creator_id=CREATOR_ID):
    chat = FakeChat(chat_id, creator_id=creator_id)
    users = {
        TARGET_ID: FakeUser(TARGET_ID, "target_user"),
        NATIVE_ADMIN_ID: FakeUser(NATIVE_ADMIN_ID, "native_admin"),
        REGISTERED_ADMIN_ID: FakeUser(REGISTERED_ADMIN_ID, "reg_admin"),
        OWNER_ID: FakeUser(OWNER_ID, "group_owner"),
        CREATOR_ID: FakeUser(CREATOR_ID, "creator"),
    }
    bot = FakeBot(chat, users)
    # InputPeer کانال از پیش کش‌شده (همان چیزی که ربات واقعی دارد)
    bot.reply_input_peer_cache[chat_id] = FakeInputPeer(CHAT_SHORT)
    chat.members.update(users)
    return bot


def simulate_restart():
    """شبیه‌سازیِ ری‌استارت: حافظهٔ ماژول خالی می‌شود، فایل‌ها می‌مانند."""
    for task in list(violators._FLUSHERS.values()):
        try:
            task.cancel()
        except Exception:                            # noqa: BLE001
            pass
    violators._FLUSHERS.clear()
    violators._PENDING.clear()
    violators._INDEX.clear()
    violators._INDEX_READY = False
    violators._CREATOR_CACHE.clear()
    violators._CREATOR_LOCKS.clear()
    violators._FAIL_STATE.clear()
    storage.VIOLATORS_STORAGE._invalidate()


def reply_event(bot, actor, target_id, text="متخلف", reply_msg_id=4321,
                chat_id=CHAT_ID, **kwargs):
    """رویدادِ دستور با ریپلای روی پیامِ کاربرِ هدف."""
    bot.client.reply_sender_id = target_id
    return FakeEvent(chat_id, actor, text=text, reply_to_id=reply_msg_id,
                     **kwargs)


# ---------------------------------------------------------------------------
#  ۱) ثبت موفق + ساختار storage + حذف پیام ریپلای‌شده
# ---------------------------------------------------------------------------
def test_01_mark_success():
    print("\n### ۱) ثبت موفق")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]
    event = reply_event(bot, owner, TARGET_ID)

    async def scenario():
        return await violator_commands.handle(
            bot, event, CHAT_ID, OWNER_ID, owner, "متخلف", bot.logger)

    run(scenario())
    record = violators.get_record(CHAT_ID, TARGET_ID)
    check("رکورد ثبت شد", record is not None)
    check("ساختار دقیقاً {since, set_by, set_by_username}",
          record is not None and set(record) == {"since", "set_by",
                                                 "set_by_username"},
          record)
    check("set_by مالک ثبت‌شده است",
          record is not None and str(record.get("set_by")) == str(OWNER_ID))
    check("set_by_username ذخیره شد",
          record is not None and record.get("set_by_username") == "group_owner",
          record)
    check("since عددی (UTC epoch) است",
          record is not None and isinstance(record.get("since"), (int, float)))
    check("پیام ریپلای‌شده هم حذف شد", 4321 in bot.chat.deleted,
          bot.chat.deleted)
    check("پاسخ ربات قالب «ثبت شد» است",
          any("در لیست متخلف گروه" in text
              for text, _entities in event.replies), event.replies)
    check("پاسخ، عنوانِ نقل‌قول‌دار دارد",
          any(any(type(e).__name__ == "MessageEntityBlockquote"
                  for e in entities)
              for _text, entities in event.replies), event.replies)
    # فایل روی دیسک هم همان ساختار است
    data = storage.VIOLATORS_STORAGE.load()
    check("فایل violators.json ساختار {chat_id: {user_id: {...}}} دارد",
          data.get(storage.group_key(CHAT_ID), {}).get(
              str(TARGET_ID)) is not None, data)


# ---------------------------------------------------------------------------
#  ۲) ریپلای روی مالک ثبت‌شده ⇒ رد
# ---------------------------------------------------------------------------
def test_02_mark_registered_owner_rejected():
    print("\n### ۲) هدف: مالک ثبت‌شدهٔ گروه")
    bot = fresh()
    admin = bot.client.users[REGISTERED_ADMIN_ID]
    event = reply_event(bot, admin, OWNER_ID)

    async def scenario():
        return await violator_commands.handle(bot, event, CHAT_ID,
                                              REGISTERED_ADMIN_ID, admin,
                                              "متخلف", bot.logger)

    run(scenario())
    check("رکوردی ثبت نشد", violators.get_record(CHAT_ID, OWNER_ID) is None)
    check("پیام «محافظت‌شده» داده شد",
          any("محافظت شده" in text for text, _ in event.replies),
          event.replies)
    check("هیچ پیامی حذف نشد", not bot.chat.deleted, bot.chat.deleted)


# ---------------------------------------------------------------------------
#  ۳) ریپلای روی سازندهٔ گروه ⇒ رد
# ---------------------------------------------------------------------------
def test_03_mark_creator_rejected():
    print("\n### ۳) هدف: سازندهٔ گروه (ChannelParticipantCreator)")
    bot = fresh()
    # سازنده (4242) عمداً از مالکِ ثبت‌شدهٔ ربات (4343) جدا است تا مسیرِ
    # تشخیصِ creator از API واقعاً تست شود.
    admin = bot.client.users[REGISTERED_ADMIN_ID]
    event = reply_event(bot, admin, CREATOR_ID)

    async def scenario():
        return await violator_commands.handle(bot, event, CHAT_ID,
                                              REGISTERED_ADMIN_ID, admin,
                                              "متخلف", bot.logger)

    run(scenario())
    check("رکوردی برای سازنده ثبت نشد",
          violators.get_record(CHAT_ID, CREATOR_ID) is None)
    check("پیام «محافظت‌شده» داده شد",
          any("محافظت شده" in text for text, _ in event.replies),
          event.replies)
    check("get_participants برای تشخیص سازنده صدا زده شد",
          "get_participants" in bot.client.calls or
          "GetParticipantRequest" in bot.client.calls)


# ---------------------------------------------------------------------------
#  ۴) ریپلای روی خود ربات ⇒ رد
# ---------------------------------------------------------------------------
def test_04_mark_bot_rejected():
    print("\n### ۴) هدف: خود ربات")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]
    event = reply_event(bot, owner, BOT_ID)

    async def scenario():
        return await violator_commands.handle(bot, event, CHAT_ID, OWNER_ID,
                                              owner, "متخلف", bot.logger)

    run(scenario())
    check("رکوردی برای ربات ثبت نشد",
          violators.get_record(CHAT_ID, BOT_ID) is None)
    check("پیام «محافظت‌شده» داده شد",
          any("محافظت شده" in text for text, _ in event.replies),
          event.replies)


# ---------------------------------------------------------------------------
#  ۵) متخلفِ ادمینِ گروه (native) ⇒ پیام‌هایش حذف می‌شود
# ---------------------------------------------------------------------------
def test_05_native_admin_violator_deleted():
    print("\n### ۵) متخلفِ ادمینِ خودِ سروش")
    bot = fresh()
    bot.chat.admins[NATIVE_ADMIN_ID] = ChatAdminRights(
        **{flag: False for flag in (
            "change_info", "post_messages", "edit_messages",
            "delete_messages", "ban_users", "invite_users", "pin_messages",
            "add_admins", "anonymous", "manage_call", "other",
            "manage_topics")})
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, NATIVE_ADMIN_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        event = FakeEvent(CHAT_ID, bot.client.users[NATIVE_ADMIN_ID],
                          text="سلام", message_id=8500)
        return await violators.gate_message(bot, event, CHAT_ID,
                                            NATIVE_ADMIN_ID, event.sender,
                                            8500, logger=bot.logger)

    handled = run(scenario())
    check("ادمینِ گروه متخلف شد",
          violators.get_record(CHAT_ID, NATIVE_ADMIN_ID) is not None)
    check("پیام ادمینِ گروه حذف شد", 8500 in bot.chat.deleted,
          bot.chat.deleted)
    check("پردازش متوقف شد (gate=True)", handled is True)
    check("حذف با peer کانال انجام شد",
          any(getattr(target, "channel_id", None) == CHAT_SHORT
              for target in bot.chat.delete_targets) or
          all(isinstance(target, int) and target < 0
              for target in bot.chat.delete_targets),
          bot.chat.delete_targets)


# ---------------------------------------------------------------------------
#  ۶) متخلفِ ادمینِ ثبت‌شده که «لغو متخلف» می‌زند
# ---------------------------------------------------------------------------
def test_06_registered_admin_violator_cannot_unmark_self():
    print("\n### ۶) متخلفِ ادمینِ ثبت‌شده و دستور «لغو متخلف» برای خودش")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, REGISTERED_ADMIN_ID), CHAT_ID,
            OWNER_ID, owner, "متخلف", bot.logger)
        # ادمینِ ثبت‌شدهٔ متخلف، «لغو متخلف» روی پیام خودش می‌زند
        event = FakeEvent(CHAT_ID, bot.client.users[REGISTERED_ADMIN_ID],
                          text="لغو متخلف", message_id=8600,
                          reply_to_id=8601)
        gated = await violators.gate_message(
            bot, event, CHAT_ID, REGISTERED_ADMIN_ID, event.sender, 8600,
            logger=bot.logger)
        if gated:
            return True, event
        # اگر گیت رد کرد، روتر دستورها اجرا می‌شود (نباید)
        await violator_commands.handle(
            bot, event, CHAT_ID, REGISTERED_ADMIN_ID, event.sender,
            "لغو متخلف", bot.logger)
        return False, event

    gated, event = run(scenario())
    check("پیام متخلف پیش از روتر حذف شد", 8600 in bot.chat.deleted,
          bot.chat.deleted)
    check("پردازش متوقف شد (gate=True)", gated is True)
    check("دستور «لغو متخلف» پردازش نشد (هیچ پاسخی داده نشد)",
          not event.replies, event.replies)
    check("رکورد متخلف باقی ماند",
          violators.get_record(CHAT_ID, REGISTERED_ADMIN_ID) is not None)


# ---------------------------------------------------------------------------
#  ۷) پیام ویرایش‌شده ⇒ حذف
# ---------------------------------------------------------------------------
def test_07_edited_message_deleted():
    print("\n### ۷) پیام ویرایش‌شده")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        event = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID],
                          text="متن ویرایش‌شده", message_id=8700,
                          is_edit=True)
        return await violators.gate_message(bot, event, CHAT_ID, TARGET_ID,
                                            event.sender, 8700,
                                            logger=bot.logger)

    handled = run(scenario())
    check("پیام ویرایش‌شده حذف شد", 8700 in bot.chat.deleted,
          bot.chat.deleted)
    check("پردازش متوقف شد", handled is True)


# ---------------------------------------------------------------------------
#  ۸) رسانه و استیکر (بدون متن) ⇒ حذف
# ---------------------------------------------------------------------------
def test_08_media_and_sticker_deleted():
    print("\n### ۸) رسانه و استیکر")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        media = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID], text="",
                          message_id=8800, is_media=True)
        gated_media = await violators.gate_message(
            bot, media, CHAT_ID, TARGET_ID, media.sender, 8800,
            logger=bot.logger)
        sticker = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID], text="",
                            message_id=8801, is_media=True)
        gated_sticker = await violators.gate_message(
            bot, sticker, CHAT_ID, TARGET_ID, sticker.sender, 8801,
            logger=bot.logger)
        return gated_media, gated_sticker

    gated_media, gated_sticker = run(scenario())
    check("پیام رسانه‌ای حذف شد", 8800 in bot.chat.deleted,
          bot.chat.deleted)
    check("استیکر حذف شد", 8801 in bot.chat.deleted, bot.chat.deleted)
    check("هر دو پیام پردازش متوقف شد",
          gated_media is True and gated_sticker is True)


# ---------------------------------------------------------------------------
#  ۹) ری‌استارت ربات ⇒ رکوردها می‌مانند
# ---------------------------------------------------------------------------
def test_09_restart_keeps_records():
    print("\n### ۹) ری‌استارت ربات")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def mark_scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)

    run(mark_scenario())
    check("رکورد پیش از ری‌استارت موجود است",
          violators.get_record(CHAT_ID, TARGET_ID) is not None)

    # «ری‌استارت»: فقط حافظهٔ ماژول پاک می‌شود (فایلِ دیسک دست‌نخورده،
    # دقیقاً مثل بالا آمدنِ دوبارهٔ پروسه با همان data/)
    simulate_restart()
    check("پس از پاک‌سازیِ حافظه، ایندکس خالی است",
          violators.is_violator(CHAT_ID, TARGET_ID) is False)

    bot2 = make_bot()
    state = violators.recover(bot2, bot2.logger)
    check("recover رکوردها را می‌شمارد", state["users"] == 1, state)
    check("ایندکس پس از ری‌استارت بازسازی شد",
          violators.is_violator(CHAT_ID, TARGET_ID) is True)
    check("گروه دیگر درست است: کاربر عادی در گروه دوم متخلف نیست",
          violators.is_violator(CHAT2_ID, TARGET_ID) is False)


# ---------------------------------------------------------------------------
#  ۱۰) ربات بدون حق حذف ⇒ ثبت نمی‌شود
# ---------------------------------------------------------------------------
def test_10_bot_without_delete_rights():
    print("\n### ۱۰) ربات بدون حق حذف پیام")
    bot = fresh()
    bot.chat.bot_can_delete = False
    owner = bot.client.users[OWNER_ID]
    event = reply_event(bot, owner, TARGET_ID)

    async def scenario():
        return await violator_commands.handle(bot, event, CHAT_ID, OWNER_ID,
                                              owner, "متخلف", bot.logger)

    run(scenario())
    check("رکوردی ثبت نشد", violators.get_record(CHAT_ID, TARGET_ID) is None)
    check("پیام روشن «ربات حق حذف ندارد» داده شد",
          any("حق حذف پیام ندارد" in text for text, _ in event.replies),
          event.replies)


# ---------------------------------------------------------------------------
#  ۱۱) لغو متخلف
# ---------------------------------------------------------------------------
def test_11_unmark():
    print("\n### ۱۱) لغو متخلف")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        event = reply_event(bot, owner, TARGET_ID, text="لغو متخلف")
        await violator_commands.handle(bot, event, CHAT_ID, OWNER_ID, owner,
                                       "لغو متخلف", bot.logger)
        check("پیام «لغو شد» داده شد",
              any("میتواند پیام ارسال کند" in text
                  for text, _ in event.replies), event.replies)
        after = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID], text="سلام",
                          message_id=8900)
        return await violators.gate_message(bot, after, CHAT_ID, TARGET_ID,
                                            after.sender, 8900,
                                            logger=bot.logger)

    handled = run(scenario())
    check("رکورد حذف شد", violators.get_record(CHAT_ID, TARGET_ID) is None)
    check("کاربر دوباره می‌تواند پیام بدهد (gate=False)", handled is False)
    check("هیچ پیامی حذف نشد", 8900 not in bot.chat.deleted,
          bot.chat.deleted)


# ---------------------------------------------------------------------------
#  ۱۲) گروه‌های دیگر تأثیر نمی‌بینند
# ---------------------------------------------------------------------------
def test_12_other_groups_unaffected():
    print("\n### ۱۲) گروه‌های دیگر")
    bot = fresh()
    group_storage.set_group_owner(CHAT2_ID, OWNER_ID)
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        event = FakeEvent(CHAT2_ID, bot.client.users[TARGET_ID],
                          text="سلام گروه دوم", message_id=8950)
        return await violators.gate_message(bot, event, CHAT2_ID, TARGET_ID,
                                            event.sender, 8950,
                                            logger=bot.logger)

    handled = run(scenario())
    check("کاربر در گروه دوم متخلف نیست",
          violators.is_violator(CHAT2_ID, TARGET_ID) is False)
    check("پیام گروه دوم حذف نشد", 8950 not in bot.chat.deleted,
          bot.chat.deleted)
    check("پردازش ادامه دارد (gate=False)", handled is False)


# ---------------------------------------------------------------------------
#  ۱۳) سازندهٔ ناشناس (خطای فنی) ⇒ ثبت نمی‌شود
# ---------------------------------------------------------------------------
def test_13_creator_unknown_refuses_mark():
    print("\n### ۱۳) ناشناخته‌ماندنِ سازنده ⇒ ثبت نمی‌شود")
    bot = fresh()
    bot.chat.participant_read_fails = 5       # خواندنِ participant خطا
    bot.chat.participants_list_fails = True    # فهرستِ اعضا هم خطا
    owner = bot.client.users[OWNER_ID]
    event = reply_event(bot, owner, TARGET_ID)

    async def scenario():
        return await violator_commands.handle(bot, event, CHAT_ID, OWNER_ID,
                                              owner, "متخلف", bot.logger)

    run(scenario())
    check("رکوردی ثبت نشد", violators.get_record(CHAT_ID, TARGET_ID) is None)
    check("پیام خطای فنی («مالک گروه خوانده نشد») داده شد",
          any("مالک گروه خوانده نشد" in text for text, _ in event.replies),
          event.replies)


# ---------------------------------------------------------------------------
#  ۱۴) قالب‌ها: Bold + نقل‌قول با آفستِ UTF-16
# ---------------------------------------------------------------------------
def test_14_templates():
    print("\n### ۱۴) قالب‌های پیام")
    text, entities = texts.violator_message(username="@target_user",
                                            kind="marked")
    bolds = [e for e in entities if isinstance(e, MessageEntityBold)]
    quotes = [e for e in entities if isinstance(e, MessageEntityBlockquote)]
    check("کل متن Bold است",
          len(bolds) == 1 and bolds[0].offset == 0 and
          bolds[0].length == u16(text), (bolds, u16(text)))
    check("عنوان داخل نقل‌قول است",
          len(quotes) == 1 and
          slice16(text, quotes[0].offset, quotes[0].length) ==
          "⛔️ کاربر : @target_user", quotes)
    check("متن «ثبت شد» عیناً مطابق تصویر",
          "در لیست متخلف گروه از این پس پیام های کاربری پاک خواهد شد" in text,
          text)
    text2, _e2 = texts.violator_message(username="@target_user",
                                        kind="unmarked")
    check("متن «لغو شد» عیناً مطابق تصویر",
          "از لیست متخلف گروه از این پس کاربر میتواند پیام ارسال کند"
          in text2, text2)
    text3, entities3 = texts.violator_message(username="@u", kind="protected")
    check("قالب خطا هم header+نقل‌قول دارد",
          any(isinstance(e, MessageEntityBlockquote) for e in entities3) and
          "⛔️ کاربر : @u" in text3, text3)


# ---------------------------------------------------------------------------
#  ۱۵) لیست ادمینی: ردیف تازه + سلامتِ بلوک‌های قدیمی
# ---------------------------------------------------------------------------
def test_15_admin_list():
    print("\n### ۱۵) لیست ادمینی")
    from modules.admin_help_builder import (ADMIN_HELP_ENTITIES,
                                            ADMIN_HELP_TEXT)
    check("ردیف «متخلف» اضافه شد",
          "⛔️ : برای اینکه یک کاربر ربات اجازه نده پیامی ازش ارسال بشه "
          "داخل گروه با ریپلای" in ADMIN_HELP_TEXT)
    needle = "\nمتخلف\nلغو متخلف"
    pos = ADMIN_HELP_TEXT.find(needle)
    check("دو دستور پشت سر هم هستند", pos != -1)
    quotes = [e for e in ADMIN_HELP_ENTITIES
              if isinstance(e, MessageEntityBlockquote)]
    covered = [e for e in quotes
               if slice16(ADMIN_HELP_TEXT, e.offset, e.length) ==
               "متخلف\nلغو متخلف"]
    check("«متخلف»/«لغو متخلف» داخل **یک** نقل‌قول‌اند",
          len(covered) == 1, covered)
    bolds = [e for e in ADMIN_HELP_ENTITIES
             if isinstance(e, MessageEntityBold)]
    bold_covers = [e for e in bolds
                   if slice16(ADMIN_HELP_TEXT, e.offset, e.length).find(
                       "متخلف") != -1 and
                   slice16(ADMIN_HELP_TEXT, e.offset, e.length).find(
                       "⛔️") != -1]
    check("عنوان و هر دو دستور Bold هستند", len(bold_covers) >= 1,
          bold_covers)
    check("همهٔ entityها در محدودهٔ متن‌اند",
          all(e.offset + e.length <= u16(ADMIN_HELP_TEXT)
              for e in ADMIN_HELP_ENTITIES))
    # بلوک‌های قدیمی سالم مانده‌اند
    for expected in ("پاک لیست اخراج",
                     "اخراج نام ← اخراج نام بیو چک\nسکوت نام ← سکوت نام بیو چک",
                     "اختصاصی\nلغو اختصاصی",
                     "پیام زماندار\nلغو پیام زماندار",
                     "تماس با سازنده : @OSINE2"):
        found = any(slice16(ADMIN_HELP_TEXT, e.offset, e.length) == expected
                    for e in quotes)
        check(f"نقل‌قول قدیمی سالم است: {expected[:24]!r}", found)


# ---------------------------------------------------------------------------
#  ۱۶) آمار: شمارندهٔ جدا
# ---------------------------------------------------------------------------
def test_16_stats_counter():
    print("\n### ۱۶) شمارندهٔ جدا در آمار")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        event = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID], text="۱",
                          message_id=9001)
        await violators.gate_message(bot, event, CHAT_ID, TARGET_ID,
                                     event.sender, 9001, logger=bot.logger)
        event2 = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID], text="۲",
                           message_id=9002)
        await violators.gate_message(bot, event2, CHAT_ID, TARGET_ID,
                                     event2.sender, 9002, logger=bot.logger)

    run(scenario())
    stats = group_stats.get_stats(CHAT_ID)
    check("شمارندهٔ violator_deleted افزایش یافت",
          stats.get("violator_deleted", 0) >= 1, stats)
    check("شمارندهٔ «حذف شده» (دستی/خودکار) دست‌نخورده مانده",
          stats.get("deleted", 0) == 0, stats)
    check("شمارندهٔ پیام‌ها هم افزایش نیافته (مسیر آمار اجرا نشده)",
          stats.get("messages", 0) == 0, stats)


# ---------------------------------------------------------------------------
#  ۱۷) لغوِ رکورد «اختصاصی» هنگام ثبتِ متخلف
# ---------------------------------------------------------------------------
def test_17_exclusive_revoked_on_mark():
    print("\n### ۱۷) لغوِ «اختصاصی» هنگام ثبتِ متخلف")
    bot = fresh()
    # گروه قفل است و ربات حقِ «افزودن ادمین» دارد تا «اختصاصی» معنا داشته
    # باشد (همان پیش‌شرطِ موتورِ اختصاصی).
    bot.chat.locked = True
    bot.chat.bot_can_add_admins = True
    bot.chat.default_banned_rights = ChatBannedRights(
        until_date=None, send_messages=True, send_plain=True)
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        return exclusive.is_exclusive(CHAT_ID, TARGET_ID)

    # پیش‌شرط: رکورد اختصاصی بسازیم (از موتورِ موجود)
    async def setup():
        await exclusive.grant(bot, CHAT_ID, TARGET_ID, actor_id=OWNER_ID,
                              name="target", username="target_user",
                              entity=bot.client.users[TARGET_ID])
        return exclusive.is_exclusive(CHAT_ID, TARGET_ID)

    was_exclusive = run(setup())
    check("رکورد اختصاصی ساخته شد", was_exclusive is True)
    run(scenario())
    check("رکورد اختصاصی پس از ثبتِ متخلف لغو شد",
          exclusive.is_exclusive(CHAT_ID, TARGET_ID) is False)


# ---------------------------------------------------------------------------
#  ۱۸) دفاع دوم در اجرا: رکوردِ اشتباهِ مالک/سازنده
# ---------------------------------------------------------------------------
def test_18_runtime_protection_drops_wrong_record():
    print("\n### ۱۸) دفاع دوم در زمان اجرا")
    bot = fresh()

    async def scenario():
        # رکوردِ اشتباه: مستقیم در storage نوشته می‌شود (مثل رکوردِ قدیمی)
        violators._save_record(CHAT_ID, OWNER_ID, actor_id=1, username="x")
        event = FakeEvent(CHAT_ID, bot.client.users[OWNER_ID],
                          text="سلام مالک", message_id=9100)
        gated = await violators.gate_message(bot, event, CHAT_ID, OWNER_ID,
                                             event.sender, 9100,
                                             logger=bot.logger)
        # رکوردِ اشتباه برای سازنده
        violators._save_record(CHAT_ID, CREATOR_ID, actor_id=1, username="y")
        event2 = FakeEvent(CHAT_ID, bot.client.users[CREATOR_ID],
                           text="سلام سازنده", message_id=9101)
        gated2 = await violators.gate_message(bot, event2, CHAT_ID,
                                              CREATOR_ID, event2.sender,
                                              9101, logger=bot.logger)
        return gated, gated2

    gated, gated2 = run(scenario())
    check("پیام مالک حذف نشد", 9100 not in bot.chat.deleted,
          bot.chat.deleted)
    check("رکورد اشتباهِ مالک پاک شد",
          violators.get_record(CHAT_ID, OWNER_ID) is None)
    check("پیام سازنده حذف نشد", 9101 not in bot.chat.deleted,
          bot.chat.deleted)
    check("رکورد اشتباهِ سازنده پاک شد",
          violators.get_record(CHAT_ID, CREATOR_ID) is None)
    check("پردازش پیام‌های محافظت‌شده ادامه دارد",
          gated is False and gated2 is False)


# ---------------------------------------------------------------------------
#  ۱۹) بدون ریپلای ⇒ راهنما؛ ادمینِ ثبت‌نشده ⇒ بدون مجوز
# ---------------------------------------------------------------------------
def test_19_no_reply_and_permission():
    print("\n### ۱۹) بدون ریپلای / بدون مجوز")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]
    event = FakeEvent(CHAT_ID, owner, text="متخلف", message_id=9200)

    async def scenario():
        await violator_commands.handle(bot, event, CHAT_ID, OWNER_ID, owner,
                                       "متخلف", bot.logger)

    run(scenario())
    check("بدون ریپلای پیام راهنما داده شد",
          any("ریپلای" in text for text, _ in event.replies), event.replies)

    bot2 = fresh()
    native = bot2.client.users[NATIVE_ADMIN_ID]
    event2 = reply_event(bot2, native, TARGET_ID)

    async def scenario2():
        return await violator_commands.handle(
            bot2, event2, CHAT_ID, NATIVE_ADMIN_ID, native, "متخلف",
            bot2.logger)

    run(scenario2())
    check("ادمینِ ثبت‌نشده رد شد",
          any("فقط مالک یا ادمین ثبت شده" in text
              for text, _ in event2.replies), event2.replies)
    check("رکوردی ثبت نشد",
          violators.get_record(CHAT_ID, TARGET_ID) is None)


# ---------------------------------------------------------------------------
#  ۲۰) ادمین بی‌نام (هویتِ گروه) قابل شناسایی نیست
# ---------------------------------------------------------------------------
def test_20_anonymous_admin_skipped():
    print("\n### ۲۰) ادمین بی‌نام (هویتِ گروه)")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        # پیامی که با هویتِ خودِ گروه می‌آید (from_id = کانال)
        channel_sender = type("Channel", (), {
            "id": CHAT_ID, "channel_id": CHAT_SHORT,
            "username": None, "title": "گروه"})()
        event = FakeEvent(CHAT_ID, channel_sender, text="پیام بی‌نام",
                          message_id=9300)
        return await violators.gate_message(bot, event, CHAT_ID,
                                            CHAT_ID, channel_sender, 9300,
                                            logger=bot.logger)

    handled = run(scenario())
    check("پیام بی‌نام حذف نشد", 9300 not in bot.chat.deleted,
          bot.chat.deleted)
    check("پردازش ادامه دارد (gate=False)", handled is False)


# ---------------------------------------------------------------------------
#  ۲۱) دسته‌بندیِ حذف: چند پیامِ هم‌زمان ⇒ یک RPC با peer صحیح
# ---------------------------------------------------------------------------
def test_21_batched_delete():
    print("\n### ۲۱) دسته‌بندیِ حذف")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        target = bot.client.users[TARGET_ID]
        # سه پیامِ هم‌زمان (همان چیزی که در شلوغیِ گروه رخ می‌دهد)
        events = [FakeEvent(CHAT_ID, target, text=f"پیام {message_id}",
                            message_id=message_id)
                  for message_id in (9400, 9401, 9402)]
        results = await asyncio.gather(*[
            violators.gate_message(bot, event, CHAT_ID, TARGET_ID, target,
                                   message_id, logger=bot.logger)
            for event, message_id in zip(events, (9400, 9401, 9402))])
        return list(results)

    results = run(scenario())
    check("هر سه پیام متوقف شدند", all(result is True for result in results),
          results)
    check("هر سه شناسه حذف شدند",
          all(mid in bot.chat.deleted for mid in (9400, 9401, 9402)),
          bot.chat.deleted)
    check("حذف‌ها دسته‌ای بودند (۱ RPC برای ۳ پیام + ۱ برای ریپلای)",
          bot.client.delete_calls == 2, bot.client.delete_calls)
    check("شناسه‌ها با peer صحیحِ کانال حذف شدند",
          all(getattr(target, "channel_id", None) == CHAT_SHORT
              or (isinstance(target, int) and target == CHAT_ID)
              for target in bot.chat.delete_targets),
          bot.chat.delete_targets)


# ---------------------------------------------------------------------------
#  ۲۲) از قبل متخلف / متخلف نیست / حذفِ ناموفق فقط لاگ
# ---------------------------------------------------------------------------
def test_22_already_and_missing():
    print("\n### ۲۲) تکراری / متخلف نیست")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        event = reply_event(bot, owner, TARGET_ID, text="متخلف")
        await violator_commands.handle(bot, event, CHAT_ID, OWNER_ID, owner,
                                       "متخلف", bot.logger)
        already = any("از قبل" in text for text, _ in event.replies)
        event2 = reply_event(bot, owner, NATIVE_ADMIN_ID,
                             text="لغو متخلف")
        await violator_commands.handle(bot, event2, CHAT_ID, OWNER_ID, owner,
                                       "لغو متخلف", bot.logger)
        missing = any("در لیست متخلف گروه نیست" in text
                      for text, _ in event2.replies)
        return already, missing

    already, missing = run(scenario())
    check("پیام «از قبل متخلف است» داده شد", already is True)
    check("پیام «متخلف نیست» برای کاربرِ غیرثبت‌شده داده شد",
          missing is True)


def test_22b_failed_delete_only_logged():
    print("\n### ۲۲ب) حذفِ ناموفق فقط لاگ می‌شود")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    class FailingClient(type(bot.client)):
        async def delete_messages(self, entity, ids, revoke=True, **_kwargs):
            raise ConnectionError("network down")

    bot.client.__class__ = FailingClient

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        event = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID], text="x",
                          message_id=9500)
        gated = await violators.gate_message(bot, event, CHAT_ID, TARGET_ID,
                                             event.sender, 9500,
                                             logger=bot.logger)
        await asyncio.sleep(0.1)
        return gated

    gated = run(scenario())
    check("گیت همچنان پیام را متوقف می‌کند", gated is True)
    check("خطا لاگ شد (نه استثنا)",
          any("VIOLATOR DELETE FAILED" in message
              for message in bot.logger.errors) or
          any("VIOLATOR" in message for message in bot.logger.errors),
          bot.logger.errors)


# ---------------------------------------------------------------------------
#  ۲۳) گیت پیش از روتر دستورها اجرا می‌شود
# ---------------------------------------------------------------------------
def test_23_gate_before_command_router():
    print("\n### ۲۳) گیت پیش از روتر دستورها")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        # متخلف دستور «متخلف» روی کاربرِ دیگر می‌زند
        event = FakeEvent(CHAT_ID, bot.client.users[TARGET_ID],
                          text="متخلف", message_id=9600, reply_to_id=9601)
        gated = await violators.gate_message(bot, event, CHAT_ID, TARGET_ID,
                                             event.sender, 9600,
                                             logger=bot.logger)
        return gated, event

    gated, event = run(scenario())
    check("دستورِ متخلف پیش از روتر حذف شد", 9600 in bot.chat.deleted,
          bot.chat.deleted)
    check("پردازش متوقف شد", gated is True)
    check("دستور پردازش نشد (پاسخی داده نشد)", not event.replies,
          event.replies)
    check("کاربرِ دیگر متخلف نشد",
          violators.get_record(CHAT_ID, NATIVE_ADMIN_ID) is None)


# ---------------------------------------------------------------------------
#  ۲۴) «لغو متخلف @یوزرنیم» — بدون ریپلای
# ---------------------------------------------------------------------------
def test_24_unmark_by_username():
    print("\n### ۲۴) لغو متخلف با یوزرنیم")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        # اول با ریپلای ثبت می‌کنیم
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        deleted_before = list(bot.chat.deleted)
        # بعد فقط با یوزرنیم لغو می‌کنیم (بدون ریپلای)
        event = FakeEvent(CHAT_ID, owner, text="لغو متخلف @target_user",
                          message_id=9400)
        await violator_commands.handle(
            bot, event, CHAT_ID, OWNER_ID, owner, "لغو متخلف @target_user",
            bot.logger)
        return event, deleted_before

    event, deleted_before = run(scenario())
    check("رکورد حذف شد", violators.get_record(CHAT_ID, TARGET_ID) is None)
    check("پاسخ «لغو شد» داده شد",
          any("از لیست متخلف گروه" in text for text, _ in event.replies),
          event.replies)
    check("پاسخ عنوان نقل‌قول‌دار دارد",
          any(any(type(e).__name__ == "MessageEntityBlockquote"
                  for e in entities)
              for _text, entities in event.replies), event.replies)
    check("resolve یوزرنیم زده شد", "get_entity" in bot.client.calls,
          bot.client.calls)
    check("هیچ پیامِ تازه‌ای حذف نشد (ریپلایی نبود)",
          bot.chat.deleted == deleted_before, bot.chat.deleted)


# ---------------------------------------------------------------------------
#  ۲۵) «لغو متخلف @یوزرنیم» — یوزرنیمِ ناشناخته
# ---------------------------------------------------------------------------
def test_25_unmark_by_unknown_username():
    print("\n### ۲۵) لغو متخلف با یوزرنیمِ ناشناخته")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]
    bot.client.entity_lookup_works = False
    event = FakeEvent(CHAT_ID, owner, text="لغو متخلف @ghost_user",
                      message_id=9410)

    async def scenario():
        return await violator_commands.handle(
            bot, event, CHAT_ID, OWNER_ID, owner, "لغو متخلف @ghost_user",
            bot.logger)

    run(scenario())
    check("پیام «یوزرنیم پیدا نشد» داده شد",
          any("یوزرنیم پیدا نشد" in text for text, _ in event.replies),
          event.replies)
    check("هیچ رکوردی تغییر نکرد",
          violators.get_record(CHAT_ID, TARGET_ID) is None)


# ---------------------------------------------------------------------------
#  ۲۶) «لغو متخلف @یوزرنیم» برای کاربرِ غیرمتخلف
# ---------------------------------------------------------------------------
def test_26_unmark_by_username_not_violator():
    print("\n### ۲۶) لغو متخلف با یوزرنیمِ کاربرِ غیرمتخلف")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]
    event = FakeEvent(CHAT_ID, owner, text="لغو متخلف @reg_admin",
                      message_id=9420)

    async def scenario():
        return await violator_commands.handle(
            bot, event, CHAT_ID, OWNER_ID, owner, "لغو متخلف @reg_admin",
            bot.logger)

    run(scenario())
    check("پیام «متخلف نیست» داده شد",
          any("در لیست متخلف گروه نیست" in text for text, _ in
              event.replies), event.replies)


# ---------------------------------------------------------------------------
#  ۲۷) «متخلف @یوزرنیم» پشتیبانی نمی‌شود — ثبت فقط با ریپلای
# ---------------------------------------------------------------------------
def test_27_mark_by_username_refused():
    print("\n### ۲۷) ثبت با یوزرنیم ⇒ راهنما")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]
    event = FakeEvent(CHAT_ID, owner, text="متخلف @target_user",
                      message_id=9430)

    async def scenario():
        return await violator_commands.handle(
            bot, event, CHAT_ID, OWNER_ID, owner, "متخلف @target_user",
            bot.logger)

    run(scenario())
    check("پیام راهنما داده شد",
          any("ریپلای" in text for text, _ in event.replies), event.replies)
    check("رکوردی ثبت نشد",
          violators.get_record(CHAT_ID, TARGET_ID) is None)


# ---------------------------------------------------------------------------
#  ۲۸) تشخیص دستور با/بدون آرگومان
# ---------------------------------------------------------------------------
def test_28_is_command_variants():
    print("\n### ۲۸) تشخیص دستور با آرگومان")
    check("«لغو متخلف @oajwn»", violator_commands.is_command(
        "لغو متخلف @oajwn") is True)
    check("«لغو متخلف» تنها", violator_commands.is_command(
        "لغو متخلف") is True)
    check("«متخلف» تنها", violator_commands.is_command("متخلف") is True)
    check("«متخلف @x» هم دستور است (راهنما می‌دهد)",
          violator_commands.is_command("متخلف @x") is True)
    check("«لغو متخلف oajwn» بدون @",
          violator_commands.is_command("لغو متخلف oajwn") is True)
    check("«سلام» دستور نیست", violator_commands.is_command("سلام") is False)
    check("«لغو متخلفان» دستور نیست",
          violator_commands.is_command("لغو متخلفان") is False)
    check("«متخلفی» دستور نیست",
          violator_commands.is_command("متخلفی") is False)


# ---------------------------------------------------------------------------
#  ۲۹) یوزرنیم بر ریپلای اولویت دارد
# ---------------------------------------------------------------------------
def test_29_username_priority_over_reply():
    print("\n### ۲۹) اولویت یوزرنیم بر ریپلای")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        # هر دو کاربر را متخلف می‌کنیم
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        await violator_commands.handle(
            bot, reply_event(bot, owner, NATIVE_ADMIN_ID), CHAT_ID,
            OWNER_ID, owner, "متخلف", bot.logger)
        # ریپلای روی ادمینِ native، ولی آرگومانِ یوزرنیم روی target_user
        event = reply_event(bot, owner, NATIVE_ADMIN_ID,
                            text="لغو متخلف @target_user", reply_msg_id=4322)
        return await violator_commands.handle(
            bot, event, CHAT_ID, OWNER_ID, owner,
            "لغو متخلف @target_user", bot.logger)

    run(scenario())
    check("هدفِ یوزرنیم لغو شد",
          violators.get_record(CHAT_ID, TARGET_ID) is None)
    check("هدفِ ریپلای دست‌نخورده ماند",
          violators.get_record(CHAT_ID, NATIVE_ADMIN_ID) is not None)


# ---------------------------------------------------------------------------
#  ۳۰) لغو با یوزرنیم — بدون مجوز
# ---------------------------------------------------------------------------
def test_30_unmark_by_username_no_permission():
    print("\n### ۳۰) لغو با یوزرنیم بدون مجوز")
    bot = fresh()
    owner = bot.client.users[OWNER_ID]

    async def scenario():
        await violator_commands.handle(
            bot, reply_event(bot, owner, TARGET_ID), CHAT_ID, OWNER_ID,
            owner, "متخلف", bot.logger)
        native = bot.client.users[NATIVE_ADMIN_ID]
        event = FakeEvent(CHAT_ID, native, text="لغو متخلف @target_user",
                          message_id=9440)
        await violator_commands.handle(
            bot, event, CHAT_ID, NATIVE_ADMIN_ID, native,
            "لغو متخلف @target_user", bot.logger)
        return event

    event = run(scenario())
    check("ادمینِ ثبت‌نشده رد شد",
          any("فقط مالک یا ادمین ثبت شده" in text for text, _ in
              event.replies), event.replies)
    check("رکورد حذف نشد",
          violators.get_record(CHAT_ID, TARGET_ID) is not None)


# ---------------------------------------------------------------------------
#  اجرا
# ---------------------------------------------------------------------------
def main():
    tests = [
        test_01_mark_success,
        test_02_mark_registered_owner_rejected,
        test_03_mark_creator_rejected,
        test_04_mark_bot_rejected,
        test_05_native_admin_violator_deleted,
        test_06_registered_admin_violator_cannot_unmark_self,
        test_07_edited_message_deleted,
        test_08_media_and_sticker_deleted,
        test_09_restart_keeps_records,
        test_10_bot_without_delete_rights,
        test_11_unmark,
        test_12_other_groups_unaffected,
        test_13_creator_unknown_refuses_mark,
        test_14_templates,
        test_15_admin_list,
        test_16_stats_counter,
        test_17_exclusive_revoked_on_mark,
        test_18_runtime_protection_drops_wrong_record,
        test_19_no_reply_and_permission,
        test_20_anonymous_admin_skipped,
        test_21_batched_delete,
        test_22_already_and_missing,
        test_22b_failed_delete_only_logged,
        test_23_gate_before_command_router,
        test_24_unmark_by_username,
        test_25_unmark_by_unknown_username,
        test_26_unmark_by_username_not_violator,
        test_27_mark_by_username_refused,
        test_28_is_command_variants,
        test_29_username_priority_over_reply,
        test_30_unmark_by_username_no_permission,
    ]
    for test in tests:
        try:
            test()
        except Exception as error:                   # noqa: BLE001
            import traceback
            global FAILED
            FAILED += 1
            FAILURES.append(f"{test.__name__} CRASHED :: {error!r}")
            print(f"  💥 {test.__name__} CRASHED: {error!r}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\nشکست‌ها:")
        for failure in FAILURES:
            print(f"  - {failure}")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
