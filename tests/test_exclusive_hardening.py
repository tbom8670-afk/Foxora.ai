# -*- coding: utf-8 -*-
"""🧪 تست‌های رگرسیونِ «سخت‌سازیِ اختصاصی» — یک تست برای هر ایرادِ گزارش‌شده.

    PYTHONPATH=. python3 tests/test_exclusive_hardening.py

هر بخش به شمارهٔ ایراد در گزارشِ کارفرما اشاره دارد:

۱  پریست ``none``/موفقیتِ کاذب/عزلِ کاذب
۲  تأییدِ سه‌حالته با backoff، بدون RPC تغییردهنده در حالت نامعلوم
۳  ``get_participants`` (لیست، نه async-for) + peer واقعیِ کانال
۴  خطاها ⇒ پیامِ فارسیِ درست، هیچ «موفقیت» روی استثنا
۵  پاس دادن ``entity`` در کل مسیر (بدون resolveِ دوباره)
۶  ``AlreadyExclusive`` بعد از بررسیِ واقعیِ سرور
۷  ``rpc.call`` با factory (بدون RuntimeErrorِ coroutine مصرف‌شده)
۸  ``revoke`` ⇒ بدون mute دائمی
۹  پیش‌بررسیِ حقوقِ خودِ ربات (``add_admins``)
۱۰ فلگ‌های واقعیِ ``ChatAdminRights``، ``user_peer``، خطای peer غیرکانال،
   کشِ یکپارچهٔ قفل
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_INSTANCE", "hardening")
os.environ.setdefault("BOT_HOME", tempfile.mkdtemp(prefix="foxora-harden-"))

from splusthon.tl import types                                   # noqa: E402
from splusthon.tl.types import ChatAdminRights, ChatBannedRights  # noqa: E402

from modules import group_storage                                # noqa: E402
from modules.group_actions import text_is_banned                 # noqa: E402
from modules.manage_features import (exclusive, exclusive_commands,  # noqa: E402
                                     peers, rpc, storage)
from modules.manage_features import texts as mtexts              # noqa: E402

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


# ---------------------------------------------------------------------------
#  فیک‌ها
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self):
        self.infos, self.errors = [], []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))

    def joined(self):
        return "\n".join(self.infos + self.errors)


class User:
    def __init__(self, user_id, username=None):
        self.id = user_id
        self.user_id = user_id
        self.username = username
        self.first_name = "کاربر"
        self.last_name = None
        self.access_hash = 4242


class Peer:
    def __init__(self, channel_id=777, access_hash=12345):
        self.channel_id = channel_id
        self.access_hash = access_hash


class Chat:
    def __init__(self, chat_id=-(10 ** 12) - 900, locked=True, bot_can_add=True):
        self.chat_id = chat_id
        self.locked = locked
        self.bot_can_add = bot_can_add
        self.admins = {}                # user_id → ChatAdminRights
        self.restrictions = {}          # user_id → ChatBannedRights
        self.edit_admin_calls = []
        self.edit_banned_calls = []
        self.default_rights = ChatBannedRights(
            until_date=None, send_messages=locked, send_plain=locked)

    def snapshot(self):
        return self.default_rights


class Client:
    """کلاینتِ ساختگیِ SPlusthon با امکان تزریقِ خطا در هر RPC."""

    def __init__(self, chat, users=None):
        self.chat = chat
        self.users = users or {}
        self.calls = []
        self.bot_id = 999
        self.fail = {}                  # name → تعداد بارهایی که خطا بدهد
        self.always_fail = set()
        self.lie_not_admin = 0          # GetParticipant: کاربر را ادمین نشان نده
        self.get_participant_delay = 0.0
        self.participants_is_list = True

    def _maybe_fail(self, name):
        if name in self.always_fail:
            raise ConnectionError(f"{name} down")
        if self.fail.get(name):
            self.fail[name] -= 1
            raise ConnectionError(f"{name} temporary")

    async def get_me(self):
        return User(self.bot_id, "foxora_bot")

    async def get_entity(self, target):
        self.calls.append("get_entity")
        if isinstance(target, int) and target in self.users:
            return self.users[target]
        if isinstance(target, int):
            for user in self.users.values():
                if user.id == target:
                    return user
        if isinstance(target, str):
            for user in self.users.values():
                if user.username == target.lstrip("@"):
                    return user
        raise ValueError(f"Could not find the input entity for {target}")

    async def get_input_entity(self, target):
        self.calls.append("get_input_entity")
        if isinstance(target, int) and target == self.chat.chat_id:
            return Peer()
        raise ValueError("nope")

    async def __call__(self, request, *args, **kwargs):
        name = type(request).__name__
        self.calls.append(name)
        self._maybe_fail(name)

        if name == "GetFullChannelRequest":
            default = self.chat.default_rights
            return type("Full", (), {
                "full_chat": type("FC", (), {"participants_count": 4})(),
                "chats": [type("C", (), {"id": 1,
                                         "default_banned_rights": default})()],
            })()

        if name == "EditAdminRequest":
            self.chat.edit_admin_calls.append(request)
            user_id = peers.user_identifier(request.user_id)
            rights = request.admin_rights
            flags = [flag for flag in peers.user_identifier.__doc__ or ""]
            values = {flag: getattr(rights, flag, None)
                      for flag in _admin_flags()}
            if all(value is None for value in values.values()):
                self.chat.admins.pop(user_id, None)      # demote
                return True
            if all(value is False for value in values.values()) \
                    and not any(values.values()):
                # «ادمینِ بی‌حق» را سرورِ سروش نمی‌پذیرد ⇒ بی‌اثر
                return True
            self.chat.admins[user_id] = rights
            return True

        if name == "EditBannedRequest":
            self.chat.edit_banned_calls.append(request)
            user_id = peers.user_identifier(request.participant)
            self.chat.restrictions[user_id] = request.banned_rights
            return True

        if name == "GetParticipantRequest":
            if self.get_participant_delay:
                await asyncio.sleep(self.get_participant_delay)
            user_id = peers.user_identifier(request.participant)
            if user_id == self.bot_id:
                rights = ChatAdminRights(**{
                    flag: (True if flag == "add_admins"
                           and self.chat.bot_can_add else False)
                    for flag in _admin_flags()})
                return type("P", (), {
                    "participant": type("ChannelParticipantAdmin", (), {
                        "user_id": self.bot_id,
                        "admin_rights": rights})()})()
            is_admin = user_id in self.chat.admins and not self.lie_not_admin
            if is_admin:
                return type("P", (), {
                    "participant": type("ChannelParticipantAdmin", (), {
                        "user_id": user_id,
                        "admin_rights": self.chat.admins[user_id],
                        "banned_rights": ChatBannedRights(until_date=None),
                    })()})()
            return type("P", (), {
                "participant": type("ChannelParticipant", (), {
                    "user_id": user_id,
                    "banned_rights": self.chat.restrictions.get(
                        user_id, ChatBannedRights(until_date=None,
                                                  send_messages=True,
                                                  send_plain=True)),
                })()})()

        return True

    async def get_participants(self, entity, limit=None, *, filter=None,
                               **kwargs):
        self.calls.append("get_participants")
        self._maybe_fail("get_participants")
        rows = []
        for user_id, rights in list(self.chat.admins.items()):
            member = User(user_id)
            member.participant = type("ChannelParticipantAdmin", (), {
                "admin_rights": rights})()
            rows.append(member)
        member = User(self.bot_id)
        member.participant = type("ChannelParticipantAdmin", (), {
            "admin_rights": ChatAdminRights(
                **{flag: (flag == "add_admins" and self.chat.bot_can_add)
                   for flag in _admin_flags()})})()
        rows.append(member)
        # SPlusthon واقعی: ``TotalList`` (لیست)، نه async-iterator.
        if self.participants_is_list:
            return list(rows)
        return _AsyncIter(rows)


class _AsyncIter:
    def __init__(self, rows):
        self.rows = list(rows)

    def __aiter__(self):
        async def generator():
            for row in self.rows:
                yield row
        return generator()


def _admin_flags():
    import inspect
    params = inspect.signature(ChatAdminRights.__init__).parameters
    return [name for name in params if name != "self"]


class Actions:
    def __init__(self, chat):
        self.chat = chat

    async def _group_peer(self, chat_id):
        return Peer()

    async def _fetch_default_banned_rights(self, chat_id, peer=None):
        if peer is None:
            peer = await self._group_peer(chat_id)
        return self.chat.snapshot(), "server"


class Bot:
    def __init__(self, chat, users=None):
        self.chat = chat
        self.client = Client(chat, users)
        self.logger = Logger()
        self.group_actions = Actions(chat)
        self.bot_account_id = 999
        self.reply_input_peer_cache = {}


def fresh(chat_id=-(10 ** 12) - 900):
    # پاک‌سازیِ کش‌ها و رکوردها بین تست‌ها
    peers.clear_caches()
    exclusive_commands.reset_all()
    for key in list(storage.EXCLUSIVE_STORAGE.load().get("groups", {})):
        exclusive.forget_group(int(key) if str(key).lstrip("-").isdigit()
                               else chat_id)


def make(chat_id=-(10 ** 12) - 900, locked=True, users=None, bot_can_add=True):
    fresh(chat_id)
    chat = Chat(chat_id, locked=locked, bot_can_add=bot_can_add)
    bot = Bot(chat, users or {})
    group_storage.set_group_owner(chat_id, 4242)
    return chat, bot


# ---------------------------------------------------------------------------
#  ۱ — پریست: فقط edit_only، بدون پریستِ «none»، بدون موفقیتِ کاذب
# ---------------------------------------------------------------------------
def test_01_preset_is_send_only():
    print("\n### ۱) پریستِ ارتقا: فقط حقِ ارسال (بدون پریستِ none)")
    fresh()
    check("پریستِ «none» وجود ندارد",
          "none" not in dict(exclusive.ADMIN_RIGHT_PRESETS), 
          exclusive.ADMIN_RIGHT_PRESETS)
    rights = exclusive.admin_rights_for(exclusive.PRIMARY_PRESET)
    flags = {name: getattr(rights, name) for name in _admin_flags()}
    check("همهٔ فلگ‌ها صریح و False هستند (هیچ None)",
          all(value is False or value is True for value in flags.values()), flags)
    check("تنها فلگ روشن «edit_messages» است",
          [name for name, value in flags.items() if value] == ["edit_messages"],
          flags)
    for sensitive in ("add_admins", "ban_users", "delete_messages",
                      "invite_users", "pin_messages", "change_info",
                      "post_messages", "anonymous"):
        check(f"حقِ {sensitive} داده نمی‌شود", flags.get(sensitive) is False,
              flags.get(sensitive))
    check("SENSITIVE_ADMIN_FLAGS شاملِ add_admins/ban_users است",
          {"add_admins", "ban_users", "delete_messages"} <=
          set(exclusive.SENSITIVE_ADMIN_FLAGS), exclusive.SENSITIVE_ADMIN_FLAGS)


# ---------------------------------------------------------------------------
#  ۲ — تأیید سه‌حالته با backoff
# ---------------------------------------------------------------------------
def test_02_tristate_verification():
    print("\n### ۲) تأیید سه‌حالته: True / False / None")
    chat, bot = make()
    run(exclusive.grant(bot, chat.chat_id, 501, actor_id=4242,
                        entity=User(501)))
    check("کاربر ادمین شد", 501 in chat.admins, chat.admins)
    check("تأییدِ سریع: True",
          run(exclusive.promotion_state(bot, chat.chat_id, 501)) is True)

    # سرور می‌گوید ادمین نیست ⇒ False (و نه True)
    bot.client.lie_not_admin = 99      # GetParticipant «ادمین نیست» می‌گوید
    honest_admin = chat.admins.pop(501)   # فهرستِ ادمین‌ها هم صادق باشد
    check("هر دو مسیر «ادمین نیست» ⇒ False",
          run(exclusive.promotion_state(bot, chat.chat_id, 501,
                                        attempts=1, backoff=(0.0,))) is False)
    chat.admins[501] = honest_admin

    # خواندن همیشه خطا ⇒ None (نامعلوم)، نه True و نه False
    bot.client.always_fail = {"GetParticipantRequest", "get_participants"}
    check("خطای کاملِ خواندن ⇒ None (نامعلوم)",
          run(exclusive.promotion_state(bot, chat.chat_id, 501)) is None)
    check("backoff چند مرحله‌ای است (نه ۰٫۳ ثانیه)",
          len(exclusive.VERIFY_BACKOFF) >= 3
          and max(exclusive.VERIFY_BACKOFF) >= 1.0, exclusive.VERIFY_BACKOFF)

    # در حالت نامعلوم هیچ RPC تغییردهنده‌ای زده نمی‌شود
    chat2, bot2 = make(chat_id=-(10 ** 12) - 901)
    run(exclusive.grant(bot2, chat2.chat_id, 502, entity=User(502)))
    before = len(chat2.edit_admin_calls)
    bot2.client.always_fail = {"GetParticipantRequest", "get_participants"}
    record = run(exclusive.complete_verification(bot2, chat2.chat_id, 502))
    check("در حالت نامعلوم هیچ EditAdmin اضافه‌ای زده نشد",
          len(chat2.edit_admin_calls) == before, len(chat2.edit_admin_calls))
    check("رکورد در «در انتظار ارتقا» می‌ماند (برای بازیابی)",
          record["state"] == exclusive.PENDING_GRANT_FLAG, record)


# ---------------------------------------------------------------------------
#  ۳ — get_participants لیست است + peer واقعی
# ---------------------------------------------------------------------------
def test_03_participants_list_and_channel_peer():
    print("\n### ۳) فهرستِ ادمین‌ها: لیست (نه async-for) و peer کانال")
    chat, bot = make(chat_id=-(10 ** 12) - 902)
    chat.admins[503] = exclusive.admin_rights_for()
    state = run(exclusive._admin_in_participants_list(bot, chat.chat_id, 503))
    check("پیمایشِ لیست بدون TypeError و نتیجهٔ True", state is True, state)
    check("get_participants صدا زده شد", "get_participants" in bot.client.calls,
          bot.client.calls)
    check("تأییدِ دوم برای کاربرِ غیرادمین ⇒ False",
          run(exclusive._admin_in_participants_list(bot, chat.chat_id, 504))
          is False)
    bot.client.participants_is_list = False
    check("شکل async-iterator هم پشتیبانی می‌شود",
          run(exclusive._admin_in_participants_list(bot, chat.chat_id, 503))
          is True)


# ---------------------------------------------------------------------------
# ۴ — نگاشتِ خطا به پیامِ فارسی، بدون موفقیتِ کاذب
# ---------------------------------------------------------------------------
class Event:
    def __init__(self, text="اختصاصی", sender=None, reply_to=None):
        self.text = text
        self.sender = sender or User(4242, "owner")
        self.sender_id = self.sender.id
        self.chat_id = None
        self.is_private = False
        self.reply_to = reply_to
        self.replies = []

    async def reply(self, text, formatting_entities=None):
        self.replies.append((text, list(formatting_entities or [])))
        return type("M", (), {"id": 1})()


def _say(bot, chat_id, text, sender=None):
    event = Event(text, sender)
    run(exclusive_commands.handle(bot, event, chat_id, event.sender.id,
                                  event.sender, text, bot.logger))
    return " ".join(t2 for t2, _e in event.replies)


def test_04_error_mapping():
    print("\n### ۴) هر خطا یک پیامِ فارسیِ درست دارد؛ موفقیت فقط با RPCِ بی‌خطا")
    chat, bot = make(chat_id=-(10 ** 12) - 903, locked=False)
    body = _say(bot, chat.chat_id, "اختصاصی 601")
    check("گروهِ باز ⇒ پیامِ «قفل نیست»",
          mtexts.EXCLUSIVE_NOT_LOCKED in body, body)

    chat, bot = make(chat_id=-(10 ** 12) - 904)
    bot.client.always_fail = {"EditAdminRequest"}
    body = _say(bot, chat.chat_id, "اختصاصی 602", )
    check("سرور ارتقا را رد کرد ⇒ پیامِ شکست (نه موفقیت)",
          mtexts.EXCLUSIVE_FAILED in body
          and mtexts.EXCLUSIVE_GRANTED not in body, body)
    check("پروندهٔ کاملِ خطا در لاگ ثبت شد",
          "EXCLUSIVE GRANT REJECTED" in bot.logger.joined(),
          bot.logger.joined()[-300:])

    chat, bot = make(chat_id=-(10 ** 12) - 905, bot_can_add=False)
    body = _say(bot, chat.chat_id, "اختصاصی 603")
    check("ربات بدون حقِ افزودن ادمین ⇒ پیامِ روشن",
          mtexts.EXCLUSIVE_BOT_NO_RIGHTS in body, body)
    check("در این حالت هیچ EditAdmin زده نشد", not chat.edit_admin_calls,
          chat.edit_admin_calls)

    chat, bot = make(chat_id=-(10 ** 12) - 906)
    body = _say(bot, chat.chat_id, "اختصاصی @ghost_user_404")
    check("کاربر پیدا نشد ⇒ پیامِ «کاربر پیدا نشد»",
          mtexts.EXCLUSIVE_NO_TARGET_ENTITY in body, body)

    chat, bot = make(chat_id=-(10 ** 12) - 907)
    body = _say(bot, chat.chat_id, "اختصاصی 604")
    check("مسیرِ موفق: پیامِ موفقیت",
          mtexts.EXCLUSIVE_GRANTED in body, body)


# ---------------------------------------------------------------------------
#  ۵ — entity در کل مسیر
# ---------------------------------------------------------------------------
def test_05_entity_passed_through():
    print("\n### ۵) entity در کل مسیر پاس داده می‌شود (بدون resolveِ دوباره)")
    chat, bot = make(chat_id=-(10 ** 12) - 908, users={605: User(605, "t5")})
    entity = bot.client.users[605]
    run(exclusive.grant(bot, chat.chat_id, 605, entity=entity))
    check("بدون هیچ get_entity کاربر ادمین شد",
          "get_entity" not in bot.client.calls, bot.client.calls)
    check("کاربر ادمین شد (entity به EditAdmin رسید)", 605 in chat.admins,
          chat.admins)
    request = chat.edit_admin_calls[-1]
    check("آرگومانِ EditAdmin همان entity است، نه شناسهٔ عددی",
          request.user_id is entity, type(request.user_id))


# ---------------------------------------------------------------------------
#  ۶ — AlreadyExclusive فقط بعد از بررسیِ سرور
# ---------------------------------------------------------------------------
def test_06_already_exclusive_requires_server_state():
    print("\n### ۶) «قبلاً اختصاصی است» فقط وقتی سرور هم بگوید")
    chat, bot = make(chat_id=-(10 ** 12) - 909)
    run(exclusive.grant(bot, chat.chat_id, 606, entity=User(606)))
    run(exclusive.complete_verification(bot, chat.chat_id, 606))
    try:
        run(exclusive.grant(bot, chat.chat_id, 606, entity=User(606)))
        raised = False
    except exclusive.AlreadyExclusive:
        raised = True
    check("رکورد active + سرور «ادمین است» ⇒ AlreadyExclusive", raised)

    # رکورد می‌گوید active ولی سرور می‌گوید ادمین نیست (کاربر دستی demote شد)
    chat.admins.pop(606, None)
    record = run(exclusive.grant(bot, chat.chat_id, 606, entity=User(606)))
    check("رکورد کهنه دوباره ارتقا داده شد", 606 in chat.admins, chat.admins)
    check("رکورد نهایی active است", record["state"] == "active", record)

    # در حالت نامعلوم هیچ تغییری نمی‌دهیم و «موفق» نمی‌گوییم
    chat.admins.pop(606, None)
    bot.client.always_fail = {"GetParticipantRequest", "get_participants"}
    before = len(chat.edit_admin_calls)
    try:
        run(exclusive.grant(bot, chat.chat_id, 606, entity=User(606)))
        unknown_raised = False
    except exclusive.AlreadyExclusive:
        unknown_raised = True
    check("حالت نامعلوم ⇒ هیچ EditAdmin و اعلامِ «قبلاً هست»",
          unknown_raised and len(chat.edit_admin_calls) == before,
          len(chat.edit_admin_calls) - before)


# ---------------------------------------------------------------------------
#  ۷ — rpc.call با factory
# ---------------------------------------------------------------------------
def test_07_rpc_factory():
    print("\n### ۷) rpc.call: factory قابل تکرار، coroutine آماده فقط یک تلاش")
    fresh()
    attempts = {"count": 0}

    async def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("transient")
        return "ok"

    check("factory با خطای گذرا در نهایت موفق می‌شود",
          run(rpc.call(flaky, max_retry=3)) == "ok", attempts)
    check("factory سه بار صدا زده شد", attempts["count"] == 3, attempts)

    state = {"used": 0}

    async def once():
        state["used"] += 1
        raise ConnectionError("boom")

    coroutine = once()
    reuse_error = False
    try:
        run(rpc.call(coroutine, max_retry=3))
    except ConnectionError:
        pass
    except RuntimeError as error:
        reuse_error = "reuse" in str(error).lower()
    check("coroutine آماده بدون RuntimeErrorِ «reuse»", not reuse_error)
    check("coroutine آماده فقط یک بار اجرا شد", state["used"] == 1, state)

    # مسیر واقعیِ اختصاصی: چند تلاش روی GetParticipant بدون RuntimeError
    chat, bot = make(chat_id=-(10 ** 12) - 910)
    bot.client.fail = {"GetParticipantRequest": 2}
    participant, error = run(exclusive.read_participant(
        bot, chat.chat_id, 607, attempts=3, backoff=(0.0, 0.0, 0.0)))
    check("خواندنِ چندتلاشی بدون RuntimeError کار کرد",
          participant is not None and error is None, error)


# ---------------------------------------------------------------------------
#  ۸ — لغو بدون mute دائمی
# ---------------------------------------------------------------------------
def test_08_revoke_never_mutes():
    print("\n### ۸) لغو: demote بدون گذاشتنِ mute دائمی")
    chat, bot = make(chat_id=-(10 ** 12) - 911)
    run(exclusive.grant(bot, chat.chat_id, 608, entity=User(608)))
    run(exclusive.revoke(bot, chat.chat_id, 608, entity=User(608)))
    check("ادمین نیست", 608 not in chat.admins, chat.admins)
    check("هیچ محدودیتِ شخصی گذاشته نشد",
          608 not in chat.restrictions
          or not text_is_banned(chat.restrictions[608]), chat.restrictions)

    # اگر از قبل محدودیتِ شخصی داشت ⇒ پاک می‌شود (Back to normal)
    chat.restrictions[609] = ChatBannedRights(until_date=None,
                                              send_messages=True,
                                              send_plain=True)
    run(exclusive.grant(bot, chat.chat_id, 609, entity=User(609)))
    run(exclusive.revoke(bot, chat.chat_id, 609, entity=User(609)))
    check("محدودیتِ شخصیِ قبلی پاک شد (عضو عادیِ سالم)",
          not text_is_banned(chat.restrictions.get(609)), chat.restrictions)
    check("پاک‌سازی با banned_rights همه-False بود",
          not any(bool(getattr(chat.restrictions[609], flag, False))
                  for flag in exclusive.ALL_ADMIN_FLAGS
                  if hasattr(chat.restrictions[609], flag)),
          chat.restrictions[609])


# ---------------------------------------------------------------------------
#  ۹ — حقوقِ خودِ ربات
# ---------------------------------------------------------------------------
def test_09_bot_rights_precheck():
    print("\n### ۹) پیش‌بررسیِ حقوقِ ربات (add_admins)")
    chat, bot = make(chat_id=-(10 ** 12) - 912, bot_can_add=True)
    check("رباتِ مجاز ⇒ True",
          run(exclusive.ensure_bot_can_promote(bot, chat.chat_id)) is True)
    chat, bot = make(chat_id=-(10 ** 12) - 913, bot_can_add=False)
    try:
        run(exclusive.ensure_bot_can_promote(bot, chat.chat_id))
        raised = False
    except exclusive.BotLacksRights:
        raised = True
    check("رباتِ بی‌حق ⇒ BotLacksRights (قبل از هر ارتقا)", raised)
    check("لاگ شاملِ وضعیتِ حقوقِ ربات است",
          "EXCLUSIVE BOT RIGHTS" in bot.logger.joined(),
          bot.logger.joined()[-200:])

    # اگر خواندنِ حقوق ممکن نباشد، جلوی کار گرفته نمی‌شود (نامعلوم ≠ بی‌حق)
    chat, bot = make(chat_id=-(10 ** 12) - 914)
    bot.client.always_fail = {"GetParticipantRequest", "get_participants"}
    check("حقوقِ نامعلوم ⇒ None (تلاش ادامه می‌یابد)",
          run(exclusive.ensure_bot_can_promote(bot, chat.chat_id)) is None)


# ---------------------------------------------------------------------------
#  ۱۰ — اصلاحات کوچک
# ---------------------------------------------------------------------------
def test_10_small_fixes():
    print("\n### ۱۰) فلگ‌های واقعی، user_peer، peer غیرکانال، کشِ واحد")
    installed = set(_admin_flags())
    check("ALL_ADMIN_FLAGS دقیقاً فلگ‌های نسخهٔ نصب‌شده است (فیلترشده)",
          set(exclusive.ALL_ADMIN_FLAGS) == installed,
          (set(exclusive.ALL_ADMIN_FLAGS) ^ installed))
    check("post_stories/edit_stories (اگر باشند) هم False می‌شوند",
          all(getattr(exclusive.send_only_admin_rights(), flag) is False
              for flag in installed if "stories" in flag),
          [flag for flag in installed if "stories" in flag])

    # user_peer: تفکیکِ خطای فنی از بنِ واقعی
    fresh()
    chat, bot = make(chat_id=-(10 ** 12) - 915)
    from modules import banned_storage
    original = banned_storage.is_banned
    banned_storage.is_banned = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("sqlite is locked"))
    try:
        peer = run(exclusive.user_peer(bot, chat.chat_id, 610,
                                       entity=User(610)))
        ok = peer is not None
        log_ok = "EXCLUSIVE BAN CHECK FAILED (technical" in bot.logger.joined()
    finally:
        banned_storage.is_banned = original
    check("خطای فنیِ ban ⇒ ادامهٔ کار (fail-open) با لاگ", ok and log_ok,
          bot.logger.joined()[-200:])

    banned_storage.is_banned = lambda *a, **k: True
    try:
        run(exclusive.user_peer(bot, chat.chat_id, 611, entity=User(611)))
        raised = False
    except exclusive.ExcludedUser:
        raised = True
    finally:
        banned_storage.is_banned = original
    check("بنِ واقعی ⇒ ExcludedUser", raised)

    check("شناسهٔ رشته‌ای نرمال می‌شود (رشتهٔ عددی ⇒ عدد)",
          peers.user_identifier(" 612 ") == 612
          and peers.user_identifier("@613") == 613
          and peers.user_identifier("ali") is None,
          (peers.user_identifier(" 612 "), peers.user_identifier("@613"),
           peers.user_identifier("ali")))

    # peer غیرکانال ⇒ خطای واضح
    fresh()
    chat, bot = make(chat_id=-(10 ** 12) - 916)
    bot.reply_input_peer_cache = {chat.chat_id: _PlainPeer()}
    original_actions = exclusive._actions
    exclusive._actions = lambda _bot: type("A", (), {
        "_group_peer": staticmethod(_plain_chat_peer),
        "_fetch_default_banned_rights": staticmethod(
            lambda _self, *a, **k: None),
    })()
    try:
        run(exclusive._require_channel(bot, chat.chat_id))
        raised = False
    except exclusive.TargetChanged as error:
        raised = "not a channel" in str(error)
    finally:
        exclusive._actions = original_actions
    check("peer غیرکانال ⇒ TargetChanged با پیامِ روشن", raised)

    # کشِ قفل یکپارچه
    fresh()
    chat, bot = make(chat_id=-(10 ** 12) - 917, locked=True)
    locked = run(exclusive.is_group_locked(bot, chat.chat_id))
    first_calls = len(bot.client.calls)
    again = run(exclusive.is_group_locked(bot, chat.chat_id))
    check("کشِ قفل مشترک است (بارِ دوم هیچ RPC)",
          locked is True and again is True
          and len(bot.client.calls) == first_calls, bot.client.calls)
    check("همان کش در لایهٔ دستور دیده می‌شود",
          exclusive_commands.lock_state_cached(chat.chat_id) is True)
    check("LOCK_CHECK_INTERVAL همان TTL کش است",
          exclusive_commands.LOCK_CHECK_INTERVAL == peers.LOCK_CACHE_TTL)


class _PlainPeer:
    """پیرِ چتِ معمولی (نه کانال): ``channel_id`` ندارد."""

    def __init__(self):
        self.chat_id = 42


async def _plain_chat_peer(chat_id):
    return _PlainPeer()


def main():
    test_01_preset_is_send_only()
    test_02_tristate_verification()
    test_03_participants_list_and_channel_peer()
    test_04_error_mapping()
    test_05_entity_passed_through()
    test_06_already_exclusive_requires_server_state()
    test_07_rpc_factory()
    test_08_revoke_never_mutes()
    test_09_bot_rights_precheck()
    test_10_small_fixes()

    print("\n" + "=" * 70)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\n".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
