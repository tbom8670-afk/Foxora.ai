# -*- coding: utf-8 -*-
"""🧪 تست‌های «تأیید دو مرحله‌ای دستورات خطرناک».

    PYTHONPATH=. python3 tests/test_two_factor.py

پوشش (طبق بند ۸ مشخصات کارفرما):

* فعال‌سازی موفق (رمز → تکرار → فعال شد)
* رمز نامطابق
* ادمینِ ثبت‌شده با دستور خطرناک ⇒ فقط پیامِ «فقط مالک با رمز»
* غیرمالک که رمزِ **درست** را می‌نویسد ⇒ بی‌اثر
* ۳ رمز غلط در یک درخواست ⇒ درخواست باطل
* ۵ رمز غلط در ۱۵ دقیقه ⇒ قفل موقت (و ماندگاری بعد از ری‌استارت)
* انقضای درخواست
* ری‌استارت وسط درخواست (حافظه پاک، storage سالم)
* اجرای دستور با گیت (پس از رمزِ درست، همان دستور فوراً اجرا می‌شود)
* ۸ رقم بی‌ربط از مالک بدون درخواست (به بازی‌ها برسد)
* لغو
* رفتار بدون قفل مثل قبل
* قالب‌ها عیناً مثل اسکرین‌شات (Bold + نقل‌قول با آفست UTF-16)
* پشتیبانی از ارقام فارسی/عربی در رمز
* رمز هرگز لاگ نمی‌شود
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

os.environ.setdefault("BOT_INSTANCE", "two_factor_test")
os.environ.setdefault("BOT_HOME", tempfile.mkdtemp(prefix="foxora-2fa-"))

from modules import group_storage                                # noqa: E402
from modules.manage_features import (storage, texts, two_factor,  # noqa: E402
                                     two_factor_commands)

OWNER = 4242
ADMIN = 7777
STRANGER = 5555
CHAT = -(10 ** 12) - 900

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


def u16(value):
    return len(str(value).encode("utf-16-le")) // 2


def slice16(text, offset, length):
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le")


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

    def everything(self):
        return "\n".join(self.infos + self.errors)


class User:
    def __init__(self, user_id, username=None):
        self.id = user_id
        self.user_id = user_id
        self.username = username
        self.first_name = "کاربر"
        self.last_name = None


class Message:
    def __init__(self, message_id=100, text=""):
        self.id = message_id
        self.message = text


class Event:
    is_private = False

    def __init__(self, chat_id, sender, text="", message_id=100):
        self.chat_id = chat_id
        self.sender = sender
        self.sender_id = sender.id
        self.message = Message(message_id, text)
        self.replies = []

    async def reply(self, text, formatting_entities=None):
        self.replies.append((text, list(formatting_entities or [])))
        return Message(999, text)

    sent = []

    def text(self):
        parts = [t2 for t2, _e in self.replies] + list(self.sent)
        return " ".join(parts)


class Client:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self.calls = []

    async def send_message(self, chat_id, text, formatting_entities=None):
        self.sent.append((chat_id, text))
        return Message(len(self.sent), text)

    async def delete_messages(self, peer, ids):
        self.calls.append("delete_messages")
        self.deleted.extend(list(ids))
        return True


class Bot:
    def __init__(self):
        self.client = Client()
        self.logger = Logger()
        self.reply_input_peer_cache = {}


def fresh(chat_id=CHAT):
    two_factor_commands.reset_all()
    storage.group_key(chat_id)
    two_factor.clear_group(chat_id)
    group_storage.set_group_owner(chat_id, OWNER)


def say(bot, chat_id, sender, text, message_id=100):
    """ارسال پیام و اجرای کاملِ مسیرِ روترِ قابلیت (claim + gate).

    خروجی: ``event`` که ``all_text()`` آن هم پاسخِ ``event.reply`` و هم
    پیام‌های ``client.send_message`` را دارد (مسیر تنظیم از ``send_message``
    استفاده می‌کند تا پیامِ رمز و پاسخ‌ها جای مشخصی داشته باشند).
    """
    event = Event(chat_id, sender, text, message_id)
    sent_before = len(bot.client.sent)
    consumed = asyncio.run(
        two_factor_commands.handle_command(bot, event, chat_id, sender.id,
                                           sender, text, bot.logger))
    if not consumed:
        consumed = asyncio.run(
            two_factor_commands.handle_password(bot, event, chat_id,
                                               sender.id, sender, text,
                                               bot.logger))
    event.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    return event, consumed


def gate_of(chat_id, user_id, text):
    return two_factor.gate(chat_id, user_id, text)


# ---------------------------------------------------------------------------
#  ۱) فعال‌سازی
# ---------------------------------------------------------------------------
def test_01_enable():
    print("\n### ۱) فعال‌سازی موفق (رمز → تکرار → فعال شد)")
    fresh()
    bot = Bot()
    owner = User(OWNER, "osine2")
    event, consumed = say(bot, CHAT, owner, "دو مرحله ای")
    check("فرمان پذیرفته شد", consumed)
    check("کارتِ گام ۱ با فهرست ۹ دستور ارسال شد",
          texts.TWO_FACTOR_HEADER in event.text()
          and event.text().count("●") == 9, event.text()[:120])
    check("رمز هنوز فعال نیست", not two_factor.is_enabled(CHAT))

    event2, consumed2 = say(bot, CHAT, owner, "482719")
    check("رمزِ کوتاه در حالت انتظار ⇒ پیامِ «باید ۸ رقم باشد»", consumed2
          and texts.TWO_FACTOR_INVALID in event2.text(), event2.text())
    check("رمزِ کوتاه قفل را فعال نمی‌کند", not two_factor.is_enabled(CHAT))

    event2b, consumed2b = say(bot, CHAT, owner, "4827 1953")
    check("پیامِ غیر ۸ رقمی (شمارهٔ موبایل) claim نمی‌شود و به مسیر خودش می‌رود",
          consumed2b is False)

    event3, _ = say(bot, CHAT, owner, "12345678")
    check("رمز بدیهی رد شد",
          texts.TWO_FACTOR_OBVIOUS in event3.text(), event3.text())

    event4, _ = say(bot, CHAT, owner, "٤۸۲۷١٩٥٣")
    check("رمز با ارقام فارسی/عربی پذیرفته و تکرار خواسته شد",
          texts.TWO_FACTOR_REPEAT_REQUEST in event4.text(), event4.text())

    event5, _ = say(bot, CHAT, owner, "48271954")
    check("تکرار نامطابق ⇒ پیامِ مخصوص",
          texts.TWO_FACTOR_MISMATCH in event5.text(), event5.text())
    check("بعد از نامطابق هنوز فعال نیست", not two_factor.is_enabled(CHAT))

    # تکرار نامطابق جریان را بست؛ از نو شروع می‌کنیم.
    say(bot, CHAT, owner, "دو مرحله ای")
    say(bot, CHAT, owner, "48271953")
    sent_before = len(bot.client.sent)
    event6, _ = say(bot, CHAT, owner, "48271953")
    event6.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    check("با تکرارِ مطابق فعال شد", two_factor.is_enabled(CHAT))
    check("پیامِ «رمز ساخته شد» ارسال شد",
          texts.TWO_FACTOR_CREATED_TITLE in event6.text(), event6.text())
    check("پیامِ رمز پاک شد (best-effort)", bool(bot.client.deleted))

    entry = two_factor.status(CHAT)[1]
    check("هش و salt ذخیره شد و رمز خام جایی نیست",
          bool(entry.get("hash")) and bool(entry.get("salt"))
          and "48271953" not in str(entry), entry)
    check("set_by برای رهگیری ثبت شد", str(entry.get("set_by")) == str(OWNER),
          entry.get("set_by"))
    check("رمز هیچ‌جا لاگ نشد",
          "48271953" not in bot.logger.everything(), bot.logger.everything()[-200:])
    check("مسیر رمز صحیح است", two_factor.verify_password(CHAT, "48271953"))

    # فقط مالکِ ثبت‌شده: ادمین و کاربر عادی هیچ‌وقت
    check("ادمینِ ثبت‌شده نمی‌تواند رمز بگذارد",
          not two_factor.is_registered_owner(CHAT, ADMIN)
          and not two_factor.is_owner(CHAT, ADMIN))
    check("کاربر عادی نمی‌تواند رمز بگذارد",
          not two_factor.is_owner(CHAT, STRANGER))
    check("مالکِ ثبت‌شده مجاز است", two_factor.is_registered_owner(CHAT, OWNER))

    # فعال‌سازی دوباره
    event7, _ = say(bot, CHAT, owner, "دو مرحله ای")
    check("فعال‌سازی دوباره ⇒ «از قبل فعال است»",
          texts.TWO_FACTOR_ALREADY_ON in event7.text(), event7.text())


# ---------------------------------------------------------------------------
#  ۲) گیت
# ---------------------------------------------------------------------------
def test_02_gate():
    print("\n### ۲) گیتِ دستورهای خطرناک (مالک/غیرمالک)")
    fresh()
    bot = Bot()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    group_storage.set_group_owner(CHAT, OWNER)

    check("گروه قفل است", two_factor.is_enabled(CHAT))
    check("دستورِ محافظت‌شده شناسایی می‌شود",
          two_factor.protected_command("پاکسازی کامل") == "پاکسازی کامل")
    check("دستور با پیشوند هم شناسایی می‌شود",
          two_factor.protected_command("!پاک لیست اخراج") == "پاک لیست اخراج")
    check("دستورِ عادی محافظت‌شده نیست",
          two_factor.protected_command("لیست سکوت") is None)
    check("همهٔ ۹ دستورِ محافظت‌شده دقیقاً همان فهرستِ کارفرما هستند",
          set(two_factor.PROTECTED_COMMANDS) == {
              "پاکسازی کامل", "پاک لیست سکوت", "پاک لیست اخراج",
              "ریست آمار", "تغییر مجازات", "تغییر اخطار",
              "پاکسازی خودکار", "سرگرمی فعال", "لغو کلمات ممنوعه"},
          two_factor.PROTECTED_COMMANDS)
    check("شکلِ دیگرِ همان دستورها هم گیت می‌شود",
          two_factor.protected_command("سرگرمی خاموش") == "سرگرمی فعال"
          and two_factor.protected_command("!پاک لیست اخراج ها")
          == "پاک لیست اخراج"
          and two_factor.protected_command("پاکسازی خودکار 30")
          == "پاکسازی خودکار")

    check("مالک ⇒ «owner»", gate_of(CHAT, OWNER, "پاکسازی کامل") == "owner")
    check("ادمینِ ثبت‌شده ⇒ «denied»",
          gate_of(CHAT, ADMIN, "پاکسازی کامل") == "denied")
    check("کاربر عادی ⇒ «denied»",
          gate_of(CHAT, STRANGER, "ریست آمار") == "denied")
    check("دستورِ غیرخطرناک برای همه آزاد است",
          gate_of(CHAT, STRANGER, "لیست سکوت") == "ok")
    check("اجرای تأییدشده از گیت رد می‌شود (بدون قفل دوباره)",
          two_factor.gate(CHAT, ADMIN, "پاکسازی کامل", confirmed=True) == "ok")

    # ادمین پیام می‌گیرد و هیچ درخواستی ساخته نمی‌شود
    event = Event(CHAT, User(ADMIN, "admin"))
    consumed = run(two_factor_commands.gate(
        bot, event, CHAT, ADMIN, event.sender, "پاکسازی کامل", bot.logger))
    check("ادمین: پیام مصرف شد (اجرا نشد)", consumed)
    check("ادمین: پیامِ «فقط مالک با رمز»",
          texts.TWO_FACTOR_ASK_DANGER.split("\n")[0] in event.text(),
          event.text())
    check("ادمین: هیچ درخواستِ رمزی ساخته نشد",
          two_factor.pending_request(CHAT) is None)

    # مالک: درخواست ساخته می‌شود
    event2 = Event(CHAT, User(OWNER, "osine2"), "پاکسازی کامل")
    consumed2 = run(two_factor_commands.gate(
        bot, event2, CHAT, OWNER, event2.sender, "پاکسازی کامل", bot.logger))
    check("مالک: پیام مصرف شد و رمز خواسته شد", consumed2
          and texts.TWO_FACTOR_ASK_DANGER.split("\n")[0] in event2.text())
    request = two_factor.pending_request(CHAT)
    check("درخواست در انتظار با nonce و کلید دستور ساخته شد",
          bool(request) and request["command"] == "پاکسازی کامل"
          and len(request["nonce"]) == 16, request)

    # درخواستِ دوم جایگزین قبلی می‌شود
    first_nonce = request["nonce"]
    run(two_factor_commands.gate(bot, event2, CHAT, OWNER, event2.sender,
                                 "ریست آمار", bot.logger))
    second = two_factor.pending_request(CHAT)
    check("درخواستِ جدید جایگزین قبلی شد (هر گروه/مالک فقط یکی)",
          second["nonce"] != first_nonce and second["command"] == "ریست آمار",
          second)


# ---------------------------------------------------------------------------
#  ۳) رمزِ درست از مالک ⇒ اجرای همان دستور (بدون نوشتنِ دوباره)
# ---------------------------------------------------------------------------
def test_03_execute_after_password():
    print("\n### ۳) رمزِ درست از مالک ⇒ همان دستور فوراً اجرا می‌شود")
    fresh()
    bot = Bot()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    group_storage.set_group_owner(CHAT, OWNER)

    executed = []

    async def runner(_bot, _event, chat_id, user_id, text):
        executed.append(text)
        return True

    two_factor_commands.register_handler("پاکسازی کامل", runner)

    owner = User(OWNER, "osine2")
    run(two_factor_commands.gate(bot, Event(CHAT, owner), CHAT, OWNER, owner,
                                 "پاکسازی کامل", bot.logger))
    check("درخواست ساخته شد", two_factor.pending_request(CHAT) is not None)

    sent_before = len(bot.client.sent)
    event = Event(CHAT, owner, "48271953", message_id=321)
    consumed = run(two_factor_commands.handle_password(
        bot, event, CHAT, OWNER, owner, "48271953", bot.logger))
    event.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    check("پیامِ رمز مصرف شد", consumed)
    check("همان دستور با پرچم تأیید اجرا شد", executed == ["پاکسازی کامل"],
          executed)
    check("پیامِ «اجرا شد» ارسال شد",
          any(texts.TWO_FACTOR_EXECUTED in t2 for _c, t2 in bot.client.sent),
          bot.client.sent)
    check("درخواست یک‌بار مصرف شد (بعد از اجرا دیگر نیست)",
          two_factor.pending_request(CHAT) is None)
    check("رمز پاک شد (best-effort)", 321 in bot.client.deleted,
          bot.client.deleted)


# ---------------------------------------------------------------------------
#  ۴) رمزِ غلط، ۳ تلاش و قفلِ موقت
# ---------------------------------------------------------------------------
def test_04_wrong_password():
    print("\n### ۴) رمزِ غلط: تعداد تلاش، ۳ تلاش و قفل موقت (۵ خطا/۱۵ دقیقه)")
    fresh()
    bot = Bot()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    group_storage.set_group_owner(CHAT, OWNER)
    owner = User(OWNER, "osine2")

    for attempt in (1, 2):
        run(two_factor_commands.gate(bot, Event(CHAT, owner), CHAT, OWNER,
                                     owner, "پاکسازی کامل", bot.logger))
        sent_before = len(bot.client.sent)
        event = Event(CHAT, owner, "99999999")
        run(two_factor_commands.handle_password(bot, event, CHAT, OWNER,
                                                owner, "99999999", bot.logger))
        event.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
        attempts_left = two_factor.MAX_WRONG_PER_REQUEST - attempt
        check(f"تلاش {attempt}: تعداد تلاشِ باقی‌مانده اعلام شد",
              f"تلاش باقی مانده : {attempts_left}" in event.text(),
              event.text())
        check(f"تلاش {attempt}: درخواست هنوز باز است",
              two_factor.pending_request(CHAT) is not None)

    run(two_factor_commands.gate(bot, Event(CHAT, owner), CHAT, OWNER, owner,
                                 "پاکسازی کامل", bot.logger))
    sent_before = len(bot.client.sent)
    event = Event(CHAT, owner, "99999999")
    run(two_factor_commands.handle_password(bot, event, CHAT, OWNER, owner,
                                            "99999999", bot.logger))
    event.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    check("تلاش سوم ⇒ درخواست باطل شد",
          two_factor.pending_request(CHAT) is None)
    check("پیامِ «۳ رمز غلط» ارسال شد",
          texts.TWO_FACTOR_TOO_MANY in event.text(), event.text())

    # ۵ خطای متوالی در پنجرهٔ ۱۵ دقیقه ⇒ قفل موقت (تستِ مسیرِ مستقیم)
    fresh()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    lockout = 0
    for _ in range(5):
        _recent, lockout, _level = two_factor.register_failure(CHAT)
    check("۵ خطا ⇒ قفل موقت فعال شد", lockout >= two_factor.LOCKOUT_BASE,
          lockout)
    check("قفل موقت در storage ماندگار است (ری‌استارت صفرش نمی‌کند)",
          two_factor.lockout_remaining(CHAT) > 0)

    # مسیر واقعی: مالک دستور را می‌زند و گیت پیامِ «قفل موقت» می‌دهد
    sent_before = len(bot.client.sent)
    lock_event = Event(CHAT, owner, "پاکسازی کامل")
    run(two_factor_commands.gate(bot, lock_event, CHAT, OWNER, owner,
                                 "پاکسازی کامل", bot.logger))
    lock_event.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    check("در قفل موقت، گیت پیامِ «قفل موقت» می‌دهد",
          texts.TWO_FACTOR_LOCKOUT_TITLE in lock_event.text(),
          lock_event.text())
    check("در قفل موقت هیچ درخواستی ساخته نمی‌شود",
          two_factor.pending_request(CHAT) is None)

    # افزایش تدریجی
    first = two_factor.lockout_remaining(CHAT)
    data = two_factor.load()
    key = storage.group_key(CHAT)
    data["groups"][key]["lockout_until"] = 0.0
    two_factor.save(data)
    for _ in range(5):
        _r, second_lockout, _l = two_factor.register_failure(CHAT)
    check("قفل دوم بلندتر از اولی است (افزایش تدریجی)",
          second_lockout > first, (first, second_lockout))

    # ورودِ موفق، شمارنده را صفر می‌کند
    data = two_factor.load()
    data["groups"][key]["lockout_until"] = 0.0
    two_factor.save(data)
    two_factor.register_success(CHAT)
    check("ورودِ موفق شمارنده و قفل را صفر کرد",
          two_factor.failure_count(CHAT) == 0
          and two_factor.lockout_remaining(CHAT) == 0)


# ---------------------------------------------------------------------------
#  ۵) غیرمالک با رمزِ درست ⇒ بی‌اثر
# ---------------------------------------------------------------------------
def test_05_non_owner_with_correct_password():
    print("\n### ۵) غیرمالک حتی با رمزِ درست هیچ اثری ندارد")
    fresh()
    bot = Bot()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    group_storage.set_group_owner(CHAT, OWNER)

    stranger = User(STRANGER, "stranger")
    event = Event(CHAT, stranger, "48271953")
    consumed = run(two_factor_commands.handle_password(
        bot, event, CHAT, STRANGER, stranger, "48271953", bot.logger))
    check("پیامِ رمزِ غیرمالک مصرف نمی‌شود (به بقیهٔ هندلرها می‌رسد)",
          consumed is False)
    check("هیچ درخواستی ساخته نشد", two_factor.pending_request(CHAT) is None)
    check("رمز معتبر ولی مالک نیست ⇒ گیت همچنان «denied»",
          gate_of(CHAT, STRANGER, "پاکسازی کامل") == "denied")

    # وقتی درخواستی برای گروه باز است، غیرمالک پیامِ «مالک نیستید» می‌گیرد
    owner = User(OWNER, "osine2")
    run(two_factor_commands.gate(bot, Event(CHAT, owner), CHAT, OWNER, owner,
                                 "پاکسازی کامل", bot.logger))
    sent_before = len(bot.client.sent)
    event2 = Event(CHAT, stranger, "48271953")
    run(two_factor_commands.handle_password(bot, event2, CHAT, STRANGER,
                                            stranger, "48271953", bot.logger))
    event2.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    check("غیرمالک: پیامِ «شما مالک ثبت شده نیستید»",
          texts.TWO_FACTOR_NOT_OWNER in event2.text(), event2.text())
    check("رمزِ غیرمالک رمزِ مالک را مصرف نکرد",
          two_factor.pending_request(CHAT) is not None)


# ---------------------------------------------------------------------------
#  ۶) پیام ۸ رقمی بی‌ربط ⇒ به بازی‌ها برسد
# ---------------------------------------------------------------------------
def test_06_random_digits_pass_through():
    print("\n### ۶) ۸ رقمِ بی‌ربط از مالک (بدون درخواست) ⇒ مسیر عادی/بازی‌ها")
    fresh()
    bot = Bot()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    group_storage.set_group_owner(CHAT, OWNER)
    owner = User(OWNER, "osine2")

    event = Event(CHAT, owner, "48271953")
    consumed = run(two_factor_commands.handle_password(
        bot, event, CHAT, OWNER, owner, "48271953", bot.logger))
    check("بدون درخواست/حالتِ انتظار مصرف نمی‌شود", consumed is False)
    check("هیچ پاسخی ارسال نشد", not event.replies)
    check("claims برای این پیام False است",
          two_factor_commands.claims(bot, CHAT, OWNER, "48271953") is False)
    check("۸ رقم از غیرمالک هم هرگز claim نمی‌شود",
          two_factor_commands.claims(bot, CHAT, STRANGER, "48271953") is False)


# ---------------------------------------------------------------------------
#  ۷) انقضا و ری‌استارت
# ---------------------------------------------------------------------------
def test_07_expiry_and_restart():
    print("\n### ۷) انقضای درخواست و ری‌استارت وسط درخواست")
    fresh()
    bot = Bot()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    group_storage.set_group_owner(CHAT, OWNER)
    owner = User(OWNER, "osine2")

    request = two_factor.create_request(CHAT, OWNER, "پاکسازی کامل",
                                        "پاکسازی کامل")
    check("درخواست ساخته شد", two_factor.pending_request(CHAT) is not None)
    # انقضا با دست‌کاری ساعتِ یکنواختِ همان درخواست
    from modules.manage_features import two_factor as module
    key = storage.group_key(CHAT)
    module._REQUESTS[key]["created_at"] -= (module.REQUEST_TTL + 5)
    check("درخواستِ منقضی دیگر معتبر نیست",
          two_factor.pending_request(CHAT) is None)

    event = Event(CHAT, owner, "48271953")
    consumed = run(two_factor_commands.handle_password(
        bot, event, CHAT, OWNER, owner, "48271953", bot.logger))
    check("رمز بعد از انقضا مصرف نمی‌شود (به بازی‌ها می‌رسد)", consumed is False)

    # «ری‌استارت»: حافظه پاک، storage سالم
    two_factor.create_request(CHAT, OWNER, "ریست آمار", "ریست آمار")
    persisted = two_factor.load()
    two_factor._REQUESTS.clear()
    two_factor._AWAIT.clear()
    check("بعد از ری‌استارت درخواستِ حافظه از بین می‌رود",
          two_factor.pending_request(CHAT) is None)
    check("رکوردِ رمز در storage سالم می‌ماند (قفل فعال)",
          two_factor.is_enabled(CHAT) and bool(persisted["groups"]))
    check("رمزِ اصلی بعد از ری‌استارت هم معتبر است",
          two_factor.verify_password(CHAT, "48271953"))


# ---------------------------------------------------------------------------
#  ۸) لغو
# ---------------------------------------------------------------------------
def test_08_disable():
    print("\n### ۸) لغو دو مرحله‌ای (با رمزِ فعلی)")
    fresh()
    bot = Bot()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    group_storage.set_group_owner(CHAT, OWNER)
    owner = User(OWNER, "osine2")

    event, consumed = say(bot, CHAT, owner, "لغو دو مرحله ای")
    check("فرمانِ لغو پذیرفته و رمز خواسته شد", consumed
          and texts.TWO_FACTOR_DISABLE_REQUEST in event.text(), event.text())
    check("تا رمزِ درست، قفل هنوز فعال است", two_factor.is_enabled(CHAT))

    sent_before = len(bot.client.sent)
    event2 = Event(CHAT, owner, "99999999")
    run(two_factor_commands.handle_password(bot, event2, CHAT, OWNER, owner,
                                            "99999999", bot.logger))
    event2.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    check("رمز غلط در لغو ⇒ رد شد و قفل ماند", two_factor.is_enabled(CHAT))
    check("رمز غلط در لغو ⇒ پیامِ «رمز رد شد اشتباه میباشد»",
          texts.TWO_FACTOR_WRONG_TITLE in event2.text(), event2.text())

    sent_before = len(bot.client.sent)
    event3 = Event(CHAT, owner, "48271953")
    run(two_factor_commands.handle_password(bot, event3, CHAT, OWNER, owner,
                                            "48271953", bot.logger))
    event3.sent = [t2 for _c, t2 in bot.client.sent[sent_before:]]
    check("با رمزِ درست، رکورد گروه کامل حذف شد",
          not two_factor.is_enabled(CHAT)
          and two_factor.status(CHAT)[1] is None)
    check("پیامِ «تایید دو مرحله ای لغو شد» ارسال شد",
          any(texts.TWO_FACTOR_DISABLED_TITLE in t2
              for _c, t2 in bot.client.sent), bot.client.sent)
    check("حالا ادمین‌های ثبت‌شده هم می‌توانند دستورها را اجرا کنند",
          gate_of(CHAT, ADMIN, "پاکسازی کامل") == "ok")

    event4, _ = say(bot, CHAT, owner, "لغو دو مرحله ای")
    check("لغو وقتی فعال نیست ⇒ «فعال نیست»",
          texts.TWO_FACTOR_NOT_ON in event4.text(), event4.text())


# ---------------------------------------------------------------------------
#  ۹) گروه بدون قفل = رفتار قبلی
# ---------------------------------------------------------------------------
def test_08b_owner_policy():
    print("\n### ۸ب) «فقط مالک ثبت‌شده» — بدونِ استثنای مالک سراسری")
    fresh()
    bot = Bot()
    # گروهی که مالکِ ثبت‌شده ندارد
    other = -(10 ** 12) - 902
    two_factor.clear_group(other)
    no_owner_chat = -(10 ** 12) - 903
    two_factor.clear_group(no_owner_chat)

    # «ثبت مالک» نکرده ⇒ هیچ‌کس (حتی مالک سراسری) نمی‌تواند رمز بگذارد
    from modules import owner_check
    original_global = owner_check.is_global_owner
    owner_check.is_global_owner = lambda user: True
    try:
        check("گروهِ بدون مالک: مالکِ سراسری هم مجاز نیست",
              not two_factor.is_registered_owner(no_owner_chat, OWNER))
        event = Event(no_owner_chat, User(OWNER, "global_owner"), "دو مرحله ای")
        consumed = run(two_factor_commands.handle_command(
            bot, event, no_owner_chat, OWNER, event.sender, "دو مرحله ای",
            bot.logger))
        text = event.text() + " ".join(t2 for _c, t2 in bot.client.sent)
        check("گروهِ بدون مالک: پیامِ «اول ثبت مالک»",
              consumed and texts.TWO_FACTOR_NO_REGISTERED_OWNER in text, text)
        check("گروهِ بدون مالک: قفل فعال نشد",
              not two_factor.is_enabled(no_owner_chat))
    finally:
        owner_check.is_global_owner = original_global

    # رمزِ درست هم از غیرمالک پذیرفته نمی‌شود (حتی اگر درخواست باز باشد)
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    two_factor.create_request(CHAT, OWNER, "ریست آمار", "ریست آمار")
    event = Event(CHAT, User(ADMIN, "admin"), "48271953")
    consumed = run(two_factor_commands.handle_password(
        bot, event, CHAT, ADMIN, event.sender, "48271953", bot.logger))
    check("ادمین با رمزِ درست: مصرف نمی‌شود", consumed is False)
    check("ادمین با رمزِ درست: درخواست مالک دست‌نخورده می‌ماند",
          two_factor.pending_request(CHAT) is not None)


def test_09_no_lock_same_behaviour():
    print("\n### ۹) گروهِ بدون قفل: رفتار دقیقاً مثل قبل")
    fresh()
    for user_id in (OWNER, ADMIN, STRANGER):
        for command in two_factor.PROTECTED_COMMANDS:
            assert gate_of(CHAT, user_id, command) == "ok", (user_id, command)
    check("همهٔ ۹ دستور برای همه آزاد است (بدون قفل)", True)
    check("پیام ۸ رقمی هیچ claim نمی‌شود",
          not two_factor_commands.claims(Bot(), CHAT, OWNER, "48271953"))
    check("فرمانِ دو مرحله‌ای از غیرمالک پذیرفته نمی‌شود", run(
        two_factor_commands.handle_command(
            Bot(), Event(CHAT, User(STRANGER)), CHAT, STRANGER,
            User(STRANGER), "دو مرحله ای", Logger())) is True)


# ---------------------------------------------------------------------------
#  ۱۰) قالب‌ها عیناً مثل اسکرین‌شات
# ---------------------------------------------------------------------------
def test_10_templates_match_screenshot():
    print("\n### ۱۰) قالب‌ها عیناً مثل اسکرین‌شات (Bold + نقل‌قول)")
    text, entities = texts.two_factor_activate_request()
    bolds = [e for e in entities if type(e).__name__ == "MessageEntityBold"]
    quotes = [e for e in entities if type(e).__name__ == "MessageEntityBlockquote"]
    check("کلِ متن Bold است",
          len(bolds) == 1 and bolds[0].offset == 0 and bolds[0].length == u16(text))
    check("فقط عنوان داخل نقل‌قول است (فهرست و درخواستِ رمز بیرون)",
          len(quotes) == 1
          and slice16(text, quotes[0].offset, quotes[0].length)
          == texts.TWO_FACTOR_HEADER,
          [(q.offset, q.length) for q in quotes])
    check("نُه خطِ محافظت‌شده با «●» آمده‌اند",
          all(f"●{line}" in text for line in texts.TWO_FACTOR_PROTECTED_LINES))
    check("متنِ درخواستِ رمز عیناً مطابق تصویر است",
          "⚙ : یک رمز 8 رقمی بنویسید و ارسال کنید\n"
          "این رمز رو به خاطر بسپارید" in text)

    text2, ents2 = texts.two_factor_created()
    q2 = [e for e in ents2 if type(e).__name__ == "MessageEntityBlockquote"]
    check("«رمز ساخته شد»: عنوان در نقل‌قول",
          len(q2) == 1 and slice16(text2, q2[0].offset, q2[0].length)
          == texts.TWO_FACTOR_CREATED_TITLE)
    check("متنِ توضیحِ بعد از ساخت عیناً مطابق تصویر",
          "از این پس برای اجرای این دستورات باید فقط رمز رو\n"
          "وارد کنید اونم فقط توسط مالک ثبت شده نه ادمین ها" in text2)

    text3, ents3 = texts.two_factor_disabled()
    q3 = [e for e in ents3 if type(e).__name__ == "MessageEntityBlockquote"]
    check("«لغو شد»: عنوان در نقل‌قول",
          len(q3) == 1 and slice16(text3, q3[0].offset, q3[0].length)
          == texts.TWO_FACTOR_DISABLED_TITLE)
    check("متنِ «حالا ادمین های ثبت شده …» عیناً",
          "حالا ادمین های ثبت شده هم میتوانند از سایر\n"
          "دستورات هم استفاده کنند" in text3)

    text4, ents4 = texts.two_factor_ask_password()
    check("«این دستور تایید دو مرحله ای دارد …» بدون نقل‌قول",
          not [e for e in ents4
               if type(e).__name__ == "MessageEntityBlockquote"]
          and "✺ : این دستور تایید دو مرحله ای دارد\n"
              "اگر مالـک ثبت شده هستید رمز آن را وارد کنید" == text4)

    text5, _ = texts.two_factor_message("wrong")
    check("«رمز رد شد اشتباه میباشد» عیناً (بدون نقل‌قول)",
          text5.startswith("📌 رمـز رد شد اشتباه میـباشد"))
    text6, _ = texts.two_factor_not_registered_owner()
    check("«رمز شما تایید نشد …» عیناً",
          text6 == "▪️رمـز شما تـایید نشد شما مالـک ثبت شده نیستید")


# ---------------------------------------------------------------------------
#  ۱۱) «لیست ادمینی»
# ---------------------------------------------------------------------------
def test_11_admin_help_entry():
    print("\n### ۱۱) «لیست ادمینی»: عنوان Bold + دو دستور در یک نقل‌قول")
    from modules import admin_help_builder as help_builder

    text = help_builder.ADMIN_HELP_TEXT
    entities = help_builder.ADMIN_HELP_ENTITIES
    expected = ("⏰ : برای تایید دو مرحله ای دستورات خطرناک بنویسید\n"
                "دو مرحله ای\n"
                "لغو دو مرحله ای")
    check("متنِ درخواستیِ کارفرما در لیست ادمینی هست", expected in text)
    start = u16(text[:text.index(expected)])
    bolds = [(e.offset, e.length) for e in entities
             if type(e).__name__ == "MessageEntityBold"
             and e.offset == start and e.length == u16(expected)]
    check("کلِ بلوک Bold است", bool(bolds), bolds)
    quotes = [(e.offset, e.length) for e in entities
              if type(e).__name__ == "MessageEntityBlockquote"]
    want = ("\n".join(("دو مرحله ای", "لغو دو مرحله ای")))
    found = [q for q in quotes
             if slice16(text, q[0], q[1]).strip() == want]
    check("هر دو دستور کنارِ هم در **یک** نقل‌قول‌اند", bool(found), found)


# ---------------------------------------------------------------------------
#  ۱۲) امنیت
# ---------------------------------------------------------------------------
def test_12_security():
    print("\n### ۱۲) امنیت: هش، salt یکتا، عدم ذخیره/لاگِ رمز")
    fresh()
    two_factor.set_password(CHAT, "48271953", set_by=OWNER)
    other_chat = -(10 ** 12) - 901
    two_factor.clear_group(other_chat)
    two_factor.set_password(other_chat, "48271953", set_by=OWNER)

    first = two_factor.status(CHAT)[1]
    second = two_factor.status(other_chat)[1]
    check("salt برای هر گروه یکتاست", first["salt"] != second["salt"])
    check("هش برای هر گروه یکتاست (salt اثر دارد)",
          first["hash"] != second["hash"])
    check("الگوریتم و پارامترهای KDF ذخیره شده",
          first["kdf"]["algo"] in {"scrypt", "pbkdf2_sha256"}, first["kdf"])
    check("هش با salt و KDF قابل تأیید است",
          two_factor.verify_password(CHAT, "48271953")
          and not two_factor.verify_password(CHAT, "48271954"))
    check("رمز خام در فایل نیست",
          "48271953" not in str(two_factor.load()))
    check("رمز در فایل با ارقام فارسی هم نشت نکرده",
          "۴۸۲۷۱۹۵۳" not in str(two_factor.load()))
    check("ارقام فارسی در ورود هم پذیرفته می‌شوند",
          two_factor.verify_password(CHAT, "۴۸۲۷۱۹۵۳"))

    # فلفل سروری: با تغییرِ pepper، هشِ قبلی دیگر معتبر نیست
    os.environ["FOXORA_TWO_FACTOR_PEPPER"] = "server-pepper"
    try:
        two_factor.set_password(other_chat, "48271953", set_by=OWNER)
        check("با pepper، رمزِ درست تأیید می‌شود",
              two_factor.verify_password(other_chat, "48271953"))
        os.environ["FOXORA_TWO_FACTOR_PEPPER"] = "other-pepper"
        check("با pepperِ متفاوت، همان رمز رد می‌شود",
              not two_factor.verify_password(other_chat, "48271953"))
    finally:
        os.environ.pop("FOXORA_TWO_FACTOR_PEPPER", None)

    # خرابیِ فایل نباید دستورها را قفل کند
    fresh()
    two_factor._TWO_FACTOR_STORAGE.save("{not-a-dict")
    check("فایلِ خراب ⇒ is_enabled=False (دستورها قفل نمی‌شوند)",
          two_factor.is_enabled(CHAT) is False)


def main():
    test_01_enable()
    test_02_gate()
    test_03_execute_after_password()
    test_04_wrong_password()
    test_05_non_owner_with_correct_password()
    test_06_random_digits_pass_through()
    test_07_expiry_and_restart()
    test_08_disable()
    test_08b_owner_policy()
    test_09_no_lock_same_behaviour()
    test_10_templates_match_screenshot()
    test_11_admin_help_entry()
    test_12_security()

    print("\n" + "=" * 70)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\n".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
