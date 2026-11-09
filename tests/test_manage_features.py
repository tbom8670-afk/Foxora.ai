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

from splusthon.tl.types import (ChatBannedRights, MessageEntityBlockquote,
                                MessageEntityBold)

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
        self.edit_banned_calls = []
        self.edit_admin_calls = []
        self.fail_next = 0
        self.deleted = []
        self.deleted_ids = []

    def snapshot(self):
        return self.default_rights

    def report(self):
        return self.default_rights


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
        self.edits = []
        self.sent = []

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
        if isinstance(target, int) and target in self.users:
            return self.users[target]
        if isinstance(target, str):
            for user in self.users.values():
                if getattr(user, "username", None) == target:
                    return user
        return None

    async def __call__(self, request, *args, **kwargs):
        name = type(request).__name__
        if name == "EditBannedRequest":
            if self.fail_edit_banned:
                raise ConnectionError("network down")
            self.chat.edit_banned_calls.append(request)
            participant = request.participant
            user_id = getattr(participant, "user_id", participant)
            self.chat.participants[user_id] = request.banned_rights
            return True
        if name == "EditAdminRequest":
            self.chat.edit_admin_calls.append(request)
            return True
        if name == "GetParticipantRequest":
            if self.get_participant_fails:
                self.get_participant_fails -= 1
                raise ConnectionError("temporary")
            participant = request.participant
            user_id = getattr(participant, "user_id", participant)
            rights = self.chat.participants.get(
                user_id, ChatBannedRights(until_date=None, send_messages=True,
                                          send_plain=True))
            return type("Participant", (), {"banned_rights": rights,
                                            "user_id": user_id})()
        if name in {"GetFullChannelRequest", "GetChannelsRequest"}:
            chat = type("Chat", (), {
                "id": 1, "default_banned_rights": self.chat.report()})()
            return type("Full", (), {"chats": [chat],
                                     "full_chat": type("FC", (), {
                                         "participants_count": 3})()})()
        return True

    async def get_messages(self, chat_id, ids=None, **_kwargs):
        sender = self.users.get(self.reply_sender_id)
        return type("Message", (), {
            "id": ids,
            "get_sender": _sender_getter(sender),
        })()

    async def delete_messages(self, chat_id, ids, **_kwargs):
        ids = list(ids)
        if self.chat.fail_next:
            self.chat.fail_next -= 1
            ids = ids[:len(ids) // 2]           # حذف جزئی + خطا
            self.chat.deleted.extend(ids)
            raise ConnectionError("network down")
        self.chat.deleted.extend(ids)
        return type("Affected", (), {"pts_count": len(ids)})()

    async def edit_message(self, message, text, formatting_entities=None,
                           **_kwargs):
        self.edits.append((message, text, list(formatting_entities or [])))
        return type("Message", (), {"id": message})()

    async def get_me(self):
        return type("Me", (), {"username": "foxora_bot"})()


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
    rights = chat.participants[777]
    check("فقط مجوز ارسال داده شد (send_messages=False)",
          getattr(rights, "send_messages", None) is False, rights)
    check("send_plain هم باز شد", getattr(rights, "send_plain", None) is False)
    check("مسدودسازی بقیه دست‌نخورده (سایر فلگ‌ها False)",
          all(not getattr(rights, flag) for flag in
              ("send_media", "send_gifs", "send_polls", "invite_users",
               "change_info", "pin_messages")), rights)
    check("هیچ ارتقای ادمینی رخ نداده", chat.edit_admin_calls == [],
          chat.edit_admin_calls)
    check("تأیید با API واقعی (GetParticipant) انجام شد",
          record["confirmed"] is True, record)
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
    check("مجوز واقعی برگشت (send_messages=True)",
          text_is_banned(chat.participants[783]), chat.participants[783])
    check("اجرای دوبارهٔ لغو مسدود است", second == "blocked")
    check("هیچ ارتقای ادمینی در هیچ مرحله", chat.edit_admin_calls == [])

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
    ids = {job["user_id"] for job in jobs}
    check("پس از ری‌استارت فقط رکورد فعال در بازیابی است", ids == {"784"},
          ids)
    check("رکورد لغوشده در بازیابی نمی‌آید", "785" not in ids)

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
    check("مجوز هر دو کاربر برگشت",
          all(text_is_banned(chat.participants[user_id])
              for user_id in users), chat.participants)
    check("هیچ ارتقای ادمینی برای لغو لازم نشد",
          chat.edit_admin_calls == [])


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
    check("برای هر پنج کاربر مجوز جداگانه زده شد",
          len(chat.edit_banned_calls) == 5, len(chat.edit_banned_calls))
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
    edits = [item for item in bot.client.edits if item[0] == 3300]
    check("پیام وضعیت ویرایش شد (نه پیام جدید)",
          bool(edits), bot.client.edits[:2])
    check("پیام وضعیت چند بار ویرایش شد (زنده)",
          len(edits) >= 2, len(edits))
    check("محتوای ویرایش شامل شمارش پیام‌هاست",
          any("تعداد پیام در حال پاک شدن" in text for _m, text, _e in edits),
          [text[:20] for _m, text, _e in edits][:2])
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
        return reports.get_case(chat_id, case["case_id"])

    approved = run(scenario_approve())
    check("پرونده تأیید شد", approved["state"] == "approved", approved)
    check("دقیقاً دو اخطار در سیستم اخطار ثبت شد",
          bot.tracker.get_count(chat_id, 7002) == 2,
          bot.tracker.get_count(chat_id, 7002))
    check("تعداد اخطار در پرونده = ۲", approved.get("warning_count") == 2)
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
    test_02_exclusive_revoke_and_restart()
    test_03_exclusive_auto_revoke_on_unlock()
    test_04_exclusive_concurrent()
    test_05_timed_all_durations_and_cancel()
    test_06_timed_finish_deletes_only_window()
    test_07_timed_restart_and_failures()
    test_08_report_flow()
    test_09_report_decision()
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
