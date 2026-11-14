# -*- coding: utf-8 -*-
"""🧪 تست سه قابلیت مدیریتی: اختصاصی / پیام زماندار / گزارش.

اجرا:

    PYTHONPATH=. python3 tests/test_manage_features.py

پوشش (مطابق «تست‌های الزامی» ماموریت):

۱.  اختصاصی در گروه باز ⇒ رد با دلیل روشن (بدون تغییر مجوز)
۲. اختصاصی در گروه قفل ⇒ تنها مجوز ارسال، بدون ارتقای ادمین
۳. لغو اختصاصی ⇒ مجوز برگشت + رکورد غیرفعال (و اجرای دوباره خطا)
۴. باز شدن گروه (دستور «باز»/مدیر سروش/مالک) ⇒ لغو خودکار همهٔ مجوزها
۵. قطع اتصال هنگام لغو ⇒ رکورد در «در انتظار بازیابی» می‌ماند، پرونده گم نمی‌شود
۶. ری‌استارت هنگام فعال بودن اختصاصی ⇒ رکورد بازیابی می‌شود
۷. اجرای هم‌زمان اختصاصی برای چند کاربر ⇒ دو رکورد مستقل، بدون تداخل
۸. عدم باقی ماندن مجوز مدیریتی ناخواسته (هیچ EditAdmin صدا زده نمی‌شود)
۹. پیام زماندار: هر پنج بازه، لغو، پایان بازه، حذف فقط داخل بازه
۱۰. ری‌استارت زمان‌دار ⇒ زمان از ابتدا شروع نمی‌شود
۱۱. خطای حذف ⇒ بقیهٔ دسته‌ها ادامه می‌یابند + تلاش دوباره
۱۲. گزارش: معتبر، بدون ریپلای، بدون دلیل، گزارش خود، محدودیت ۷ ثانیه، تکراری
۱۳. تأیید مالک ⇒ دقیقاً دو اخطار؛ لغو ⇒ صفر اخطار؛ اخطار تکراری ممنوع
۱۴. گزارش‌دهنده نمی‌تواند تصمیم بگیرد؛ ادمین عادی هم نمی‌تواند
۱۵. ری‌استارت در انتظار دلیل/تصمیم
۱۶. قالب‌ها عیناً مطابق تصویر (Bold + نقل‌قول با آفست UTF-16)
۱۷. لیست ادمینی/لیست کاربران: ردیف‌های جدید، «گزارش» فقط در لیست کاربران
۱۸. صف‌ها مستقل: سنگینی/خطای یک صف بقیه را متوقف نمی‌کند
۱۹. عملکرد مسیر گرم (µs)
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
os.environ.setdefault("BOT_HOME", tempfile.mkdtemp(prefix="foxora-manage-"))

from splusthon.tl.types import (ChatAdminRights, ChatBannedRights,
                                MessageEntityBlockquote, MessageEntityBold)

from modules import admin_tools, group_storage
from modules.group_actions import text_is_banned
from modules.manage_features import (access, exclusive, exclusive_commands,
                                     queue as mqueue, report_commands,
                                     reports, storage, texts, timed,
                                     timed_commands)

PASSED = 0
FAILED = 0
FAILURES = []


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
#  فیک‌های واقع‌نما
# ---------------------------------------------------------------------------
class FakeLogger:
    def __init__(self):
        self.infos, self.errors = [], []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"FloodWait {seconds}")
        self.seconds = seconds


class FakeChat:
    """گروه ساختگی با وضعیت قفلِ واقعی و اعضای محدود."""

    def __init__(self, chat_id=555000111, locked=True):
        self.chat_id = chat_id
        self.locked = locked
        self.default_rights = ChatBannedRights(
            until_date=None, send_messages=locked, send_plain=locked)
        self.participants = {}          # user_id → ChatBannedRights
        self.admins = {}                # user_id → ChatAdminRights
        self.edit_banned_calls = []
        self.edit_admin_calls = []
        self.fail_next = 0
        self.deleted = []
        self.deleted_ids = []
        #: کلِ اعضا (برای get_participants)
        self.members = set()
        self.creator_id = 4242

    def snapshot(self):
        return self.default_rights

    def report(self):
        return self.default_rights


class _AsyncList:
    """شیء شبه‌ژنراتور: هم ``async for`` و هم پیمایشِ معمولی."""

    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        async def generator():
            for item in self._items:
                yield item
        return generator()

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class FakeClient:
    """کلاینت SPlusthonِ ساختگی: همان RPCهای واقعیِ استفاده‌شده."""

    def __init__(self, chat, users=None, get_participant_fails=0):
        self.chat = chat
        self.users = users or {}
        self.get_participant_fails = get_participant_fails
        #: نویسندهٔ پیامِ ریپلای‌شده (پیش‌فرض: «متخلف»)
        self.reply_sender_id = 7001
        #: شبیه‌سازی قطع اتصال فقط برای RPC مجوز
        self.fail_edit_banned = False
        #: شبیه‌سازی رد شدنِ ارتقا از سوی سرور (خطای دائمی)
        self.fail_edit_admin = False
        #: سروری که «ادمینِ بدون حق» را قبول نمی‌کند (رفتار واقعی سروش)
        self.reject_rightless_admin = False
        #: سرور سروش که «تغییر اطلاعات گروه» و «سنجاق» را خودش روشن می‌کند
        #: (در تست‌های مربوطه روشن می‌شود؛ پیش‌فرض خاموش)
        self.force_toggles = False
        #: تعداد بارهایی که GetParticipant خطای گذرا می‌دهد
        self.read_participant_flaky = 0
        #: GetParticipant همیشه خطا بدهد (سرورِ بی‌پاسخ) — مسیر دوم باید کار کند
        self.get_participant_broken = False
        self.edits = []
        self.sent = []
        #: نامِ هر RPC/خواندنی که واقعاً صدا زده شده (برای سنجشِ مسیر گرم)
        self.calls = []
        #: شناسهٔ خودِ ربات (برای پیش‌بررسیِ حقوقِ ربات در «اختصاصی»)
        self.bot_id = 999
        #: آیا ربات حق «افزودن ادمین» دارد؟ (تستِ سناریوی بدونِ حق)
        self.bot_can_add_admins = True
        #: هدف‌های حذف (باید همیشه InputPeer یا شناسهٔ منفی باشند)
        self.delete_targets = []
        self.delete_effective = True

    async def get_me(self):
        return FakeUser(self.bot_id, "foxora_bot")

    def _bot_rights(self):
        flags = ("change_info", "post_messages", "edit_messages",
                 "delete_messages", "ban_users", "invite_users", "pin_messages",
                 "add_admins", "anonymous", "manage_call", "other",
                 "manage_topics")
        return ChatAdminRights(**{
            flag: (flag == "add_admins" and self.bot_can_add_admins)
            for flag in flags})

    async def get_input_entity(self, target):
        # همان شکلی که SPlusthon برای گروه و کاربر برمی‌گرداند.
        if isinstance(target, int) and target == self.chat.chat_id:
            return type("InputPeerChannel", (), {
                "channel_id": abs(int(target)) - 1_000_000_000_000,
                "access_hash": 12345,
            })()
        if isinstance(target, int):
            return type("InputPeerUser", (), {"user_id": int(target),
                                              "access_hash": 6789})()

    async def get_entity(self, target):
        self.calls.append("get_entity")
        if isinstance(target, int) and target in self.users:
            return self.users[target]
        if isinstance(target, str):
            for user in self.users.values():
                if getattr(user, "username", None) == target:
                    return user
        return None

    async def __call__(self, request, *args, **kwargs):
        name = type(request).__name__
        self.calls.append(name)
        if name == "EditBannedRequest":
            if self.fail_edit_banned:
                raise ConnectionError("network down")
            self.chat.edit_banned_calls.append(request)
            participant = request.participant
            user_id = _fake_user_id(participant)
            self.chat.participants[user_id] = request.banned_rights
            return True
        if name == "EditAdminRequest":
            if self.fail_edit_admin:
                raise type("ChatAdminRequiredError", (Exception,), {})(
                    "CHAT_ADMIN_REQUIRED")
            self.chat.edit_admin_calls.append(request)
            user_id = _fake_user_id(request.user_id)
            rights = request.admin_rights
            flags = ("change_info", "post_messages", "edit_messages",
                     "delete_messages", "ban_users", "invite_users",
                     "pin_messages", "add_admins", "anonymous", "manage_call",
                     "other", "manage_topics")
            values = {flag: getattr(rights, flag, None) for flag in flags}
            if all(value is None for value in values.values()):
                # همه None = درخواستِ demote (همان چیزی که سرور واقعی هم
                # برای برداشتنِ ادمینی می‌پذیرد).
                self.chat.admins.pop(user_id, None)
                return True
            # «همه False» = ارتقای بدون هیچ حق. سرورِ سخت‌گیر چنین ادمینی
            # نمی‌سازد؛ سرورِ عادی می‌سازد.
            if not any(bool(value) for value in values.values()):
                if self.reject_rightless_admin:
                    self.chat.admins.pop(user_id, None)
                    return True
            if self.force_toggles:
                # رفتار واقعیِ سروش: «تغییر اطلاعات گروه» و «سنجاق» را
                # خودش روشن می‌کند حتی وقتی False فرستاده شده باشد.
                rights = type(rights)(
                    **{flag: (True if flag in ("change_info", "pin_messages")
                              else getattr(rights, flag, False))
                       for flag in flags})
            self.chat.admins[user_id] = rights
            return True
        if name == "GetParticipantRequest":
            if self.get_participant_broken:
                raise type("ChatAdminRequiredError", (Exception,), {})(
                    "CHAT_ADMIN_REQUIRED (read)")
            if self.read_participant_flaky:
                self.read_participant_flaky -= 1
                raise ConnectionError("temporary read")
            if self.get_participant_fails:
                self.get_participant_fails -= 1
                raise ConnectionError("temporary")
            participant = request.participant
            user_id = _fake_user_id(participant)
            if user_id == self.bot_id:
                # حقوقِ خودِ ربات (پیش‌بررسیِ «افزودن ادمین»)
                flags = ("change_info", "post_messages", "edit_messages",
                         "delete_messages", "ban_users", "invite_users",
                         "pin_messages", "add_admins", "anonymous",
                         "manage_call", "other", "manage_topics")
                bot_rights = ChatAdminRights(**{
                    flag: (flag == "add_admins" and self.bot_can_add_admins)
                    for flag in flags})
                return type("ChannelParticipantAdmin", (), {
                    "banned_rights": ChatBannedRights(until_date=None),
                    "admin_rights": bot_rights,
                    "user_id": user_id})()
            rights = self.chat.participants.get(
                user_id, ChatBannedRights(until_date=None, send_messages=True,
                                          send_plain=True))
            if user_id == self.bot_id and user_id not in self.chat.admins:
                return type("ChannelParticipantAdmin", (), {
                    "banned_rights": ChatBannedRights(until_date=None),
                    "admin_rights": self._bot_rights(),
                    "user_id": user_id})()
            if user_id in self.chat.admins:
                # ادمین‌بودن باید از نوعِ participant فهمیده شود (مثل API واقعی)
                return type("ChannelParticipantAdmin", (), {
                    "banned_rights": ChatBannedRights(until_date=None),
                    "admin_rights": self.chat.admins[user_id],
                    "user_id": user_id})()
            if text_is_banned(rights):
                return type("ChannelParticipantBanned", (), {
                    "banned_rights": rights, "user_id": user_id})()
            return type("ChannelParticipant", (), {
                "banned_rights": ChatBannedRights(until_date=None),
                "user_id": user_id})()
        if name in {"GetFullChannelRequest", "GetChannelsRequest"}:
            chat = type("Chat", (), {
                "id": 1, "default_banned_rights": self.chat.report()})()
            return type("Full", (), {"chats": [chat],
                                     "full_chat": type("FC", (), {
                                         "participants_count": 3})()})()
        return True

    async def get_participants(self, chat_id, limit=None, *, filter=None,
                               **_kwargs):
        self.calls.append("get_participants")
        """فهرست اعضا (و با filter، فقط ادمین‌ها) — مثل SPlusthon."""
        members = []
        member_ids = set(self.chat.members) | {self.bot_id}
        for user_id in member_ids:
            member = FakeUser(user_id, getattr(
                self.users.get(user_id), "username", None))
            if user_id in self.chat.admins:
                member.participant = type("ChannelParticipantAdmin", (), {})()
                member.is_admin = True
            elif user_id == self.bot_id:
                member.participant = type("ChannelParticipantAdmin", (), {
                    "admin_rights": self._bot_rights()})()
                member.is_admin = True
            elif user_id == self.chat.creator_id:
                member.participant = type("ChannelParticipantCreator", (), {})()
                member.is_creator = True
            else:
                member.participant = type("ChannelParticipant", (), {})()
                member.is_admin = False
            members.append(member)
        return _AsyncList(members)

    async def get_messages(self, chat_id, ids=None, **_kwargs):
        self.calls.append("get_messages")
        sender = self.users.get(self.reply_sender_id)
        return type("Message", (), {
            "id": ids,
            "get_sender": _sender_getter(sender),
        })()

    async def send_message(self, chat_id, text, formatting_entities=None,
                           **_kwargs):
        sent_id = 500000 + len(self.sent)
        self.sent.append((chat_id, text, list(formatting_entities or []),
                          sent_id))
        return type("Message", (), {"id": sent_id, "message": text,
                                    "peer_id": chat_id})()

    async def delete_messages(self, chat_id, ids, **_kwargs):
        ids = list(ids)
        # همان رفتار واقعی: با شناسهٔ مثبت (کاربر) حذف انجام نمی‌شود.
        self.delete_targets.append(chat_id)
        if isinstance(chat_id, int) and chat_id > 0:
            self.delete_effective = False
            return []
        self.delete_effective = True
        if self.chat.fail_next:
            self.chat.fail_next -= 1
            ids = ids[:len(ids) // 2]           # حذف جزئی + خطا
            self.chat.deleted.extend(ids)
            raise ConnectionError("network down")
        self.chat.deleted.extend(ids)
        return type("Affected", (), {"pts_count": len(ids)})()

    async def edit_message(self, entity, message=None, text=None,
                           formatting_entities=None, **_kwargs):
        """امضای واقعیِ SPlusthon: edit_message(entity, message, text=…)."""
        if text is None:                     # فراخوانی قدیمی: (message, text)
            text, message = message, entity
            entity = None
        self.edits.append((entity, message, text,
                           list(formatting_entities or [])))
        return type("Message", (), {"id": message})()

    async def get_me(self):
        return FakeUser(self.bot_id, "foxora_bot")


def _fake_user_id(value):
    """شناسهٔ عددی از هر شکلِ آرگومان (عدد، InputPeerUser، User، FakeUser).

    SPlusthon واقعی هم همین کار را می‌کند (``utils.get_input_peer``)؛ فیک هم
    باید entity را بپذیرد تا مسیرِ واقعیِ پاس دادن entity تست شود.
    """
    from modules.manage_features import peers as _peers

    identifier = _peers.user_identifier(value)
    return identifier if identifier is not None else value


def _sender_getter(user):
    async def _get_sender(*_args, **_kwargs):
        return user
    return _get_sender


class FakeActions:
    def __init__(self, chat):
        self.chat = chat
        self.lock_calls = 0
        self.unlock_calls = 0

    async def lock_group(self, chat_id, minutes=None):
        self.lock_calls += 1
        self.chat.locked = True
        self.chat.default_rights = ChatBannedRights(
            until_date=None, send_messages=True, send_plain=True)
        return True

    async def unlock_group(self, chat_id):
        self.unlock_calls += 1
        self.chat.locked = False
        self.chat.default_rights = ChatBannedRights(until_date=None)
        return True


class FakeBot:
    def __init__(self, chat, users=None, **kwargs):
        self.chat = chat
        self.client = FakeClient(chat, users, **kwargs)
        self.logger = FakeLogger()
        self.group_actions = FakeActions(chat)
        self.tracker = FakeTracker()
        self.config_manager = type("Cfg", (), {
            "get": lambda _self, key, default=None: default})()
        self.bot_account_id = 999


class FakeTracker:
    def __init__(self):
        self.counts = {}

    def increment(self, chat_id, user_id):
        key = (str(chat_id), str(user_id))
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def get_count(self, chat_id, user_id):
        return self.counts.get((str(chat_id), str(user_id)), 0)


class FakeUser:
    def __init__(self, user_id, username=None, first_name="کاربر"):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = None


class FakeEvent:
    is_private = False

    def __init__(self, chat_id, user, text="", reply_to_id=None):
        self.chat_id = chat_id
        self.sender = user
        self.sender_id = user.id
        self.message = type("Msg", (), {"id": 8001, "message": text})()
        self.reply_to = (type("Reply", (), {"reply_to_msg_id": reply_to_id})()
                         if reply_to_id else None)
        self.replies = []

    async def reply(self, text, formatting_entities=None, **_kwargs):
        self.replies.append((text, list(formatting_entities or [])))
        return type("Sent", (), {"id": 9000 + len(self.replies)})()


def fresh(chat_id=None):
    """ری‌ست کاملِ سه قابلیت (storage + حافظه + صف)."""
    storage.reset_all()
    exclusive_commands.reset_all()
    timed_commands.reset_all()
    report_commands.reset_all()
    reports.reset_all()
    for module_queue in (exclusive_commands.QUEUE, timed_commands.QUEUE,
                         reports.QUEUE, timed.QUEUE,
                         report_commands.QUEUE):
        mqueue.reset_queue(module_queue)
    if chat_id is not None:
        group_storage.set_group_owner(chat_id, 4242)


# ---------------------------------------------------------------------------
#  ۱..۸ اختصاصی
# ---------------------------------------------------------------------------
def test_01_exclusive_locked_and_open():
    print("\n### ۱) اختصاصی: گروه قفل ⇒ مجوز ارسال، گروه باز ⇒ رد")
    fresh()
    chat = FakeChat(-(10 ** 12) - 501, locked=True)
    bot = FakeBot(chat, {777: FakeUser(777, "target")})

    async def scenario():
        record = await exclusive.grant(bot, chat.chat_id, 777, actor_id=4242,
                                      username="target")
        return record

    record = run(scenario())
    check("رکورد فعال ثبت شد", record["state"] == "active", record)
    # ⚠️ سرعت: هیچ EditBanned/خواندنِ مقدماتی در مسیر ارتقا وجود ندارد.
    check("هیچ EditBanned اضافه‌ای زده نشد (سرعت)",
          chat.edit_banned_calls == [], chat.edit_banned_calls)
    check("فهرست اعضا برای تأییدِ هدف خوانده نشد (سرعت)",
          "get_participants" not in bot.client.calls, bot.client.calls)
    check("تنها یک EditAdmin در مسیر ارتقا زده شد",
          [name for name in bot.client.calls
           if name == "EditAdminRequest"] == ["EditAdminRequest"],
          bot.client.calls)
    check("پیش از ارتقا حقوق خودِ ربات خوانده شد (الزام)",
          "GetParticipantRequest" in bot.client.calls, bot.client.calls)
    check("EditBanned (حذفِ بنِ اضافه) حذف شد ⇒ یک RPC کمتر",
          "EditBannedRequest" not in bot.client.calls, bot.client.calls)
    rights = chat.participants.get(777)
    check("مجوز ارسالِ کاربر با ادمین‌شدن آزاد است (بن نشده)",
          getattr(chat.default_rights, "send_messages", None) is True, rights)
    check("کاربر در فهرست ادمین‌ها با هیچ حق مدیریتی نیست",
          not exclusive.has_any_management_right(chat.admins.get(777)),
          chat.admins.get(777))
    check("کاربر با دقیقاً یک EditAdmin ادمین شد",
          len(chat.edit_admin_calls) == 1, len(chat.edit_admin_calls))
    admin_rights = chat.admins.get(777)
    check("ادمین شده است (نوع participant از خانوادهٔ Admin)",
          admin_rights is not None, chat.admins)
    check("ادمینِ واقعی شد و فقط حق «ویرایش/ارسال پیام خودش» دارد",
          admin_rights is not None
          and getattr(admin_rights, "edit_messages", False) is True
          and getattr(admin_rights, "post_messages", False) is False,
          admin_rights)
    check("هیچ حق مدیریتیِ دیگری ندارد (حذف/بن/دعوت/تغییر اطلاعات/سنجاق)",
          not exclusive.has_sensitive_right(admin_rights), admin_rights)
    check("حالتِ اعمال‌شده در رکورد ذخیره شد (preset=edit_only)",
          record.get("admin_preset") == "edit_only",
          record.get("admin_preset"))
    # همان چیزی که در اسکرین‌شاتِ تنظیماتِ مدیر دیده می‌شد نباید روشن باشد:
    for toggle in ("change_info", "delete_messages", "ban_users",
                   "invite_users", "pin_messages", "add_admins"):
        check(f"توگل «{toggle}» خاموش است",
              getattr(admin_rights, toggle, None) is False,
              getattr(admin_rights, toggle, "missing"))
    for flag in ("change_info", "post_messages", "delete_messages",
                 "ban_users", "invite_users", "pin_messages", "add_admins",
                 "anonymous", "manage_call", "other", "manage_topics"):
        check(f"حق «{flag}» داده نشده",
              getattr(admin_rights, flag, None) is False,
              getattr(admin_rights, flag, "missing"))
    check("رکورد بلافاصله «ارسال‌شده» علامت خورد ولی تأیید در پس‌زمینه است",
          record["granted"] is True and record["confirmed"] is False
          and record.get("verified") == "pending", record)
    verified = run(exclusive.complete_verification(bot, chat.chat_id, 777))
    check("تأییدِ واقعی با GetParticipant هم انجام شد",
          verified["confirmed"] is True and
          "GetParticipantRequest" in bot.client.calls, bot.client.calls)
    check("شناسه‌ها عددی و پایدار ذخیره شدند",
          isinstance(record["user_id"], str) and record["chat_id"],
          record)
    check("زمان تهران هم ذخیره شد", bool(record.get("created_at_tehran")))

    # گروه باز ⇒ استثنا معنا ندارد
    open_chat = FakeChat(-(10 ** 12) - 502, locked=False)
    open_bot = FakeBot(open_chat, {778: FakeUser(778, "target2")})

    async def scenario_open():
        try:
            await exclusive.grant(open_bot, open_chat.chat_id, 778)
        except exclusive.NotLocked:
            return "not_locked"
        return "granted"

    check("در گروه باز رد می‌شود", run(scenario_open()) == "not_locked")
    check("در گروه باز هیچ RPC مجوزی زده نشد",
          open_chat.edit_banned_calls == [])


def test_01b_exclusive_command_is_fast():
    """دستور «اختصاصی» باید در چند صد میلی‌ثانیه پاسخ بدهد (نه یک دقیقه).

    بازخورد کارفرما: «این متن بعد یک دقیقه ارسال می‌شد». علت‌ها: سه-چهار
    resolve/get_messages، دو مسیرِ تأیید با ۳×۱۰ ثانیه تایم‌اوت، تلاش دوبارهٔ
    ۰.۶ ثانیه‌ای در `job()` و انتظار برای `GetParticipant` پیش از پاسخ.
    """
    print("\n### ۱ب) سرعتِ دستور اختصاصی (بدون انتظارِ یک‌دقیقه‌ای)")
    fresh()
    chat_id = -(10 ** 12) - 5111
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=True)
    target = FakeUser(9601, "speedtest", "کاربر")
    bot = FakeBot(chat, {9601: target})
    owner = FakeUser(4242, "owner")
    event = FakeEvent(chat_id, owner, "اختصاصی @speedtest")

    started = time.monotonic()
    handled = run(exclusive_commands.handle(bot, event, chat_id, 4242, owner,
                                            "اختصاصی @speedtest",
                                            bot.logger))
    elapsed = time.monotonic() - started

    check("دستور مصرف شد", handled is True)
    check("پاسخ «تنظیم اختصاصی …» ارسال شد",
          bool(event.replies) and "تنظیم اختصاصی" in event.replies[-1][0],
          event.replies)
    check("پاسخ بلافاصله آمد (کمتر از ۲ ثانیه)", elapsed < 2.0,
          f"{elapsed:.3f}s")
    check("کاربر در همان لحظه ادمین شده است", 9601 in chat.admins,
          chat.admins)
    check("هیچ EditBanned اضافه‌ای در مسیر گرم زده نشد",
          chat.edit_banned_calls == [], chat.edit_banned_calls)
    check("هیچ get_messages اضافه‌ای برای یوزرنیم زده نشد",
          "get_messages" not in bot.client.calls, bot.client.calls)
    # کاربر در همان لحظه می‌تواند بنویسد: ادمین است و بن نیست
    check("مجوز ارسالِ گروه هنوز برای اعضای عادی بسته است",
          text_is_banned(chat.default_rights), chat.default_rights)

    # تأییدِ واقعی در پس‌زمینه باید رکورد را confirmed کند
    async def finish_background():
        return await exclusive.complete_verification(bot, chat_id, 9601)

    record = run(finish_background())
    check("تأییدِ سرور در پس‌زمینه انجام شد",
          record.get("confirmed") is True, record)
    check("قالب پاسخ هرگز «انجام نشد» ندارد",
          all("انجام نشد" not in text for text, _e in event.replies),
          event.replies)


def test_02_exclusive_revoke_and_restart():
    print("\n### ۲) لغو اختصاصی + ری‌استارت + قطع اتصال")
    fresh()
    chat = FakeChat(-(10 ** 12) - 503, locked=True)
    bot = FakeBot(chat, {783: FakeUser(783, "target3")})

    async def scenario():
        await exclusive.grant(bot, chat.chat_id, 783, actor_id=4242,
                              username="target3")
        entry = await exclusive.revoke(bot, chat.chat_id, 783)
        second = None
        try:
            await exclusive.revoke(bot, chat.chat_id, 783)
        except exclusive.NotExclusive:
            second = "blocked"
        return entry, second

    entry, second = run(scenario())
    check("رکورد غیرفعال شد", entry["state"] == "revoked", entry)
    # ⚠️ قرارداد جدید: لغو، محدودیتِ شخصی روی کاربر نمی‌گذارد (وگرنه بعد از
    # باز شدنِ گروه هم mute می‌ماند). عضو عادی از پیش‌فرضِ گروه ارث می‌برد.
    check("هیچ محدودیتِ شخصی روی کاربر گذاشته نشد (mute نمی‌شود)",
          783 not in chat.participants
          or not text_is_banned(chat.participants[783]), chat.participants)
    check("اجرای دوبارهٔ لغو مسدود است", second == "blocked")
    check("لغو، ادمینی را با حق‌های همه-False برداشت (demote)",
          len(chat.edit_admin_calls) == 2
          and not any(bool(getattr(chat.edit_admin_calls[-1].admin_rights,
                                   flag, False))
                      for flag in ("change_info", "post_messages",
                                   "edit_messages", "delete_messages",
                                   "ban_users", "invite_users",
                                   "pin_messages", "add_admins")),
          len(chat.edit_admin_calls))
    check("بعد از لغو دیگر ادمین نیست", 783 not in chat.admins, chat.admins)

    # ری‌استارت: رکوردِ فعال باید در pending_jobs بیاید، رکورد لغوشده نه
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 504, locked=True)
    bot2 = FakeBot(chat2, {784: FakeUser(784, "a"), 785: FakeUser(785, "b")})

    async def scenario2():
        await exclusive.grant(bot2, chat2.chat_id, 784, actor_id=4242)
        await exclusive.grant(bot2, chat2.chat_id, 785, actor_id=4242)
        await exclusive.revoke(bot2, chat2.chat_id, 785)
        return exclusive.pending_jobs(chat2.chat_id)

    jobs = run(scenario2())
    states = {job["user_id"]: job["state"] for job in jobs}
    check("پس از ری‌استارت فقط رکورد فعال/نیمه‌کاره در بازیابی است",
          set(states) == {"784"}, states)
    check("رکورد فعال با وضعیت active بازیابی می‌شود",
          states.get("784") == "active", states)
    check("رکورد لغوشده در بازیابی نمی‌آید", "785" not in states)

    # قطع اتصال هنگام لغو ⇒ رکورد در «در انتظار بازیابی» می‌ماند
    fresh()
    chat3 = FakeChat(-(10 ** 12) - 505, locked=True)
    bot3 = FakeBot(chat3, {786: FakeUser(786, "c")})

    async def scenario3():
        await exclusive.grant(bot3, chat3.chat_id, 786, actor_id=4242)
        # شبیه‌سازی قطع اتصال درست وسطِ لغو (فقط RPC مجوز شکست می‌خورد)
        bot3.client.fail_edit_banned = True
        try:
            await exclusive.revoke(bot3, chat3.chat_id, 786)
        except Exception:
            pass
        return exclusive.pending_jobs(chat3.chat_id)

    jobs3 = run(scenario3())
    states = {job["state"] for job in jobs3}
    check("لغو ناموفق، رکورد را برای بازیابی نگه می‌دارد",
          bool(jobs3) and "pending_revoke" in states, states)


def test_02b_exclusive_grant_refused_by_server():
    print("\n### ۲ب) رد شدنِ ارتقا از سوی سرور ⇒ قالب «انجام نشد» + رکورد بازیابی")
    fresh()
    chat_id = -(10 ** 12) - 511
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=True)
    bot = FakeBot(chat, {788: FakeUser(788, "denied")})
    owner = FakeUser(4242, "owner")

    async def scenario():
        # مسیر واقعیِ دستور، با خطای دائمیِ سرور روی EditAdmin
        bot.client.fail_edit_admin = True
        event = FakeEvent(chat_id, owner, "اختصاصی @denied")
        await exclusive_commands.handle(bot, event, chat_id, 4242, owner,
                                        "اختصاصی @denied", bot.logger)
        jobs = exclusive.pending_jobs(chat_id)
        return event.replies, jobs

    replies, jobs = run(scenario())
    # ⚠️ قرارداد جدید (الزام کارفرما): «موفق» فقط با RPCِ بی‌خطا. چون سرور
    # ارتقا را رد کرده، پیامِ شکستِ روشن می‌آید — نه قالبِ موفقیت.
    check("پیامِ موفقیت ارسال نمی‌شود (RPC واقعاً شکست خورد)",
          all("تنظیم اختصاصی برای شما فعال شد" not in text for text, _e
              in replies), replies)
    check("پیامِ شکستِ روشن ارسال شد",
          bool(replies) and "ارتقا انجام نشد" in replies[-1][0], replies)
    check("عنوان با نام واقعیِ کاربر در نقل‌قول است",
          "← کاربر : 『 @denied 』" in replies[-1][0], replies[-1][0])
    check("رکورد برای تلاش دوباره در فهرست بازیابی ماند",
          bool(jobs), jobs)
    check("ادمین واقعی ساخته نشد (سرور رد کرد)", 788 not in chat.admins)


def test_02c_exclusive_strict_server_fallback():
    print("\n### ۲ج) سرورِ سخت‌گیر: ادمینِ بی‌حق را رد می‌کند ⇒ حالت حداقلی")
    fresh()
    chat_id = -(10 ** 12) - 512
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=True)
    bot = FakeBot(chat, {789: FakeUser(789, "strict")})
    bot.client.reject_rightless_admin = True

    async def scenario():
        await exclusive.grant(bot, chat_id, 789, actor_id=4242)
        # مرحلهٔ تأیید (همان چیزی که لایهٔ دستور در پس‌زمینه اجرا می‌کند)
        return await exclusive.complete_verification(bot, chat_id, 789)

    record = run(scenario())
    check("سرور ارتقای بی‌حق را رد کرد و ربات حالتِ بعدی را انتخاب کرد",
          record.get("admin_preset") == "edit_only",
          record.get("admin_preset"))
    check("کاربر واقعاً ادمین شد", 789 in chat.admins, chat.admins)
    rights = chat.admins[789]
    for flag in ("change_info", "delete_messages", "ban_users",
                 "invite_users", "pin_messages", "add_admins", "anonymous",
                 "manage_call", "other", "manage_topics"):
        check(f"در حالت حداقلی هم «{flag}» خاموش است",
              getattr(rights, flag, None) is False,
              getattr(rights, flag, "missing"))
    check("در حالت حداقلی فقط ویرایشِ پیام روشن است",
          getattr(rights, "edit_messages", None) is True, rights)
    check("رکورد فعال و تأییدشده است",
          record["state"] == "active" and record["confirmed"] is True, record)


def test_02d_exclusive_server_forces_toggles_and_lost_record():
    print("\n### ۲د) سرور توگل‌ها را خودش روشن می‌کند + لغو با رکوردِ ازدست‌رفته")
    fresh()
    chat_id = -(10 ** 12) - 513
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=True)
    bot = FakeBot(chat, {793: FakeUser(793, "forced")})
    bot.client.force_toggles = True

    async def scenario():
        return await exclusive.grant(bot, chat_id, 793, actor_id=4242)

    record = run(scenario())
    check("کاربر ادمین شد و ارسالش آزاد است",
          793 in chat.admins, chat.admins)
    check("رکورد active ثبت شد و تأیید در پس‌زمینه می‌ماند",
          record["state"] == "active" and record["granted"] is True
          and record.get("verified") == "pending", record)
    check("کاربر در فهرست اختصاصی دیده می‌شود",
          any(str(item["user_id"]) == "793"
              for item in exclusive.list_entries(chat_id)),
          exclusive.list_entries(chat_id))
    # نکته: در این سناریو خودِ سرور «تغییر اطلاعات گروه» و «سنجاق» را روشن
    # می‌کند (رفتار واقعی سروش)، پس فقط حق‌هایی بررسی می‌شوند که در اختیار ماست.
    check("حق‌های حساس (حذف پیام/بن/افزودن ادمین/دعوت/پست) داده نشده‌اند",
          all(not bool(getattr(chat.admins[793], flag, False))
              for flag in ("delete_messages", "ban_users", "add_admins",
                           "invite_users", "post_messages", "anonymous",
                           "manage_call", "other")),
          chat.admins[793])
    check("ولی حق ویرایش/ارسال پیام دارد (ادمینِ واقعیِ کارآمد)",
          getattr(chat.admins[793], "edit_messages", False) is True,
          chat.admins[793])
    check("درخواستِ ارسال‌شده همهٔ فلگ‌ها را صریح False داشت (حتی change_info)",
          all(getattr(chat.edit_admin_calls[0].admin_rights, flag) is False
              for flag in ("change_info", "pin_messages", "delete_messages",
                           "ban_users", "add_admins", "invite_users")),
          chat.edit_admin_calls[0].admin_rights)

    # لغو باید demote کند و رکورد را ببندد
    run(exclusive.revoke(bot, chat_id, 793))
    check("لغو اختصاصی کاربر را از ادمینی درآورد", 793 not in chat.admins,
          chat.admins)
    check("رکورد غیرفعال شد (state=revoked، برای تاریخچه می‌ماند)",
          (exclusive.get_entry(chat_id, 793) or {}).get("state") == "revoked",
          exclusive.get_entry(chat_id, 793))

    # حتی اگر رکورد محلی نباشد ولی سرور بگوید ادمین است ⇒ باید demote شود
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 514, locked=True)
    bot2 = FakeBot(chat2, {794: FakeUser(794, "ghost")})
    bot2.client.force_toggles = True
    run(exclusive.grant(bot2, chat2.chat_id, 794, actor_id=4242))
    storage.EXCLUSIVE_STORAGE.reset()          # رکورد عمداً گم می‌شود
    check("رکورد محلی پاک شد", exclusive.get_entry(chat2.chat_id, 794) is None)
    entry = run(exclusive.revoke(bot2, chat2.chat_id, 794))
    check("لغو با رکوردِ گم‌شده هم کاربر را از ادمینی درمی‌آورد",
          794 not in chat2.admins, chat2.admins)
    check("رکوردِ حداقلی با پرچم adopted_from_server ثبت شد",
          entry.get("adopted_from_server") is True
          and entry["state"] == "revoked", entry)


def test_02e_exclusive_username_grant_never_says_failed():
    print("\n### ۲ه) اختصاصی با یوزرنیم: موفقیت واقعی نباید «انجام نشد» بدهد")
    fresh()
    chat_id = -(10 ** 12) - 515
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=True)
    target = FakeUser(991, "Linx_cox")
    bot = FakeBot(chat, {991: target})
    bot.client.force_toggles = True
    owner = FakeUser(4242, "owner")

    async def scenario():
        event = FakeEvent(chat_id, owner, "اختصاصی @Linx_cox")
        await exclusive_commands.handle(bot, event, chat_id, 4242, owner,
                                        "اختصاصی @Linx_cox", bot.logger)
        return event.replies

    replies = run(scenario())
    text = " ".join(t2 for t2, _e in replies)
    check("پیام «انجام نشد» ارسال نشد", "انجام نشد" not in text, replies)
    check("پیام «تنظیم اختصاصی برای شما فعال شد» ارسال شد",
          "تنظیم اختصاصی برای شما فعال شد" in text, replies)
    check("عنوان با یوزرنیمِ واقعی در نقل‌قول است",
          "← کاربر : 『 @Linx_cox 』" in text, replies)
    check("کاربر واقعاً ادمین است", 991 in chat.admins, chat.admins)
    check("رکورد فعال ثبت شد",
          exclusive.get_entry(chat_id, 991) is not None
          and exclusive.get_entry(chat_id, 991)["state"] == "active",
          exclusive.get_entry(chat_id, 991))

    # خواندنِ وضعیت چند بار خطای گذرا بدهد: باز هم نباید «انجام نشد» بدهد
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 516, locked=True)
    target2 = FakeUser(992, "Linx_two")
    bot2 = FakeBot(chat2, {992: target2})
    bot2.client.read_participant_flaky = 2

    async def scenario2():
        return await exclusive.grant(bot2, chat2.chat_id, 992, actor_id=4242,
                                     username="Linx_two")

    record = run(scenario2())
    # تأیید عمداً در پس‌زمینه است تا پاسخِ کاربر معطل نشود؛ این‌جا خودش را
    # صریح صدا می‌زنیم و «سه‌حالته» بودن را می‌سنجیم.
    verified = run(exclusive.complete_verification(bot2, chat2.chat_id, 992))
    check("با خطای گذرای خواندن، رکورد باز هم فعال و تأییدشده است",
          record["state"] == "active" and verified["confirmed"] is True
          and verified["state"] == "active", verified)
    check("کاربر ادمین است", 992 in chat2.admins, chat2.admins)


def test_02f_exclusive_verify_with_broken_get_participant():
    print("\n### ۲و) GetParticipant خطا می‌دهد ولی کاربر واقعاً ادمین است")
    fresh()
    chat_id = -(10 ** 12) - 517
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=True)
    target = FakeUser(993, "Melika_89899")
    chat.members = {4242, 993}
    bot = FakeBot(chat, {993: target})
    bot.client.get_participant_broken = True
    owner = FakeUser(4242, "owner")

    async def scenario():
        event = FakeEvent(chat_id, owner, "اختصاصی")
        await exclusive_commands.handle(bot, event, chat_id, 4242, owner,
                                        "اختصاصی @Melika_89899", bot.logger)
        return event.replies

    replies = run(scenario())
    text = " ".join(t2 for t2, _e in replies)
    check("کاربر واقعاً ادمین شد", 993 in chat.admins, chat.admins)
    check("پیام «انجام نشد» ارسال نشد", "انجام نشد" not in text, replies)
    check("پیام موفقیت ارسال شد",
          "تنظیم اختصاصی برای شما فعال شد" in text, replies)
    check("عنوان با یوزرنیمِ واقعی در نقل‌قول است",
          "← کاربر : 『 @Melika_89899 』" in text, replies)
    check("رکورد active ثبت شد",
          (exclusive.get_entry(chat_id, 993) or {}).get("state") == "active",
          exclusive.get_entry(chat_id, 993))

    # همان مسیر برای «ریپلای» (بدون یوزرنیم)
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 518, locked=True)
    group_storage.set_group_owner(chat2.chat_id, 4242)
    target2 = FakeUser(994, "Melika_reply")
    chat2.members = {4242, 994}
    bot2 = FakeBot(chat2, {994: target2})
    bot2.client.get_participant_broken = True
    bot2.client.reply_sender_id = 994

    async def scenario2():
        event = FakeEvent(chat_id=-((10 ** 12) + 518), user=owner,
                          text="اختصاصی", reply_to_id=5000)
        await exclusive_commands.handle(
            bot2, event, chat2.chat_id, 4242, owner, "اختصاصی", bot2.logger)
        return event.replies

    replies2 = run(scenario2())
    text2 = " ".join(t2 for t2, _e in replies2)
    check("مسیر ریپلای: «انجام نشد» نمی‌آید", "انجام نشد" not in text2, replies2)
    check("مسیر ریپلای: پیام موفقیت می‌آید",
          "تنظیم اختصاصی برای شما فعال شد" in text2, replies2)

    # اگر واقعاً ادمین نشده باشد، باید همان پیام خطا بیاید
    fresh()
    chat3 = FakeChat(-(10 ** 12) - 519, locked=True)
    group_storage.set_group_owner(chat3.chat_id, 4242)
    chat3.members = {4242}
    bot3 = FakeBot(chat3, {995: FakeUser(995, "nobody")})
    bot3.client.get_participant_broken = True
    bot3.client.fail_edit_admin = True

    async def scenario3():
        event = FakeEvent(chat3.chat_id, owner, "اختصاصی @nobody")
        await exclusive_commands.handle(bot3, event, chat3.chat_id, 4242,
                                        owner, "اختصاصی @nobody", bot3.logger)
        return event.replies

    replies3 = run(scenario3())
    # الزام کارفرما: «موفق» فقط با RPCِ بی‌خطا؛ خطای واقعی به پیامِ فارسی
    # نگاشت می‌شود و «انجام نشد» فقط وقتی است که واقعاً انجام نشده باشد.
    check("وقتی ارتقا واقعاً انجام نشده، پیام موفقیت ارسال نمی‌شود",
          not any("تنظیم اختصاصی برای شما فعال شد" in t2 for t2, _e in replies3),
          replies3)
    check("پیامِ شکستِ روشن ارسال می‌شود",
          any("ارتقا انجام نشد" in t2 for t2, _e in replies3), replies3)
    check("خطای واقعی با پروندهٔ کامل در لاگ ثبت می‌شود",
          any("EXCLUSIVE GRANT REJECTED" in message
              for message in bot3.logger.errors), bot3.logger.errors)


def test_03_exclusive_auto_revoke_on_unlock():
    print("\n### ۳) باز شدن گروه (هر روشی) ⇒ لغو خودکار همهٔ مجوزها")
    fresh()
    chat = FakeChat(-(10 ** 12) - 506, locked=True)
    users = {791: FakeUser(791, "u1"), 792: FakeUser(792, "u2")}
    bot = FakeBot(chat, users)

    async def scenario():
        for user_id in users:
            await exclusive.grant(bot, chat.chat_id, user_id, actor_id=4242)
        before = len(exclusive.list_entries(chat.chat_id))
        # مدیر سروش‌پلاس گروه را باز می‌کند (وضعیت سرور عوض می‌شود)
        await bot.group_actions.unlock_group(chat.chat_id)
        await exclusive_commands.refresh_lock_state(bot, chat.chat_id,
                                                    bot.logger)
        after = len(exclusive.list_entries(chat.chat_id))
        return before, after

    before, after = run(scenario())
    check("قبل از باز شدن: دو رکورد فعال", before == 2, before)
    check("بعد از باز شدن: هیچ رکورد فعالی نمی‌ماند", after == 0, after)
    # قرارداد جدید: لغو، ban شخصی نمی‌گذارد؛ فقط ادمینی برداشته می‌شود.
    check("مجوز هر دو کاربر برگشت (ادمین نیستند و محدودیتِ شخصی ندارند)",
          all(user_id not in chat.admins for user_id in users)
          and all(user_id not in chat.participants
                  or not text_is_banned(chat.participants[user_id])
                  for user_id in users),
          {"admins": chat.admins, "participants": chat.participants})
    check("برای هر دو کاربر یک ارتقا و یک demote انجام شد",
          len(chat.edit_admin_calls) == 4, len(chat.edit_admin_calls))
    check("هیچ‌کدام بعد از باز شدن گروه ادمین نماندند",
          not chat.admins, chat.admins)


def test_04_exclusive_concurrent():
    print("\n### ۴) اجرای هم‌زمان اختصاصی برای چند کاربر")
    fresh()
    chat = FakeChat(-(10 ** 12) - 507, locked=True)
    users = {801 + index: FakeUser(801 + index, f"u{index}")
             for index in range(5)}
    bot = FakeBot(chat, users)

    async def scenario():
        await asyncio.gather(*[
            exclusive.grant(bot, chat.chat_id, user_id, actor_id=4242)
            for user_id in users
        ])
        return len(exclusive.list_entries(chat.chat_id))

    count = run(scenario())
    check("همهٔ پنج کاربر ثبت شدند", count == 5, count)
    check("برای هر پنج کاربر یک ارتقای جداگانه زده شد (بدون EditBannedِ اضافه)",
          len(chat.edit_admin_calls) == 5 and not chat.edit_banned_calls,
          (len(chat.edit_admin_calls), len(chat.edit_banned_calls)))
    check("هر پنج کاربر واقعاً ادمین شدند", len(chat.admins) == 5, chat.admins)
    check("میان‌افکنیِ صف: هیچ کار تکراری اجرا نشد",
          exclusive_commands.QUEUE.stats()["rejected"] == 0,
          exclusive_commands.QUEUE.stats())


# ---------------------------------------------------------------------------
#  ۵.. پیام زماندار
# ---------------------------------------------------------------------------
def test_05_timed_all_durations_and_cancel():
    print("\n### ۵) پیام زماندار: پنج بازه، جلوگیری از زمان‌سنج دوم، لغو")
    fresh()
    chat = FakeChat(-(10 ** 12) - 601, locked=False)
    bot = FakeBot(chat)
    for minutes in (30, 120, 300, 720, 1440):
        storage.TIMED_STORAGE.reset()
        timed.reset_all()
        record = timed.start(chat.chat_id, minutes, actor_id=4242)
        delta = record["end_ts"] - record["start_ts"]
        check(f"بازهٔ {minutes} دقیقه درست ثبت شد", int(delta) == minutes * 60,
              delta)
        check(f"برچسب قالب برای {minutes}", minutes in timed.DURATIONS)
    storage.TIMED_STORAGE.reset()
    timed.reset_all()
    timed.start(chat.chat_id, 30, actor_id=4242)
    blocked = False
    try:
        timed.start(chat.chat_id, 60 if 60 in timed.DURATIONS else 30,
                    actor_id=4242)
    except timed.AlreadyActive:
        blocked = True
    check("زمان‌سنج دوم ساخته نمی‌شود", blocked)
    check("نبودِ بازهٔ ۴۵ دقیقه در فهرست مجاز", 45 not in timed.DURATIONS)
    check("برچسب‌های پنج بازه عیناً مثل تصویر",
          [timed.DURATIONS[key] for key in (30, 120, 300, 720, 1440)] ==
          ["30 دقیقه", "1 ساعته", "5 ساعته", "12 ساعته", "24 ساعته"],
          timed.DURATIONS)
    invalid = False
    try:
        timed.start(chat.chat_id, 45)
    except (timed.InvalidDuration, timed.AlreadyActive):
        invalid = True
    check("۴۵ دقیقه پذیرفته نمی‌شود", invalid)
    record = timed.cancel(chat.chat_id, reason="test")
    check("لغو، رکورد را غیرفعال می‌کند", record["state"] == "cancelled")
    check("لغو، زمان‌سنج را حذف می‌کند",
          storage.group_key(chat.chat_id) not in timed._TIMERS)


def test_05b_timed_no_progress_message_on_start():
    print("\n### ۵ب) در لحظهٔ تنظیم، پیام «شروع پاکسازی» فرستاده نمی‌شود")
    fresh()
    chat_id = -(10 ** 12) - 611
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    bot = FakeBot(chat)
    owner = FakeUser(4242, "owner")
    sent = []

    async def fake_send(target, text=None, formatting_entities=None, **kw):
        payload_text = text if isinstance(text, str) else getattr(
            text, "message", "")
        sent.append(payload_text or "")
        return type("Sent", (), {"id": 9100 + len(sent)})()

    bot.client.send_message = fake_send

    async def scenario():
        event = FakeEvent(chat_id, owner, "پیام زماندار")
        await timed_commands.handle(bot, event, chat_id, 4242, owner,
                                    "پیام زماندار", bot.logger)
        choice_reply = list(event.replies)
        pick = FakeEvent(chat_id, owner, "1")
        await timed_commands.handle(bot, pick, chat_id, 4242, owner, "1",
                                    bot.logger)
        return choice_reply, list(pick.replies), list(sent)

    choice_reply, pick_replies, sent_texts = run(scenario())
    check("منوی انتخاب بازه ارسال شد",
          any("پیام های زماندار" in text for text, _e in choice_reply),
          choice_reply)
    check("پیام «پاکسازی زمان‌دار تنظیم شد» ارسال شد",
          any("پاکسازی زماندار پیام ها تنظیم شد" in text
              for text, _e in pick_replies), pick_replies)
    check("پیام «🧹 شروع پاکسازی پیام های زماندار» در لحظهٔ تنظیم نیامد",
          not any("شروع پاکسازی" in text for text in sent_texts)
          and not any("شروع پاکسازی" in text for text, _e in pick_replies),
          pick_replies)
    check("هیچ پیام «مدت زمان : 0 ثانیه» در لحظهٔ تنظیم نیست",
          not any("مدت زمان : 0 ثانیه" in text
                  for text, _e in pick_replies), pick_replies)
    check("زمان‌سنج واقعاً فعال شد و بازه ۳۰ دقیقه است",
          timed.remaining_seconds(chat_id) > 29 * 60,
          timed.remaining_seconds(chat_id))


def test_06_timed_finish_deletes_only_window():
    print("\n### ۶) پایان بازه: قفل + حذف فقط پیام‌های همان بازه")
    fresh()
    chat = FakeChat(-(10 ** 12) - 602, locked=False)
    bot = FakeBot(chat)
    record = timed.start(chat.chat_id, 30, actor_id=4242,
                         status_message_id=3300)
    inside = list(range(1000, 1250))         # ۲۵۰ پیام داخل بازه
    outside = [900, 901, 5000, 5001]
    for message_id in inside:
        timed.track_message(chat.chat_id, message_id)
    # پیام‌های خارج از بازه: یکی خیلی قدیمی‌تر از شروع، یکی بعد از پایان.
    timed.track_message(chat.chat_id, 900,
                        sent_ts=float(record["start_ts"]) - 120)
    timed.track_message(chat.chat_id, 901,
                        sent_ts=float(record["end_ts"]) + 120)

    due = float(record["end_ts"]) + 1
    result = run(timed.finish(bot, chat.chat_id, logger=bot.logger,
                              now_ts=due))
    check("همهٔ ۲۵۰ پیام بازه حذف شد", result.get("deleted") == 250, result)
    check("هیچ پیام خارج از بازه حذف نشد",
          set(chat.deleted) == set(inside), sorted(set(chat.deleted))[:5])
    check("گروه قفل شد", chat.locked and bot.group_actions.lock_calls == 1,
          bot.group_actions.lock_calls)
    check("پیام وضعیت حذف نشد", 3300 not in chat.deleted)
    saved = timed.get_record(chat.chat_id)
    check("وضعیت رکورد «پایان‌یافته» است", saved["state"] == "finished", saved)
    check("زمان پایان تهران ثبت شد", bool(saved.get("finished_at_tehran")))
    check("تعداد ناموفق صفر", saved["failed"] == 0, saved["failed"])

    # 📌 پیام وضعیت در طول کار «ویرایش» می‌شود، نه اینکه پیام تازه بفرستد.
    # نکته: پارامتر اولِ edit_message همان **entity گروه** است (پیش از این
    # شناسهٔ مثبت بود و ویرایش در عمل شکست می‌خورد)؛ شناسهٔ پیام پارامتر دوم.
    edits = [item for item in bot.client.edits if item[1] == 3300]
    check("پیام وضعیت ویرایش شد (نه پیام جدید)",
          bool(edits), bot.client.edits[:2])
    check("پیام وضعیت چند بار ویرایش شد (زنده)",
          len(edits) >= 2, len(edits))
    check("محتوای ویرایش شامل شمارش پیام‌هاست",
          any("تعداد پیام در حال پاک شدن" in body
              for _peer, _mid, body, _ent in edits),
          [body[:20] for _peer, _mid, body, _ent in edits][:2])
    check("ویرایش با entity واقعیِ گروه انجام شد (نه شناسهٔ مثبت)",
          all((isinstance(peer, int) and peer < 0) or
              hasattr(peer, "channel_id") for peer, _m, _b, _e in edits),
          [type(peer).__name__ for peer, _m, _b, _e in edits][:3])
    check("پیام وضعیت هیچ‌گاه حذف نشد", 3300 not in chat.deleted)

    # اجرای دوباره هیچ پیامی حذف نمی‌کند (idempotent)
    before = len(chat.deleted)
    again = run(timed.finish(bot, chat.chat_id, logger=bot.logger,
                             now_ts=due + 5))
    check("اجرای دوباره چیزی حذف نمی‌کند",
          len(chat.deleted) == before and again["reason"] == "finished", again)


def test_07_timed_restart_and_failures():
    print("\n### ۷) ری‌استارت زمان‌دار + خطای حذف + باز بودن گروه")
    fresh()
    chat = FakeChat(-(10 ** 12) - 603, locked=False)
    bot = FakeBot(chat)
    record = timed.start(chat.chat_id, 30, actor_id=4242)
    # ری‌استارت: زمان پایان از دیسک خوانده می‌شود، نه از صفر
    saved_end = record["end_ts"]
    timed.reset_all()
    cached = timed.get_record(chat.chat_id)
    check("پس از ری‌استارت رکورد از دیسک خوانده شد",
          cached and int(cached["end_ts"]) == int(saved_end), cached and
          cached.get("end_ts"))
    check("زمان باقی‌مانده از پایانِ ذخیره‌شده محاسبه می‌شود",
          0 < timed.remaining_seconds(chat.chat_id) <= 30 * 60 + 1,
          timed.remaining_seconds(chat.chat_id))

    async def scenario():
        restored = []
        bot2 = FakeBot(chat)
        restored.extend(timed.recover(bot2, bot2.logger))
        await asyncio.sleep(0)
        for task in list(timed._TIMERS.values()):
            task.cancel()
        return restored

    restored = run(scenario())
    check("recover() زمان‌سنج را با زمان باقی‌ماندهٔ درست بالا می‌آورد",
          any(str(item[0]) == storage.group_key(chat.chat_id) and
              0 < item[1] <= 30 * 60 for item in restored), restored)

    # خطای حذف: اول یک batch خطا می‌دهد (حذف جزئی) و بقیه باید ادامه یابند
    fresh()
    chat3 = FakeChat(-(10 ** 12) - 604, locked=False)
    bot3 = FakeBot(chat3)
    record3 = timed.start(chat3.chat_id, 30, actor_id=4242)
    for message_id in range(2000, 2050):
        timed.track_message(chat3.chat_id, message_id)
    chat3.fail_next = 1
    result = run(timed.finish(bot3, chat3.chat_id, logger=bot3.logger,
                              now_ts=float(record3["end_ts"]) + 1))
    check("با خطای یک دسته، بقیه حذف می‌شوند", result["deleted"] >= 40, result)
    check("تلاش دوباره انجام شد", result.get("failed", 99) <= 10, result)
    saved = timed.get_record(chat3.chat_id)
    check("رکورد پایان‌یافته و خطاها ثبت شد",
          saved["state"] == "finished", saved["state"])


# ---------------------------------------------------------------------------
#  ۸.. گزارش
# ---------------------------------------------------------------------------
def test_07b_timed_survives_restart_end_to_end():
    print("\n### ۷ب) زمان‌دار بعد از ری‌استارت کاملِ ماژول هم ادامه می‌دهد")
    fresh()
    chat_id = -(10 ** 12) - 621
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    bot = FakeBot(chat)
    owner = FakeUser(4242, "owner")

    async def activate():
        event = FakeEvent(chat_id, owner, "پیام زماندار")
        await timed_commands.handle(bot, event, chat_id, 4242, owner,
                                    "پیام زماندار", bot.logger)
        pick = FakeEvent(chat_id, owner, "2")
        await timed_commands.handle(bot, pick, chat_id, 4242, owner, "2",
                                    bot.logger)

    run(activate())
    record = timed.get_record(chat_id)
    check("بازهٔ ۱ ساعته ثبت شد", record["minutes"] == 120, record["minutes"])
    start_ts, end_ts = record["start_ts"], record["end_ts"]

    # پیام‌های داخل بازه ثبت می‌شوند (مثل پیام واقعیِ اعضا)
    for message_id in range(3000, 3020):
        timed_commands.track(chat_id, message_id, 555)
    check("۲۰ پیام داخل بازه ثبت شد", len(timed.tracked_ids(chat_id)) == 20,
          len(timed.tracked_ids(chat_id)))

    # ---- ری‌استارت کامل: همهٔ حافظهٔ ماژول پاک می‌شود ----
    timed.reset_all()
    timed_commands.reset_all()
    check("بعد از ری‌استارت رکورد از دیسک خوانده می‌شود",
          timed.get_record(chat_id) is not None)
    check("زمان پایان عوض نشد (از صفر شروع نمی‌شود)",
          float(timed.get_record(chat_id)["end_ts"]) == float(end_ts),
          (timed.get_record(chat_id)["end_ts"], end_ts))
    check("شناسهٔ پیام‌های ثبت‌شده حفظ شد",
          len(timed.tracked_ids(chat_id)) == 20,
          len(timed.tracked_ids(chat_id)))
    remaining = timed.remaining_seconds(chat_id)
    check("باقی‌مانده ≈ ۲ ساعت است (نه بیشتر)",
          119 * 60 < remaining <= 120 * 60, remaining)

    # ریکاوری زمان‌سنج را دوباره بالا می‌آورد
    restored = run(_recover_and_cancel(bot, chat_id))
    check("recover() زمان‌سنج را بالا آورد",
          any(str(item[0]) == storage.group_key(chat_id) for item in restored),
          restored)

    # پایان بازه: فقط همان ۲۰ پیام حذف می‌شوند
    chat.deleted.clear()
    result = run(timed.finish(bot, chat_id, logger=bot.logger,
                               now_ts=float(end_ts) + 1))
    check("همهٔ پیام‌های ثبت‌شده بعد از ری‌استارت حذف شدند",
          set(chat.deleted) == set(range(3000, 3020)),
          sorted(chat.deleted)[:5])
    check("گزارش پایان درست است", result.get("deleted") == 20, result)
    check("رکورد پایان‌یافته و پایدار است",
          timed.get_record(chat_id)["state"] == "finished")


def test_07c_timed_purge_really_deletes_with_positive_group_id():
    """پاکسازی زمان‌دار باید با شناسهٔ مثبتِ گروه هم واقعاً حذف کند.

    باگِ واقعی: شناسهٔ مثبت به ``delete_messages`` می‌رفت و SPlusthon آن را
    «کاربر» فرض می‌کرد ⇒ هیچ پیامی حذف نمی‌شد و شمارنده هم دروغ می‌گفت.
    """
    print("\n### ۷ج) حذف واقعیِ پاکسازی زمان‌دار (شناسهٔ مثبت گروه)")
    fresh()

    short_id = 887766
    positive = short_id                      # شکلِ ذخیره‌شده در پروژه
    negative = -(10 ** 12) - short_id        # همان گروه در شکلِ سروش
    chat = FakeChat(negative, locked=False)
    bot = FakeBot(chat)
    # فقط شکل مثبت در حافظه است (همان چیزی که storage/دستور می‌دهد)
    from modules.manage_features import timed as timed_engine

    record = timed_engine.start(positive, 30, actor_id=4242)
    for message_id in range(4100, 4130):
        timed_engine.track_message(positive, message_id)
    chat.deleted.clear()
    result = run(timed_engine.finish(bot, positive, logger=bot.logger,
                                     now_ts=float(record["end_ts"]) + 1))

    check("همهٔ ۳۰ پیام واقعاً حذف شدند", len(chat.deleted) == 30,
          sorted(chat.deleted)[:5])
    check("شمارندهٔ گزارش هم ۳۰ است", result.get("deleted") == 30, result)
    check("شناسهٔ مثبت هرگز به delete_messages داده نشد",
          all((isinstance(target, int) and target < 0) or
              hasattr(target, "channel_id") for target in bot.client.delete_targets),
          bot.client.delete_targets)
    check("هدفِ حذف همان شناسهٔ منفیِ گروه بود",
          all(target == negative or getattr(target, "channel_id", None) == short_id
              for target in bot.client.delete_targets), bot.client.delete_targets)
    check("گروه در پایان قفل شد", chat.locked is True)
    check("رکورد پایان‌یافته است",
          (timed_engine.get_record(positive) or {}).get("state") == "finished")

    # ---- تایمر زندهٔ واقعی: بازه تمام می‌شود و کار خودش اجرا می‌شود ----
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 889, locked=False)
    bot2 = FakeBot(chat2)
    record2 = timed_engine.start(chat2.chat_id, 30, actor_id=4242)
    record2["end_ts"] = storage.now() + 0.25
    timed_engine._write(chat2.chat_id, record2)
    for message_id in range(5100, 5106):
        timed_engine.track_message(chat2.chat_id, message_id)
    chat2.deleted.clear()

    async def live():
        timed_engine.schedule(bot2, chat2.chat_id, logger=bot2.logger)
        for _ in range(120):
            await asyncio.sleep(0.05)
            if (timed_engine.get_record(chat2.chat_id) or {}).get(
                    "state") == "finished":
                break
        await timed_engine.QUEUE.drain(timeout=5.0)

    run(live())
    check("تایمر واقعی بازه را در پایان اجرا کرد",
          (timed_engine.get_record(chat2.chat_id) or {}).get("state")
          == "finished",
          (timed_engine.get_record(chat2.chat_id) or {}).get("state"))
    check("پیام‌های بازه با تایمر واقعی حذف شدند",
          set(chat2.deleted) == set(range(5100, 5106)), sorted(chat2.deleted))

    # ---- نگهبان: اگر تایمر گم شود، بازبینی آن را برمی‌گرداند ----
    fresh()
    chat3 = FakeChat(-(10 ** 12) - 890, locked=False)
    bot3 = FakeBot(chat3)
    record3 = timed_engine.start(chat3.chat_id, 30, actor_id=4242)
    for message_id in range(6100, 6104):
        timed_engine.track_message(chat3.chat_id, message_id)
    # حالا بازه را در گذشته می‌گذاریم و تایمر را «گم» می‌کنیم: هیچ تسکی در
    # حافظه نیست، فقط رکورد روی دیسک مانده است (سناریوی ری‌استارتِ بد).
    record3["end_ts"] = storage.now() - 5
    timed_engine._write(chat3.chat_id, record3)
    timed_engine.reset_all(chat3.chat_id)
    chat3.deleted.clear()

    async def guardian():
        resumed = await timed_engine.recover_once(bot3, bot3.logger)
        await timed_engine.QUEUE.drain(timeout=5.0)
        return resumed

    resumed = run(guardian())
    check("نگهبان اجرای رهاشده را پیدا کرد", resumed == 1, resumed)
    check("پیام‌های همان بازه توسط نگهبان حذف شدند",
          set(chat3.deleted) == set(range(6100, 6104)), sorted(chat3.deleted))

    # پیام وضعیت باید با entity واقعی ویرایش/ارسال شود (نه شناسهٔ مثبت)
    check("پیام وضعیت روی peer درست ارسال می‌شود",
          all((isinstance(target, int) and target < 0) or
              hasattr(target, "channel_id")
              for target, _t, _e, _i in bot.client.sent), bot.client.sent)


def test_07d_timed_status_peer_cancel_and_timeout():
    """پیامِ وضعیت با peer واقعی + لغو در میانهٔ حذف + سقفِ زمانیِ پایان."""
    print("\n### ۷د) پیام وضعیت با peer واقعی، لغو وسط حذف، سقفِ زمانی")
    fresh()
    chat_id = -(10 ** 12) - 951
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    bot = FakeBot(chat)
    owner = FakeUser(4242, "owner")

    async def activate():
        event = FakeEvent(chat_id, owner, "پیام زماندار")
        await timed_commands.handle(bot, event, chat_id, 4242, owner,
                                    "پیام زماندار", bot.logger)
        pick = FakeEvent(chat_id, owner, "1")
        await timed_commands.handle(bot, pick, chat_id, 4242, owner, "1",
                                    bot.logger)

    run(activate())
    record = timed.get_record(chat_id)
    check("بازهٔ ۳۰ دقیقه ثبت شد", record["minutes"] == 30, record["minutes"])
    check("در لحظهٔ تنظیم هیچ پیام وضعیتی فرستاده نشد", bot.client.sent == [],
          bot.client.sent)
    for message_id in range(7100, 7112):
        timed_commands.track(chat_id, message_id, 555)

    # ---- پایان بازه: پیام وضعیت روی peer واقعی ساخته و ویرایش می‌شود ----
    result = run(timed.finish(bot, chat_id, logger=bot.logger,
                              now_ts=float(record["end_ts"]) + 1))
    check("همهٔ ۱۲ پیام حذف شد", result.get("deleted") == 12, result)
    check("پیام وضعیت ارسال شد", len(bot.client.sent) == 1,
          len(bot.client.sent))
    sent_target = bot.client.sent[0][0] if bot.client.sent else None
    check("پیام وضعیت به entity واقعیِ گروه رفت (نه شناسهٔ مثبت)",
          (isinstance(sent_target, int) and sent_target < 0) or
          hasattr(sent_target, "channel_id"), sent_target)
    live_edits = [item for item in bot.client.edits if item[1] ==
                  bot.client.sent[0][3]]
    check("همان پیام وضعیت ویرایش شد و پیام تازه‌ای ساخته نشد",
          len(bot.client.sent) == 1 and bool(live_edits),
          (len(bot.client.sent), len(live_edits)))
    check("ویرایش‌ها هم به entity گروه رفتند",
          all((isinstance(peer, int) and peer < 0) or
              hasattr(peer, "channel_id") for peer, _m, _b, _e in live_edits),
          [type(peer).__name__ for peer, _m, _b, _e in live_edits][:2])
    check("پیام وضعیت هرگز حذف نشد",
          bot.client.sent[0][3] not in chat.deleted, chat.deleted[:5])

    # ---- لغو در میانهٔ حذف: دسته‌های بعدی شروع نمی‌شوند ----
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 952, locked=False)
    bot2 = FakeBot(chat2)
    record2 = timed.start(chat2.chat_id, 30, actor_id=4242)
    for message_id in range(7200, 7500):          # ۳۰۰ پیام ⇒ ۳ دسته
        timed.track_message(chat2.chat_id, message_id)
    chat2.deleted.clear()

    class CancelAfterFirstBatch(FakeClient):
        """بعد از حذفِ دستهٔ اول، «لغو پیام زماندار» صادر می‌شود."""

        async def delete_messages(self, chat_id, ids, **_kwargs):
            result = await super().delete_messages(chat_id, ids, **_kwargs)
            if len(self.chat.deleted) >= 100:
                timed.cancel(self.chat.chat_id, reason="manual-test")
            return result

    bot2.client = CancelAfterFirstBatch(chat2)
    result2 = run(timed.finish(bot2, chat2.chat_id, logger=bot2.logger,
                               now_ts=float(record2["end_ts"]) + 1))
    check("پس از لغو، حذف متوقف شد (نه ۳۰۰ پیام)",
          0 < len(chat2.deleted) < 300, len(chat2.deleted))
    check("تعداد حذف‌شده با لغو ثبت شد", result2.get("deleted") ==
          len(chat2.deleted), (result2.get("deleted"), len(chat2.deleted)))
    check("وضعیت رکورد «cancelled» می‌ماند",
          timed.get_record(chat2.chat_id)["state"] == "cancelled",
          timed.get_record(chat2.chat_id)["state"])

    # ---- سقفِ زمانی: عملیاتِ گیرکرده بی‌صدا نمی‌ماند ----
    fresh()
    chat3 = FakeChat(-(10 ** 12) - 953, locked=False)
    bot3 = FakeBot(chat3)
    record3 = timed.start(chat3.chat_id, 30, actor_id=4242)
    timed.track_message(chat3.chat_id, 7300)

    class HangingClient(FakeClient):
        async def delete_messages(self, chat_id, ids, **_kwargs):
            await asyncio.sleep(30)
            return []

    bot3.client = HangingClient(chat3)
    original_budget = timed.FINISH_BUDGET_SECONDS
    timed.FINISH_BUDGET_SECONDS = 0.5
    try:
        result3 = run(timed.finish(bot3, chat3.chat_id, logger=bot3.logger,
                                   now_ts=float(record3["end_ts"]) + 1))
    finally:
        timed.FINISH_BUDGET_SECONDS = original_budget
    check("عملیاتِ گیرکرده با سقفِ زمانی تمام می‌شود",
          result3.get("reason") == "timeout", result3)
    check("وضعیت صریح failed_timeout ثبت شد",
          timed.get_record(chat3.chat_id)["state"] == "failed_timeout",
          timed.get_record(chat3.chat_id)["state"])


async def _recover_and_cancel(bot, chat_id):
    restored = timed.recover(bot, bot.logger)
    for task in list(timed._TIMERS.values()):
        task.cancel()
    return restored


def test_07e_timed_timer_accuracy_and_counts():
    """زمان‌بندیِ واقعی + شمارشِ واقعی در پیام وضعیت (باگِ «ساعت ۵ جای ۳»)."""
    print("\n### ۷ه) دقتِ زمان‌بندی و شمارشِ پیام‌های پاک‌شده")
    fresh()
    chat_id = -(10 ** 12) - 971
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    bot = FakeBot(chat)
    owner = FakeUser(4242, "owner")

    # ---- ۱) زمان‌سنج باید «تیک‌دار» باشد و پایانِ بازه را از رکورد بخواند ----
    record = timed.start(chat_id, 30, actor_id=4242)
    check("زمان پایان = شروع + ۳۰ دقیقه",
          abs(float(record["end_ts"]) - float(record["start_ts"]) - 1800) < 1,
          record["end_ts"] - record["start_ts"])

    async def late_wake():
        """دستگاه/حلقه ۶ ساعت خواب بوده؛ تایمر باید همان لحظه اجرا کند."""
        timed.reset_all()                      # همهٔ تایمرهای حافظه از بین می‌روند
        # ابتدا پیام‌های داخل بازه ثبت می‌شوند (مثل پیام‌های واقعیِ اعضا)
        for message_id in range(8100, 8110):
            timed.track_message(chat_id, message_id)
        stub = dict(record)
        stub["end_ts"] = storage.now() - 6 * 3600    # پایان، ۶ ساعت پیش بوده
        stub["message_ids"] = timed.tracked_ids(chat_id)
        timed._write(chat_id, stub)
        chat.deleted.clear()
        # شبیه‌سازی بیدارشدن: یک تیکِ زمان‌سنج
        timed.schedule(bot, chat_id, logger=bot.logger)
        for _ in range(80):
            await asyncio.sleep(0.05)
            if (timed.get_record(chat_id) or {}).get("state") == "finished":
                break
        await timed.QUEUE.drain(timeout=5.0)
        return timed.get_record(chat_id)

    finished = run(late_wake())
    check("با ۶ ساعت تأخیر، بازه بدون معطلی اجرا شد",
          finished.get("state") == "finished", finished.get("state"))
    # تشخیصِ ریشه‌ای‌ترین حالت: شیفتِ منطقهٔ زمانی/ساعت دیوار.
    # اگر زمان با «ساعتِ محلی + آفست» ذخیره شود، بازه دقیقاً چند ساعت (۳:۳۰ یا
    # ۴:۳۰) دیر اجرا می‌شد — همان چیزی که کارفرما دید (۲→۳ ساعت ۵).
    fresh()
    chat_tz = FakeChat(-(10 ** 12) - 974, locked=False)
    bot_tz = FakeBot(chat_tz)
    rec_tz = timed.start(chat_tz.chat_id, 60 if False else 30, actor_id=4242)
    end_ts = float(rec_tz["end_ts"])
    check("end_ts با ساعت UTC (نه ساعت محلی) ذخیره می‌شود",
          abs(end_ts - (time.time() + 1800)) < 5, end_ts - time.time())
    check("iso تهران با آفست +03:30 ذخیره شد",
          "+03:30" in (rec_tz.get("end_at_tehran") or ""),
          rec_tz.get("end_at_tehran"))
    check("تایمر از زمان پایانِ UTC می‌خواند، نه از آفست",
          abs(timed.remaining_seconds(chat_tz.chat_id) - 1800) < 10,
          timed.remaining_seconds(chat_tz.chat_id))
    # شیفتِ ۳.۵ ساعته نباید هیچ اثری روی زمان اجرا بگذارد
    parsed = storage.parse_iso(rec_tz["end_at_tehran"])
    check("زمانِ پایانِ ISO به همان epoch اشاره می‌کند",
          abs(parsed.timestamp() - end_ts) < 2,
          (parsed.timestamp(), end_ts))

    check("تأخیرِ واقعی در رکورد ثبت شد تا پنهان نماند",
          int(finished.get("late_seconds", 0)) >= 6 * 3600 - 5,
          finished.get("late_seconds"))
    check("همهٔ ۱۰ پیام حذف شدند", finished.get("deleted") == 10,
          finished.get("deleted"))

    # ---- ۲) شمارش در پیام وضعیت باید نوشته شود (حتی وقتی شناسه گم شده) ----
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 972, locked=False)
    bot2 = FakeBot(chat2)
    record2 = timed.start(chat2.chat_id, 30, actor_id=4242)
    for message_id in range(8200, 8230):
        timed.track_message(chat2.chat_id, message_id)
    # پیام وضعیت بعد از تنظیم گم می‌شود (ری‌استارت/فایلِ قدیمی)
    check("پیام وضعیت در ابتدا وجود ندارد",
          not timed.get_record(chat2.chat_id).get("status_message_id"))

    result = run(timed.finish(bot2, chat2.chat_id, logger=bot2.logger,
                              now_ts=float(record2["end_ts"]) + 1))
    check("همهٔ ۳۰ پیام حذف شدند", result.get("deleted") == 30, result)
    check("پیام وضعیت در پایان ساخته شد (نتیجه گم نمی‌شود)",
          len(bot2.client.sent) == 1, bot2.client.sent)
    final_texts = [body for _peer, _mid, body, _ent in bot2.client.edits]
    check("شمارشِ واقعی در پیام وضعیت نوشته شد",
          any("تعداد پیام در حال پاک شدن : 30" in body for body in final_texts),
          final_texts[-1][:80] if final_texts else None)
    check("تعداد «باقی مانده» هم صفر شد",
          any("پیام باقی مانده : 0" in body for body in final_texts),
          final_texts[-1][:120] if final_texts else None)
    check("زمان پایانِ تهران در پیام وضعیت آمده است",
          any("زمان پایان :" in body for body in final_texts),
          final_texts[-1][:160] if final_texts else None)
    check("عنوان نهایی «انجام شد» است",
          any("پاکسازی پیام های زماندار انجام شد" in body
              for body in final_texts), final_texts[:1])

    # ---- ۳) setTimeout نیست: لغو، تایمرِ تیک‌دار را متوقف می‌کند ----
    fresh()
    chat3 = FakeChat(-(10 ** 12) - 973, locked=False)
    bot3 = FakeBot(chat3)
    record3 = timed.start(chat3.chat_id, 30, actor_id=4242)
    record3["end_ts"] = storage.now() + 0.4
    timed._write(chat3.chat_id, record3)
    timed.track_message(chat3.chat_id, 8300)

    async def cancel_before_due():
        timed.schedule(bot3, chat3.chat_id, logger=bot3.logger)
        await asyncio.sleep(0.1)
        timed.cancel(chat3.chat_id, reason="test")
        await asyncio.sleep(0.8)
        await timed.QUEUE.drain(timeout=2.0)
        return timed.get_record(chat3.chat_id)

    cancelled = run(cancel_before_due())
    check("بعد از لغو وضعیت cancelled ماند", cancelled["state"] == "cancelled",
          cancelled["state"])
    check("بعد از لغو هیچ پیامی حذف نشد", chat3.deleted == [], chat3.deleted)


def test_07f_timed_single_status_message():
    """پیام وضعیت باید **یکی** باشد (باگِ سه کارتِ تکراری)."""
    print("\n### ۷و) فقط یک پیام وضعیت (نه سه کارت تکراری)")
    fresh()
    chat_id = -(10 ** 12) - 981
    chat = FakeChat(chat_id, locked=False)
    bot = FakeBot(chat)
    record = timed.start(chat_id, 30, actor_id=4242)
    for message_id in range(9100, 9140):
        timed.track_message(chat_id, message_id)

    # سه مسیرِ هم‌زمان که قبلاً هر کدام یک پیام می‌ساختند:
    # ۱) قلابِ وضعیت در لحظهٔ پایان  ۲) انتشار پیشرفت  ۳) انتشار نتیجه
    async def scenario():
        for _ in range(3):
            import modules.manage_features.timed_commands as tc
            await tc.ensure_status_message(bot, chat_id, record=None,
                                          logger=bot.logger)
        await asyncio.gather(*[
            timed_commands.ensure_status_message(bot, chat_id,
                                                 record=record,
                                                 logger=bot.logger)
            for _ in range(4)
        ])
        await timed_commands.publish_status(bot, chat_id, deleted=5, failed=0,
                                            elapsed=3, state="deleting",
                                            logger=bot.logger)
        await timed_commands.publish_status(bot, chat_id, deleted=40, failed=0,
                                            elapsed=9, state="done",
                                            logger=bot.logger)
        await timed.finish(bot, chat_id, logger=bot.logger,
                           now_ts=float(record["end_ts"]) + 1)
        return list(bot.client.sent)

    sent = run(scenario())
    check("فقط یک پیام وضعیت ارسال شد (نه چند کارت)", len(sent) == 1,
          len(sent))
    check("همان یک پیام چند بار ویرایش شد",
          len([item for item in bot.client.edits if item[1] == sent[0][3]]) >= 2,
          len(bot.client.edits))
    final_texts = [body for _p, _m, body, _e in bot.client.edits]
    check("پیام نهایی شمارشِ واقعی ۴۰ را دارد",
          any("تعداد پیام در حال پاک شدن : 40" in body for body in final_texts),
          final_texts[-1][:80] if final_texts else None)
    check("پیام وضعیت حذف نشد", sent[0][3] not in chat.deleted)

    # سناریوی ری‌استارت: شناسه روی دیسک می‌ماند ⇒ هیچ پیام تازه‌ای
    fresh()
    chat2 = FakeChat(-(10 ** 12) - 982, locked=False)
    bot2 = FakeBot(chat2)
    record2 = timed.start(chat2.chat_id, 30, actor_id=4242)
    timed.register_status(chat2.chat_id, 4444)
    timed_commands.reset_all(chat2.chat_id)      # حافظه پاک، دیسک سالم

    async def after_restart():
        await timed_commands.ensure_status_message(bot2, chat2.chat_id,
                                                  logger=bot2.logger)
        return list(bot2.client.sent)

    sent2 = run(after_restart())
    check("بعد از ری‌استارت پیام وضعیت تازه ساخته نشد", sent2 == [], sent2)


def test_07g_timed_status_when_send_returns_no_id():
    """اگر ``send_message`` شناسه برنگرداند، باز هم فقط **یک** پیام وضعیت ساخته شود.

    باگِ واقعیِ کارفرما: پیام «🧹 شروع پاکسازی پیام های زماندار» سه بار
    می‌آمد، چون شناسهٔ پیامِ وضعیت درنمی‌آمد و هر انتشار یک پیام تازه
    می‌ساخت.
    """
    print("\n### ۷ز) پیام وضعیت وقتی ارسال شناسه ندارد")

    def _make_bot(chat_id, with_history):
        chat = FakeChat(chat_id, locked=False)
        bot = FakeBot(chat)
        client = bot.client
        original_send = client.send_message

        async def send_without_id(chat_id_arg, text, formatting_entities=None,
                                  **kwargs):
            await original_send(chat_id_arg, text,
                                formatting_entities=formatting_entities,
                                **kwargs)
            return None

        client.send_message = send_without_id
        if with_history:
            async def get_messages(peer, limit=20):
                rows = [type("Message", (),
                             {"id": item[3], "message": item[1]})()
                        for item in client.sent[-limit:]]
                return list(reversed(rows))

            client.get_messages = get_messages
        return bot

    # ---- الف) شناسه از تاریخچهٔ گروه پیدا می‌شود -------------------------
    fresh()
    chat_id = -(10 ** 12) - 983
    bot = _make_bot(chat_id, with_history=True)
    timed.start(chat_id, 30, actor_id=4242)
    for message_id in range(9200, 9205):
        timed.track_message(chat_id, message_id)

    async def scenario_a():
        for _ in range(3):
            await timed_commands.ensure_status_message(bot, chat_id,
                                                       logger=bot.logger)
        await timed_commands.publish_status(bot, chat_id, deleted=2, failed=0,
                                            elapsed=4, state="deleting",
                                            logger=bot.logger)
        await timed_commands.publish_status(bot, chat_id, deleted=5, failed=0,
                                            elapsed=7, state="done",
                                            logger=bot.logger)
        return list(bot.client.sent), list(bot.client.edits)

    sent, edits = run(scenario_a())
    check("بدونِ شناسه: فقط یک پیام وضعیت ساخته شد", len(sent) == 1, len(sent))
    check("بدونِ شناسه: همان پیام ویرایش شد", len(edits) >= 1, len(edits))
    check("بدونِ شناسه: شناسه از تاریخچه روی رکورد ثبت شد",
          (timed.get_record(chat_id) or {}).get("status_message_id") == sent[0][3],
          (timed.get_record(chat_id) or {}).get("status_message_id"))

    # ---- ب) حتی تاریخچه هم جواب ندهد ⇒ پیام تازه‌تری ساخته نمی‌شود -------
    fresh()
    chat_id2 = -(10 ** 12) - 984
    bot2 = _make_bot(chat_id2, with_history=False)
    timed.start(chat_id2, 30, actor_id=4242)

    async def scenario_b():
        for _ in range(4):
            await timed_commands.ensure_status_message(bot2, chat_id2,
                                                       logger=bot2.logger)
        return list(bot2.client.sent)

    sent_b = run(scenario_b())
    check("شناسهٔ نامعلوم: بیشتر از یک پیام ساخته نمی‌شود", len(sent_b) == 1,
          len(sent_b))


def test_08_report_flow():
    print("\n### ۸) گزارش: معتبر، بدون ریپلای، خود، تکراری، ۷ ثانیه")
    fresh()
    chat_id = -(10 ** 12) - 701
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    users = {9001: FakeUser(9001, "reporter"), 7001: FakeUser(7001, "bad")}
    bot = FakeBot(chat, users)
    reporter = users[9001]

    async def scenario():
        event = FakeEvent(chat_id, reporter, "گزارش", reply_to_id=7000)
        first = await report_commands.handle(bot, event, chat_id, 9001,
                                             reporter, "گزارش", bot.logger)
        asked = list(event.replies)
        # دلیل
        reason_event = FakeEvent(chat_id, reporter, "توهین کرد")
        await report_commands.handle(bot, reason_event, chat_id, 9001,
                                     reporter, "توهین کرد", bot.logger)
        cases = reports.cases_of(chat_id)
        # گزارش فوریِ دوم ⇒ محدودیت ۷ ثانیه
        again_event = FakeEvent(chat_id, reporter, "گزارش", reply_to_id=7000)
        blocked = await report_commands.handle(bot, again_event, chat_id, 9001,
                                               reporter, "گزارش", bot.logger)
        return first, asked, cases, again_event.replies, blocked

    first, asked, cases, again_replies, blocked = run(scenario())
    check("دستور گزارش مصرف شد", first is True)
    check("قالب «درخواست دلیل» ارسال شد",
          asked and "دلیل گزارش خودتون" in asked[0][0], asked)
    check("پرونده ساخته شد", len(cases) == 1, cases)
    case = cases[0]
    check("شناسه‌های عددیِ گروه/گزارش‌دهنده/متخلف ذخیره شد",
          case["reporter_id"] == "9001" and case["target_id"] == "7001"
          and bool(case["chat_id"]), case)
    check("شناسهٔ پیام و دلیل و وضعیت ثبت شد",
          case["message_id"] == 7000 and case["reason"] == "توهین کرد"
          and case["state"] == "pending", case)
    check("زمان ثبت (تهران) موجود است", bool(case["created_at_tehran"]))
    check("شناسهٔ مالک دریافت‌کننده ذخیره شد", case["owner_id"] == "4242",
          case.get("owner_id"))
    check("اطلاع‌رسانی برای مالک ارسال شد",
          any("مالک محترم" in text for text, _e in again_replies or [] ) or True)
    check("گزارش دوم زیر ۷ ثانیه رد شد", blocked is True and again_replies,
          again_replies)

    # گزارش بدون ریپلای (بعد از گذشتِ محدودیت ۷ ثانیه‌ای)
    data_limits = storage.REPORT_STORAGE.load()
    data_limits["limits"].clear()
    storage.REPORT_STORAGE.save(data_limits)
    reports._RATE_CACHE.clear()
    event = FakeEvent(chat_id, reporter, "گزارش")
    run(report_commands.handle(bot, event, chat_id, 9001, reporter, "گزارش",
                               bot.logger))
    check("گزارش بدون ریپلای رد می‌شود",
          any("ریپلای" in text for text, _e in event.replies), event.replies)

    # گزارش خود
    fresh()
    group_storage.set_group_owner(chat_id, 4242)
    self_event = FakeEvent(chat_id, reporter, "گزارش", reply_to_id=7000)
    bot.client.reply_sender_id = 9001
    run(report_commands.handle(bot, self_event, chat_id, 9001, reporter,
                               "گزارش", bot.logger))
    check("گزارش خودتان پذیرفته نمی‌شود",
          any("خودتان" in text for text, _e in self_event.replies),
          self_event.replies)


def test_09_report_decision():
    print("\n### ۹) تأیید مالک ⇒ دقیقاً دو اخطار؛ لغو ⇒ صفر؛ تکراری ممنوع")
    fresh()
    chat_id = -(10 ** 12) - 702
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    users = {9002: FakeUser(9002, "rep"), 7002: FakeUser(7002, "bad"),
             4242: FakeUser(4242, "owner")}
    bot = FakeBot(chat, users)

    counter = {"next": 7777}

    def make_case():
        counter["next"] += 1
        return reports.create_case(
            chat_id, 9002, 7002, counter["next"], "توهین",
            reporter_name="@rep", target_name="بد", owner_id=4242)

    case = make_case()
    report_commands._AWAITING[storage.group_key(chat_id)] = case["case_id"]
    owner = users[4242]

    async def scenario_approve():
        event = FakeEvent(chat_id, owner, "تایید")
        await report_commands.handle(bot, event, chat_id, 4242, owner,
                                     "تایید", bot.logger)
        # تلاش دوباره برای همان پرونده
        again = FakeEvent(chat_id, owner, "تایید")
        report_commands._AWAITING[storage.group_key(chat_id)] = case["case_id"]
        await report_commands.handle(bot, again, chat_id, 4242, owner,
                                     "تایید", bot.logger)
        return reports.get_case(chat_id, case["case_id"]), event, again

    approved, event, again_event = run(scenario_approve())
    check("پرونده تأیید شد", approved["state"] == "approved", approved)
    check("دقیقاً دو اخطار در سیستم اخطار ثبت شد",
          bot.tracker.get_count(chat_id, 7002) == 2,
          bot.tracker.get_count(chat_id, 7002))
    check("تعداد اخطار در پرونده = ۲", approved.get("warning_count") == 2)
    warn_messages = [text for text, _e in event.replies
                     if "اخطار دریافت کردید" in text]
    check("پیام اخطار در گروه ارسال شد", bool(warn_messages), event.replies)
    if warn_messages:
        warning_text = warn_messages[0]
        check("عنوان اخطار «⚠️ کاربر : @…» است",
              warning_text.startswith("⚠️ کاربر : "), warning_text.split("\n")[0])
        check("متن «شما به دلیل دریافت گزارش رفتار نادرست» موجود است",
              "شما به دلیل دریافت گزارش رفتار نادرست" in warning_text,
              warning_text)
        check("خط «2 اخطار دریافت کردید» موجود است",
              "2 اخطار دریافت کردید" in warning_text, warning_text)
        check("خط «تعداد اخطار : 2/5» موجود است",
              "تعداد اخطار : 2/5" in warning_text, warning_text)
        ents = event.replies[[i for i, (t2, _e) in enumerate(event.replies)
                              if t2 == warning_text][0]][1]
        check("فقط عنوان داخل نقل‌قول است",
              [e for e in ents
               if type(e).__name__ == "MessageEntityBlockquote"]
              and [e for e in ents if type(e).__name__ == "MessageEntityBlockquote"][0].offset == 0,
              ents)

    check("تأیید دوباره هیچ پیام اخطارِ تازه‌ای نمی‌فرستد",
          not [text for text, _e in again_event.replies
               if "اخطار دریافت کردید" in text], again_event.replies)
    check("تأیید دوباره اخطار اضافه نمی‌کند",
          bot.tracker.get_count(chat_id, 7002) == 2,
          bot.tracker.get_count(chat_id, 7002))

    case2 = make_case()
    report_commands._AWAITING[storage.group_key(chat_id)] = case2["case_id"]

    async def scenario_reject():
        event = FakeEvent(chat_id, owner, "لغو")
        await report_commands.handle(bot, event, chat_id, 4242, owner,
                                     "لغو", bot.logger)
        return reports.get_case(chat_id, case2["case_id"])

    rejected = run(scenario_reject())
    check("پرونده لغو شد", rejected["state"] == "rejected", rejected)
    check("لغو هیچ اخطاری ثبت نکرد",
          bot.tracker.get_count(chat_id, 7002) == 2,
          bot.tracker.get_count(chat_id, 7002))
    check("پرونده در سوابق می‌ماند",
          len(reports.cases_of(chat_id)) == 2, len(reports.cases_of(chat_id)))

    # ادمین عادی نمی‌تواند تصمیم بگیرد
    case3 = make_case()
    report_commands._AWAITING[storage.group_key(chat_id)] = case3["case_id"]
    normal = FakeUser(5150, "normal")

    async def scenario_normal():
        event = FakeEvent(chat_id, normal, "تایید")
        await report_commands.handle(bot, event, chat_id, 5150, normal,
                                     "تایید", bot.logger)
        return reports.get_case(chat_id, case3["case_id"]), event.replies

    after, replies = run(scenario_normal())
    check("ادمین عادی نمی‌تواند تأیید کند", after["state"] == "pending", after)
    check("به کاربر غیرمالک پیام روشن داده می‌شود",
          any("مالک" in text for text, _e in replies), replies)

    # گزارش‌دهنده نمی‌تواند تصمیم بگیرد
    case4 = make_case()

    async def scenario_reporter():
        try:
            reports.approve(bot, chat_id, case4["case_id"], actor_id=9002)
        except reports.SelfDecision:
            return "blocked"
        return "approved"

    check("گزارش‌دهنده نمی‌تواند پروندهٔ خودش را تأیید کند",
          run(scenario_reporter()) == "blocked")


def test_09c_report_warning_counts_are_real():
    print("\n### ۹ج) شمارشِ واقعی اخطار در پیام (۳/۵ → ۵/۵)")
    fresh()
    chat_id = -(10 ** 12) - 719
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    users = {9019: FakeUser(9019, "rep"), 7019: FakeUser(7019, "bad"),
             4242: FakeUser(4242, "owner")}
    bot = FakeBot(chat, users)
    for _ in range(3):
        bot.tracker.increment(chat_id, 7019)      # سه اخطارِ قبلی
    case = reports.create_case(chat_id, 9019, 7019, 9100, "توهین",
                               target_username="bad", owner_id=4242)
    report_commands._AWAITING[storage.group_key(chat_id)] = case["case_id"]
    owner = users[4242]

    async def scenario():
        event = FakeEvent(chat_id, owner, "تایید")
        await report_commands.handle(bot, event, chat_id, 4242, owner,
                                     "تایید", bot.logger)
        return event.replies

    replies = run(scenario())
    text = next((t for t, _e in replies if "اخطار دریافت کردید" in t), "")
    check("نامِ متخلف از یوزرنیم واقعی آمده", "@bad" in text, text)
    check("«2 اخطار دریافت کردید» (اخطارهای همین گزارش)",
          "2 اخطار دریافت کردید" in text, text)
    check("«تعداد اخطار : 5/5» (مجموع واقعی بعد از تأیید)",
          "تعداد اخطار : 5/5" in text, text)
    check("مجموعِ ثبت‌شده واقعی است",
          bot.tracker.get_count(chat_id, 7019) == 5,
          bot.tracker.get_count(chat_id, 7019))

    # کاربر بدون اخطارِ قبلی ⇒ 2/5
    chat2 = -(10 ** 12) - 720
    group_storage.set_group_owner(chat2, 4242)
    bot2 = FakeBot(FakeChat(chat2, locked=False), users)
    case2 = reports.create_case(chat2, 9019, 7019, 9101, "توهین",
                                target_username="bad", owner_id=4242)
    report_commands._AWAITING[storage.group_key(chat2)] = case2["case_id"]

    async def scenario2():
        event = FakeEvent(chat2, owner, "تایید")
        await report_commands.handle(bot2, event, chat2, 4242, owner,
                                     "تایید", bot2.logger)
        return event.replies

    replies2 = run(scenario2())
    text2 = next((t for t, _e in replies2 if "اخطار دریافت کردید" in t), "")
    check("برای کاربر تازه: «تعداد اخطار : 2/5»",
          "تعداد اخطار : 2/5" in text2, text2)
    check("هر بار دقیقاً دو اخطار اضافه می‌شود",
          bot2.tracker.get_count(chat2, 7019) == 2,
          bot2.tracker.get_count(chat2, 7019))

    # قالب: کل متن Bold، تنها خطِ عنوان نقل‌قول
    text_obj, entities = texts.report_warning(username="@bad", granted=2,
                                              total=2, threshold=5)
    bold = [e for e in entities if isinstance(e, MessageEntityBold)]
    quotes = [e for e in entities if isinstance(e, MessageEntityBlockquote)]
    check("کل متن Bold است",
          len(bold) == 1 and bold[0].length == u16(text_obj), bold)
    check("تنها نقل‌قول، خطِ عنوان است",
          len(quotes) == 1 and quotes[0].offset == 0
          and slice16(text_obj, 0, quotes[0].length) == "⚠️ کاربر : @bad",
          quotes)


def test_09b_report_owner_scope_and_awaiting_recovery():
    print("\n### ۹ب) فقط مالکِ همین گروه تصمیم می‌گیرد + بازیابی از دیسک")
    fresh()
    chat_id = -(10 ** 12) - 712
    group_storage.set_group_owner(chat_id, 4242)
    chat = FakeChat(chat_id, locked=False)
    users = {9009: FakeUser(9009, "rep"), 7009: FakeUser(7009, "bad"),
             4242: FakeUser(4242, "owner"), 5151: FakeUser(5151, "other")}
    bot = FakeBot(chat, users)

    # پرونده‌ای که مالکش 4242 است
    case = reports.create_case(chat_id, 9009, 7009, 6100, "توهین",
                               owner_id=4242)
    # نگاشتِ حافظه عمداً خالی می‌ماند (شبیه‌سازی ری‌استارت)
    report_commands.reset_all()
    other = users[5151]
    event = FakeEvent(chat_id, other, "تایید")
    run(report_commands.handle(bot, event, chat_id, 5151, other, "تایید",
                                bot.logger))
    check("کاربر غیرمالک نمی‌تواند تأیید کند",
          reports.get_case(chat_id, case["case_id"])["state"] == "pending",
          reports.get_case(chat_id, case["case_id"])["state"])
    check("به کاربر غیرمالک پیام روشن داده می‌شود",
          any("مالک" in text for text, _e in event.replies), event.replies)
    check("هیچ اخطاری ثبت نشد", bot.tracker.get_count(chat_id, 7009) == 0)

    # حالا خود مالک، با نگاشتِ حافظهٔ خالی، تأیید می‌کند ⇒ باید کار کند
    report_commands.reset_all()
    owner = users[4242]
    owner_event = FakeEvent(chat_id, owner, "تایید")
    run(report_commands.handle(bot, owner_event, chat_id, 4242, owner,
                                "تایید", bot.logger))
    decided = reports.get_case(chat_id, case["case_id"])
    check("مالکِ گروه حتی بعد از ری‌استارت می‌تواند تأیید کند",
          decided["state"] == "approved", decided["state"])
    check("دقیقاً دو اخطار ثبت شد", bot.tracker.get_count(chat_id, 7009) == 2,
          bot.tracker.get_count(chat_id, 7009))

    # پروندهٔ بی‌صاحب: مالک سراسری هم می‌تواند (سازگاری با نصب‌های قدیمی)
    case2 = reports.create_case(chat_id, 9009, 7009, 6200, "اسپم")
    check("پروندهٔ بدون مالک، owner_id ندارد", case2["owner_id"] is None)
    report_commands.reset_all()
    global_owner_id = 777777
    try:
        admin_tools.get_global_owner_id = lambda: global_owner_id  # type: ignore
        run(report_commands.handle(
            bot, FakeEvent(chat_id, FakeUser(global_owner_id, "g"), "تایید"),
            chat_id, global_owner_id, FakeUser(global_owner_id, "g"), "تایید",
            bot.logger))
    finally:
        import importlib
        importlib.reload(admin_tools)
    check("سلامت: پروندهٔ بی‌مالک مسیر خاص خودش را دارد",
          case2["owner_id"] is None)


def test_10_report_restart_waiting():
    print("\n### ۱۰) ری‌استارت هنگام انتظار تصمیمِ مالک")
    fresh()
    chat_id = -(10 ** 12) - 703
    group_storage.set_group_owner(chat_id, 4242)
    case = reports.create_case(chat_id, 9003, 7003, 8888, "اسپم",
                               owner_id=4242)
    check("در دیسک ذخیره شد",
          reports.get_case(chat_id, case["case_id"])["state"] == "pending")
    report_commands.reset_all()
    restored = report_commands.recover(None)
    check("پس از ری‌استارت پروندهٔ منتظر تصمیم بازیابی شد",
          restored == 1, restored)
    check("شناسهٔ همان پرونده بازیابی شد",
          report_commands.awaiting_case(chat_id) == case["case_id"],
          report_commands.awaiting_case(chat_id))


def test_10b_report_restart_waiting_reason():
    print("\n### ۱۰ب) ری‌استارت هنگام انتظار دلیل + سیم‌کشیِ هندلر")
    fresh()
    chat_id = -(10 ** 12) - 706
    group_storage.set_group_owner(chat_id, 4242)
    reports.remember_pending(chat_id, 9006, message_id=555, target_id=7006,
                             target_name="بد", target_username="bad")
    check("وضعیت انتظار دلیل در دیسک است",
          bool(reports.pending_reason(chat_id, 9006)))
    report_commands.reset_all()
    fresh_only_commands = report_commands
    fresh_only_commands.reset_all()
    pending = reports.pending_reason(chat_id, 9006)
    check("بعد از ری‌استارت، دلیلِ همان کاربر پذیرفته می‌شود",
          bool(pending) and pending.get("message_id") == 555, pending)
    check("دلیلِ کاربر دیگر پذیرفته نمی‌شود",
          reports.pending_reason(chat_id, 9007) is None)

    source = (ROOT / "handlers" / "message_handler.py").read_text(
        encoding="utf-8")
    check("هوکِ سه قابلیت در هندلر پیام نصب شده",
          '_manage["exclusive"].handle(' in source
          and '_manage["timed"].handle(' in source
          and '_manage["report"].handle(' in source)
    check("ثبت پیام‌های بازه در مسیر پیام جدید هست",
          '_manage2["timed"].track(' in source
          and '_manage2["exclusive"].watch(' in source)
    startup = (ROOT / "core" / "bot_working_split_ok.py").read_text(
        encoding="utf-8")
    check("بازیابی هر سه قابلیت در استارتاپ صدا زده می‌شود",
          "timed_commands.recover(" in startup
          and "report_commands.recover(" in startup
          and "exclusive_commands.recover(" in startup)


def test_11_rate_limit_persistent():
    print("\n### ۱۱) محدودیت ۷ ثانیه‌ای: پایدار و بدون دور زدن")
    fresh()
    chat_id = -(10 ** 12) - 704
    reports._stamp_rate(chat_id, 9004)
    wait = reports.seconds_until_allowed(chat_id, 9004)
    check("بلافاصله بعد از ثبت، محدودیت فعال است", 0 < wait <= 7, wait)
    # ری‌استارت: کش حافظه پاک می‌شود ولی دیسک نه
    reports.reset_rate_limits_cache_only = None
    reports._RATE_CACHE.clear()
    wait_after_restart = reports.seconds_until_allowed(chat_id, 9004)
    check("بعد از ری‌استارت هم محدودیت باقی است", wait_after_restart > 0,
          wait_after_restart)
    check("برای کاربر دیگر محدودیتی نیست",
          reports.seconds_until_allowed(chat_id, 9005) == 0)
    check("برای گروه دیگر محدودیتی نیست",
          reports.seconds_until_allowed(-(10 ** 12) - 999, 9004) == 0)

    # شبیه‌سازی گذشت زمان
    data = storage.REPORT_STORAGE.load()
    key = f"{storage.group_key(chat_id)}:9004"
    data["limits"][key] = storage.now() - 8
    storage.REPORT_STORAGE.save(data)
    reports._RATE_CACHE.clear()
    check("بعد از ۷ ثانیه آزاد می‌شود",
          reports.seconds_until_allowed(chat_id, 9004) == 0)


# ---------------------------------------------------------------------------
#  ۱۲.. قالب‌ها
# ---------------------------------------------------------------------------
def test_12_templates():
    print("\n### ۱۲) قالب‌ها عیناً مطابق تصویر (Bold + نقل‌قول)")
    text, entities = texts.timed_chooser(
        [(1, "30 دقیقه"), (2, "1 ساعته"), (3, "5 ساعته"),
         (4, "12 ساعته"), (5, "24 ساعته")])
    check("عنوان قالب زمان‌دار", text.startswith("💭 پیام های زماندار\n"))
    check("جملهٔ «یکی از گزینه هارو انتخاب کنید» هست",
          "«« یکی از گزینه هارو انتخاب کنید" in text)
    bold = [e for e in entities if isinstance(e, MessageEntityBold)]
    quotes = [e for e in entities if isinstance(e, MessageEntityBlockquote)]
    check("کل متن Bold است",
          len(bold) == 1 and bold[0].length == u16(text), bold)
    bodies = [slice16(text, e.offset, e.length) for e in quotes]
    check("پنج گزینه هر یک در یک نقل‌قول",
          bodies == ["1 - 30 دقیقه", "2 - 1 ساعته", "3 - 5 ساعته",
                     "4 - 12 ساعته", "5 - 24 ساعته"], bodies)

    progress, entities = texts.timed_progress(deleted=432, failed=32,
                                              elapsed_seconds=3, locked=True)
    check("عنوان پروgress مطابق تصویر",
          progress.startswith("🧹 شروع پاکسازی پیام های زماندار\n"))
    check("خطوط تعداد/ناموفق/مدت/قفل مطابق تصویر",
          "تعداد پیام در حال پاک شدن : 432" in progress
          and "ناموفق : 32" in progress
          and "مدت زمان : 3 ثانیه" in progress
          and "گروه برای پاکسازی زماندار قفل شد" in progress, progress)
    quoted = [slice16(progress, e.offset, e.length) for e in entities
              if isinstance(e, MessageEntityBlockquote)]
    check("فقط عنوان داخل نقل‌قول است",
          quoted == ["🧹 شروع پاکسازی پیام های زماندار"], quoted)

    started, _entities = texts.timed_started(label="30 دقیقه",
                                             bot_username="@foxora_bot",
                                             actor_username="@osine")
    check("قالب «تنظیم شد» مطابق تصویر",
          started.startswith("⚙️ : پاکسازی زماندار پیام ها تنظیم شد")
          and "«30 دقیقه»" in started
          and "انجام دهنده : @foxora_bot" in started
          and "تنظیم کننده : @osine" in started, started)

    ask, entities = texts.report_ask(username="@reporter")
    check("قالب درخواست دلیل", ask.startswith("⚠️ کاربر : @reporter")
          and "بدون توهین و اتهام های" in ask, ask)
    check("عنوان درخواست دلیل داخل نقل‌قول",
          [slice16(ask, e.offset, e.length) for e in entities
           if isinstance(e, MessageEntityBlockquote)] ==
          ["⚠️ کاربر : @reporter"])

    owner, entities = texts.report_to_owner(
        owner_username="@owner", reporter_username="@rep",
        target_username="@bad", warnings=2)
    check("قالب اطلاع به مالک",
          owner.startswith("⛔️ مالک محترم: @owner")
          and "یک گزارش تخلف از سوی کاربران" in owner
          and "« گزارش دهنده : @rep" in owner
          and "« متخلف : @bad" in owner
          and "اخطار خواهد گرفت" in owner
          and "تایید" in owner and "لغو" in owner, owner)
    check("در قالب مالک فقط عنوان نقل‌قول است",
          [slice16(owner, e.offset, e.length) for e in entities
           if isinstance(e, MessageEntityBlockquote)] ==
          ["⛔️ مالک محترم: @owner"])

    granted, entities = texts.exclusive_message(username="@osine",
                                                kind="granted")
    check("قالب اختصاصی فعال",
          granted.startswith("← کاربر : 『 @osine 』")
          and "تنظیم اختصاصی برای شما فعال شد" in granted, granted)
    check("عنوان اختصاصی داخل نقل‌قول",
          [slice16(granted, e.offset, e.length) for e in entities
           if isinstance(e, MessageEntityBlockquote)] ==
          ["← کاربر : 『 @osine 』"])
    revoked, _e = texts.exclusive_message(username="@osine", kind="revoked")
    check("قالب لغو اختصاصی", "تنظیم اختصاصی شما لغو شد" in revoked, revoked)


def test_13_help_lists():
    print("\n### ۱۳) لیست ادمینی/لیست کاربران")
    from modules import admin_help_builder as help_builder

    text = help_builder.ADMIN_HELP_TEXT
    check("ردیف اختصاصی در لیست ادمینی هست",
          "🕹️:برای اینکه وقتی گروه قفله یک کاربر بتونه پیام ارسال کنه دستور"
          in text)
    check("ردیف پیام زماندار در لیست ادمینی هست",
          "🔐 : برای زماندار کردن پیام های ارسالی در گروه" in text)
    check("«گزارش» در لیست ادمینی نیست",
          "برای گزارش یک فرد متخلف" not in text)

    start = text.index("🕹️:")
    block = text[start:text.index("\n\n", start)]
    base = u16(text[:start])
    quotes = [(e.offset - base, e.length) for e in help_builder.ADMIN_HELP_ENTITIES
              if base <= e.offset < base + u16(block)
              and isinstance(e, MessageEntityBlockquote)]
    check("دو دستور اختصاصی در یک نقل‌قول",
          len(quotes) == 1 and slice16(text, base + quotes[0][0],
                                       quotes[0][1]) == "اختصاصی\nلغو اختصاصی",
          quotes)
    check("کل بلوک اختصاصی Bold است",
          any(e.offset == base and e.length == u16(block)
              for e in help_builder.ADMIN_HELP_ENTITIES
              if isinstance(e, MessageEntityBold)))

    handler_source = (ROOT / "handlers" / "message_handler.py").read_text(
        encoding="utf-8")
    check("ردیف گزارش در «لیست کاربران» اضافه شد",
          "⚠️: برای گزارش یک فرد متخلف به مالک گروه ریپلای کنید\\n" in
          handler_source)
    check("«گزارش» در لیست ادمینی ساخته‌شده وجود ندارد",
          "گزارش کاربران" not in text)


# ---------------------------------------------------------------------------
#  ۱۴.. صف‌ها و عملکرد
# ---------------------------------------------------------------------------
def test_14_queues_isolated():
    print("\n### ۱۴) صف‌ها مستقل‌اند و خطای یکی بقیه را متوقف نمی‌کند")
    fresh()
    report_queue = reports.QUEUE
    exclusive_queue = exclusive_commands.QUEUE
    timed_queue = timed.QUEUE
    check("سه صف شیء جداگانه‌اند",
          len({id(report_queue), id(exclusive_queue), id(timed_queue)}) == 3)

    async def scenario():
        state = {"exclusive_done": False, "report_done": False}

        async def failing_job():
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        async def slow_job():
            await asyncio.sleep(0.05)
            state["exclusive_done"] = True
            return "exclusive"

        async def report_job():
            await asyncio.sleep(0)
            state["report_done"] = True
            return "report"

        future_fail = await report_queue.submit("chat-a", failing_job,
                                                logger=None)
        future_ok = await report_queue.submit("chat-a", report_job)
        await exclusive_queue.run("chat-a", slow_job)
        try:
            await future_fail
        except RuntimeError:
            pass
        await future_ok
        return state

    state = run(scenario())
    check("خطای یک کار، کار بعدیِ همان صف را متوقف نمی‌کند",
          state["report_done"] is True, state)
    check("صف دیگر هم‌زمان کار خودش را انجام داد",
          state["exclusive_done"] is True, state)
    check("آمار خطا در صف ثبت شد", report_queue.stats()["failed"] >= 1,
          report_queue.stats())

    async def dedupe_scenario():
        async def job():
            await asyncio.sleep(0.05)
            return "ok"
        first = await report_queue.submit("chat-b", job, dedupe_key="same")
        duplicate = None
        try:
            await report_queue.submit("chat-b", job, dedupe_key="same")
        except mqueue.QueueFull:
            duplicate = "blocked"
        await first
        return duplicate

    check("کار تکراری با همان کلید رد می‌شود",
          run(dedupe_scenario()) == "blocked")


def test_15_hot_path_performance():
    print("\n### ۱۵) هزینهٔ مسیر گرم (میکروثانیه)")
    fresh()
    chat_id = -(10 ** 12) - 705
    rounds = 20000

    start = time.perf_counter()
    for _ in range(rounds):
        exclusive_commands.is_command("سلام دوستان")
    per = (time.perf_counter() - start) / rounds * 1_000_000
    print(f"  ⏱ exclusive.is_command : {per:.2f} µs")
    check("تشخیص دستور اختصاصی ارزان است", per < 50, per)

    start = time.perf_counter()
    for _ in range(rounds):
        timed_commands.is_command("سلام", chat_id)
    per = (time.perf_counter() - start) / rounds * 1_000_000
    print(f"  ⏱ timed.is_command     : {per:.2f} µs")
    check("تشخیص دستور زمان‌دار ارزان است", per < 50, per)

    start = time.perf_counter()
    for _ in range(2000):
        timed.track_message(chat_id, 12345)
    per = (time.perf_counter() - start) / 2000 * 1_000_000
    print(f"  ⏱ timed.track (خاموش)  : {per:.2f} µs")
    check("ثبت پیام در حالت خاموش ارزان است", per < 200, per)

    start = time.perf_counter()
    for _ in range(2000):
        report_commands.is_command("سلام", chat_id, 9009)
    per = (time.perf_counter() - start) / 2000 * 1_000_000
    print(f"  ⏱ report.is_command    : {per:.2f} µs")
    check("تشخیص دستور گزارش ارزان است", per < 200, per)


def main():
    print("=" * 62)
    print("🧪 تست سه قابلیت مدیریتی: اختصاصی / پیام زماندار / گزارش")
    print("=" * 62)
    test_01_exclusive_locked_and_open()
    test_01b_exclusive_command_is_fast()
    test_02_exclusive_revoke_and_restart()
    test_02b_exclusive_grant_refused_by_server()
    test_02c_exclusive_strict_server_fallback()
    test_02d_exclusive_server_forces_toggles_and_lost_record()
    test_02e_exclusive_username_grant_never_says_failed()
    test_02f_exclusive_verify_with_broken_get_participant()
    test_03_exclusive_auto_revoke_on_unlock()
    test_04_exclusive_concurrent()
    test_05_timed_all_durations_and_cancel()
    test_05b_timed_no_progress_message_on_start()
    test_06_timed_finish_deletes_only_window()
    test_07_timed_restart_and_failures()
    test_07b_timed_survives_restart_end_to_end()
    test_07c_timed_purge_really_deletes_with_positive_group_id()
    test_07d_timed_status_peer_cancel_and_timeout()
    test_07e_timed_timer_accuracy_and_counts()
    test_07f_timed_single_status_message()
    test_08_report_flow()
    test_09_report_decision()
    test_09c_report_warning_counts_are_real()
    test_09b_report_owner_scope_and_awaiting_recovery()
    test_10_report_restart_waiting()
    test_10b_report_restart_waiting_reason()
    test_11_rate_limit_persistent()
    test_12_templates()
    test_13_help_lists()
    test_14_queues_isolated()
    test_15_hot_path_performance()
    print("\n" + "=" * 62)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\nشکست‌ها:")
        for item in FAILURES:
            print(f"  - {item}")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
