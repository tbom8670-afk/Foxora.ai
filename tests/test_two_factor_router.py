# -*- coding: utf-8 -*-
"""🔗 تستِ یکپارچه: «تأیید دو مرحله‌ای» از مسیرِ واقعیِ روتر.

    PYTHONPATH=. python3 tests/test_two_factor_router.py

برخلاف ``test_two_factor.py`` (که ماژول را مستقیم صدا می‌زند)، این‌جا همان
مسیرِ واقعیِ ``handlers.message_handler.handle_new_message`` اجرا می‌شود تا
ثابت شود:

1. فرمانِ «دو مرحله ای» از روتر عبور می‌کند و کارتِ گام ۱ ارسال می‌شود.
2. دستورِ خطرناک از **ادمین ثبت‌شده** به گیت می‌خورد و به هندلرِ اصلی
   (که در تست با شمارنده مشخص شده) نمی‌رسد.
3. همان دستور از **مالک** درخواستِ رمز می‌سازد و اجرا نمی‌شود.
4. رمزِ درست ⇒ همان دستور با پرچمِ «تأیید شد» اجرا می‌شود (بدون نوشتنِ
   دوبارهٔ دستور).
5. پیامِ ۸ رقمیِ بی‌ربط از مالک (بدون درخواست) به مسیرِ عادی می‌رسد.
6. گروهِ بدون قفل: رفتار دقیقاً مثل قبل (دستور اجرا می‌شود).

هیچ RPC واقعی زده نمی‌شود؛ فقط مسیرِ روتر.
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

os.environ.setdefault("BOT_INSTANCE", "two_factor_router_test")
os.environ.setdefault("BOT_HOME", tempfile.mkdtemp(prefix="foxora-2fa-router-"))

from modules import group_storage                                    # noqa: E402
from modules.manage_features import (storage, texts, two_factor,      # noqa: E402
                                     two_factor_commands)

OWNER = 4242
ADMIN = 7777
CHAT = -(10 ** 12) - 950

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


# ---------------------------------------------------------------------------
#  فیک‌های حداقلیِ روتر
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self):
        self.infos, self.errors = [], []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))

    def everything(self):
        return "\n".join(self.infos + self.errors)


class User:
    def __init__(self, user_id, username=None):
        self.id = user_id
        self.user_id = user_id
        self.username = username
        self.first_name = "کاربر"
        self.last_name = None
        self.bot = False


class Message:
    def __init__(self, message_id, text):
        self.id = message_id
        self.message = text
        self.caption = None
        self.file = None
        self.edit_date = None
        self.reply_to = None


class Event:
    is_private = False

    def __init__(self, chat_id, sender, text, message_id=500):
        self.chat_id = chat_id
        self.sender = sender
        self.sender_id = sender.id
        self.message = Message(message_id, text)
        self.replies = []

    async def reply(self, text, formatting_entities=None, **_kwargs):
        self.replies.append((text, list(formatting_entities or [])))
        return Message(900, text)

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return type("Chat", (), {"id": self.chat_id, "title": "تست"})()

    def text(self):
        return " ".join(t2 for t2, _e in self.replies)


class Client:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, formatting_entities=None,
                           **_kwargs):
        self.sent.append((chat_id, text))
        return Message(len(self.sent), text)

    async def delete_messages(self, peer, ids):
        self.deleted.extend(list(ids))
        return True

    async def get_entity(self, target):
        return User(int(target))

    async def get_messages(self, chat_id, ids=None, **_kwargs):
        return None


class Bot:
    def __init__(self):
        self.client = Client()
        self.logger = Logger()
        self.reply_input_peer_cache = {CHAT: object()}
        self.config_manager = type("Cfg", (), {
            "get": lambda _self, key, default=None: default})()
        self.bot_account_id = 999999
        self.tracker = type("T", (), {
            "increment": lambda _s, c, u: 1,
            "get_count": lambda _s, c, u: 0,
            "should_punish": lambda _s, c, u, threshold=5: False,
            "muted_users": {},
            "reset_count": lambda _s, c, u: None,
        })()
        self.punished_users = set()
        # کمک‌های روتر که مسیر عادی لازم دارد (همه بی‌اثر).
        self.is_spam_locked = lambda *_a, **_k: False
        self.clear_spam_lock = lambda *_a, **_k: None
        self.clear_released_user_state = lambda *_a, **_k: None
        self.outgoing_sender = None
        self.moderation_notification_guard = set()
        self.group_actions = type("GA", (), {
            "lock_group": _noop_async, "unlock_group": _noop_async})()
        self.detector = type("D", (), {
            "is_spam": lambda _s, text, chat_id: (False, "")})()
        self.performance_monitor = None
        self.debug_message_log = lambda *_a, **_k: None
        self.moderation_queue = type("Q", (), {"enqueue": lambda *a, **k: True})()
        self.admin_actions = type("A", (), {})()


class Helpers:
    """جایگزینیِ کمک‌های سنگینِ روتر با نسخهٔ بی‌اثر (فقط برای تست).

    ⚠️ همهٔ جایگزینی‌ها در ``_ORIGINAL`` نگه داشته و در ``restore`` برگردانده
    می‌شوند؛ وگرنه تست‌های بعدیِ همان پروسه (pytest) با ماژولِ دست‌کاری‌شده
    اجرا می‌شدند.
    """

    #: (نامِ ماژول، نامِ ویژگی) → مقدارِ اصلی
    _ORIGINAL = {}

    @classmethod
    def _set(cls, module, name, value):
        key = (getattr(module, "__name__", repr(module)), name)
        if key not in cls._ORIGINAL:
            cls._ORIGINAL[key] = getattr(module, name, _MISSING)
        setattr(module, name, value)

    @classmethod
    def restore(cls, module):
        module_name = getattr(module, "__name__", repr(module))
        for (owner, name), original in list(cls._ORIGINAL.items()):
            if owner != module_name:
                continue
            if original is _MISSING:
                try:
                    delattr(module, name)
                except AttributeError:
                    pass
            else:
                setattr(module, name, original)
            cls._ORIGINAL.pop((owner, name), None)

    @classmethod
    def install(cls, module):
        cls._set(module.admin_tools, "has_admin_permission",
                 lambda chat_id, user_id, username=None:
                 user_id in (OWNER, ADMIN))
        cls._set(module, "_has_group_management_permission",
                 lambda bot, chat_id, user_id, username=None:
                 user_id in (OWNER, ADMIN))
        cls._set(module, "_owner_permission_denied_reply", _noop_async)
        cls._set(module, "begin_response_measurement", lambda: object())
        cls._set(module, "end_response_measurement", lambda *a, **k: None)
        cls._set(module, "_capture_big_spam_message", lambda *a, **k: None)
        cls._set(module, "_warm_reply_input_chat", lambda *a, **k: None)
        cls._set(module, "record_activity", lambda *a, **k: None)
        cls._set(module, "_record_gif_stat", lambda *a, **k: None)
        cls._set(module, "MessagePerformance", _SilentProfiler)


async def _noop_async(*_args, **_kwargs):
    return None


class _SilentProfiler:
    """پروفایلرِ بی‌اثر (همان رابطِ MessagePerformance)."""

    def __init__(self, *a, **k):
        pass

    def mark(self, *_a, **_k):
        pass

    def skip_to(self, *_a, **_k):
        pass

    def set(self, *_a, **_k):
        pass

    def finish(self, *_a, **_k):
        pass

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


# ---------------------------------------------------------------------------
#  تست‌ها
# ---------------------------------------------------------------------------
def run(coro):
    return asyncio.run(coro)


def setup(fresh_owner=True):
    two_factor_commands.reset_all()
    two_factor.clear_group(CHAT)
    if fresh_owner:
        group_storage.set_group_owner(CHAT, OWNER)
        group_storage.activate_group(CHAT, "تست")
    return Bot()


_MISSING = object()

try:                                     # pytest: بازگردانیِ خودکار بین تست‌ها
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_router_module():
        yield
        from handlers import message_handler as _module
        Helpers.restore(_module)
        Helpers.restore(_module.admin_tools)
except ImportError:                      # pragma: no cover - اجرای مستقیم
    pass


def load_router():
    from handlers import message_handler as module
    Helpers.install(module)
    return module


EXECUTED = []


def install_protected_handler():
    """هندلرِ دستورِ محافظت‌شده که «اجرا شد» را ثبت می‌کند."""
    async def runner(_bot, _event, chat_id, user_id, text):
        EXECUTED.append(text)
        return True

    two_factor_commands.register_handler("پاکسازی کامل", runner)


def send(module, bot, chat_id, sender, text, message_id=500):
    EXECUTED.clear()
    event = Event(chat_id, sender, text, message_id)
    run(module.handle_new_message(bot, event))
    return event


def test_01_router_enable_and_gate():
    print("\n### ۱) روتر: فعال‌سازی، گیتِ ادمین و اجرای تأییدشده")
    module = load_router()
    bot = setup()
    install_protected_handler()
    owner = User(OWNER, "osine2")
    admin = User(ADMIN, "admin_one")

    event = send(module, bot, CHAT, owner, "دو مرحله ای")
    check("فرمانِ «دو مرحله ای» از روتر عبور کرد و کارت آمد",
          texts.TWO_FACTOR_HEADER in event.text(), event.text()[:120])

    send(module, bot, CHAT, owner, "48271953")
    event = send(module, bot, CHAT, owner, "48271953")
    check("با تکرارِ مطابق، قفل فعال شد", two_factor.is_enabled(CHAT))

    event = send(module, bot, CHAT, admin, "پاکسازی کامل")
    check("ادمینِ ثبت‌شده: پیامِ «تأیید دو مرحله‌ای دارد»",
          texts.TWO_FACTOR_ASK_DANGER.split("\n")[0] in event.text(),
          event.text())
    check("ادمینِ ثبت‌شده: دستور اصلاً اجرا نشد", EXECUTED == [], EXECUTED)
    check("ادمین: هیچ درخواستی ساخته نشد",
          two_factor.pending_request(CHAT) is None)

    event = send(module, bot, CHAT, owner, "پاکسازی کامل")
    check("مالک: رمز خواسته شد",
          texts.TWO_FACTOR_ASK_DANGER.split("\n")[0] in event.text(),
          event.text())
    check("مالک: هنوز اجرا نشده", EXECUTED == [], EXECUTED)
    request = two_factor.pending_request(CHAT)
    check("درخواستِ در انتظار برای همان دستور ساخته شد",
          bool(request) and request["command"] == "پاکسازی کامل", request)

    event = send(module, bot, CHAT, owner, "48271953", message_id=777)
    check("رمزِ درست ⇒ همان دستور فوراً اجرا شد (بدون نوشتنِ دوباره)",
          EXECUTED == ["پاکسازی کامل"], EXECUTED)
    check("پیامِ «اجرا شد» ارسال شد",
          any(texts.TWO_FACTOR_EXECUTED in t2 for _c, t2 in bot.client.sent),
          bot.client.sent)
    check("پیامِ رمز از گروه حذف شد", 777 in bot.client.deleted,
          bot.client.deleted)
    check("درخواست یک‌بار مصرف شد", two_factor.pending_request(CHAT) is None)
    check("رمز در لاگ نیست", "48271953" not in bot.logger.everything())


def test_02_random_digits_go_to_games():
    print("\n### ۲) روتر: ۸ رقمِ بی‌ربط از مالک ⇒ مسیر عادی (بازی‌ها)")
    module = load_router()
    bot = setup()
    install_protected_handler()
    owner = User(OWNER, "osine2")
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)

    event = send(module, bot, CHAT, owner, "48271953", message_id=808)
    check("هیچ پاسخِ قابلیت ارسال نشد", not event.text())
    check("پیامِ رمز پاک نشد (متعلق به قابلیت نبود)", 808 not in bot.client.deleted)

    # دستورِ محافظت‌شده همچنان گیت می‌شود
    event = send(module, bot, CHAT, owner, "ریست آمار")
    check("دستورِ خطرناک از مالک: رمز خواسته شد",
          texts.TWO_FACTOR_ASK_DANGER.split("\n")[0] in event.text(),
          event.text())


def test_02b_all_protected_commands_execute():
    print("\n### ۲ب) همهٔ ۹ دستور پس از رمز واقعاً اجرا می‌شوند (بدون «انجام نشد»)")
    module = load_router()
    bot = setup()
    owner = User(OWNER, "osine2")
    from handlers.message_handler import _two_factor_register_handlers
    _two_factor_register_handlers()

    for text in ("دو مرحله ای", "48271953", "48271953"):
        send(module, bot, CHAT, owner, text)
    check("قفل فعال شد", two_factor.is_enabled(CHAT))

    failed = []
    rejected = []
    for command in two_factor.PROTECTED_COMMANDS:
        send(module, bot, CHAT, owner, command)
        if two_factor.pending_request(CHAT) is None:
            rejected.append(command)
            continue
        before = len(bot.client.sent)
        send(module, bot, CHAT, owner, "48271953")
        replies = [t2 for _c, t2 in bot.client.sent[before:]]
        if not any(texts.TWO_FACTOR_EXECUTED in r for r in replies):
            failed.append((command, replies[:2]))
        if any(texts.TWO_FACTOR_FAILED in r for r in replies):
            failed.append((command, "FAILED MESSAGE"))

    check("برای همهٔ ۹ دستور درخواستِ رمز ساخته شد", not rejected, rejected)
    check("همهٔ ۹ دستور پس از رمز «اجرا شد» گرفتند (هیچ «انجام نشد»ی نیست)",
          not failed, failed)
    check("لاگ هیچ خطای اجرایی ندارد",
          not [e for e in bot.logger.errors if "CONFIRMED" in e],
          bot.logger.errors[-3:])


def test_03_disabled_group_unchanged():
    print("\n### ۳) روتر: گروهِ بدون قفل ⇒ رفتار قبلی")
    module = load_router()
    bot = setup()
    install_protected_handler()
    admin = User(ADMIN, "admin_one")
    owner = User(OWNER, "osine2")

    check("قفل خاموش است", not two_factor.is_enabled(CHAT))
    event = send(module, bot, CHAT, admin, "پاکسازی کامل")
    check("برای ادمین هم بدون هیچ گیت/پیامی عبور می‌کند",
          texts.TWO_FACTOR_ASK_DANGER.split("\n")[0] not in event.text(),
          event.text())
    check("گیت هیچ درخواستی نساخت", two_factor.pending_request(CHAT) is None)

    event = send(module, bot, CHAT, owner, "دو مرحله ای")
    check("کارتِ فعال‌سازی از مالک پذیرفته شد",
          texts.TWO_FACTOR_HEADER in event.text())
    send(module, bot, CHAT, owner, "27581439")
    send(module, bot, CHAT, owner, "27581439")
    check("قفل روشن شد", two_factor.is_enabled(CHAT))
    check("اکنون ادمین گیت می‌شود", two_factor.gate(
        CHAT, ADMIN, "پاکسازی کامل") == "denied")


def test_04_disable_restores_admins():
    print("\n### ۴) روتر: لغو ⇒ ادمین‌ها مثل قبل اجرا می‌کنند")
    module = load_router()
    bot = setup()
    install_protected_handler()
    owner = User(OWNER, "osine2")
    admin = User(ADMIN, "admin_one")
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)

    send(module, bot, CHAT, owner, "لغو دو مرحله ای")
    event = send(module, bot, CHAT, owner, "48271953")
    check("قفل خاموش شد", not two_factor.is_enabled(CHAT))
    check("پیامِ لغو ارسال شد",
          any(texts.TWO_FACTOR_DISABLED_TITLE in t2
              for _c, t2 in bot.client.sent), bot.client.sent)

    event = send(module, bot, CHAT, admin, "پاکسازی کامل")
    check("ادمین دیگر گیت نمی‌شود",
          texts.TWO_FACTOR_ASK_DANGER.split("\n")[0] not in event.text(),
          event.text())


def main():
    try:
        test_01_router_enable_and_gate()
        test_02_random_digits_go_to_games()
        test_02b_all_protected_commands_execute()
        test_03_disabled_group_unchanged()
        test_04_disable_restores_admins()
    finally:
        # ماژولِ روتر باید دست‌نخورده به بقیهٔ تست‌ها برگردد.
        from handlers import message_handler as module
        Helpers.restore(module)
        Helpers.restore(module.admin_tools)
    print("\n" + "=" * 70)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\n".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
