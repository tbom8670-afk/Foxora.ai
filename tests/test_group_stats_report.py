# -*- coding: utf-8 -*-
"""🧪 تست قالب «📊 آمار کل گروه» + شمارندهٔ گیف + رتبه‌بندی کاربران.

اجرا:

    PYTHONPATH=. python3 tests/test_group_stats_report.py

پوشش:
  ۱. متن دقیقاً همان قالب کارفرما (خط‌به‌خط، با اعداد فارسی)
  ۲. کل متن Bold است و هر خطِ آماری دقیقاً یک نقل‌قول (UTF-16، بدون هم‌پوشانی)
  ۳. رتبه‌بندی فقط بر اساس تعداد پیام، نزولی و قطعی (بدون حتی یک خطا)
  ۴. کمتر از ۱۰ کاربر ⇒ ردیف‌های باقی‌مانده «⬟ کاربر N : ----»
  ۵. نام: یوزرنیم، وگرنه نام نمایشی — هرگز شناسهٔ عددی و هرگز «unknown»
  ۶. شمارندهٔ گیف واقعی (گروه + کاربر) و ماندگاری روی دیسک
  ۷. تعداد اعضا: مقدار سرور ذخیره می‌شود و در خطای سرور از مقدار ذخیره‌شده
     استفاده می‌شود (نه صفرِ دروغ)
  ۸. مسیریابی واقعی هندلر برای «آمار گپ»/«آمار گروه»
  ۹. هزینهٔ ساخت قالب در حد میکروثانیه (بدون رگرسیون)
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_HOME", tempfile.mkdtemp(prefix="foxora-stats-"))

from splusthon.tl.types import MessageEntityBold, MessageEntityBlockquote

from modules import group_stats, group_stats_report as report

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


def u16(text):
    return len(str(text).encode("utf-16-le")) // 2


def slice16(text, offset, length):
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le")


def fresh(chat_id):
    """گروه تازه با شمارنده‌های صفر (فایل آمار تست آلوده نشود)."""
    group_stats.flush()
    data = group_stats.load_stats()
    data[str(chat_id)] = {
        "messages": 0, "deleted": 0, "kicked": 0, "muted": 0,
        "gifs": 0, "members": 0, "users": {},
    }
    group_stats.save_stats(data)
    group_stats.flush()
    return chat_id


# ---------------------------------------------------------------------------
# ۱) متن دقیقاً مطابق قالب کارفرما
# ---------------------------------------------------------------------------
def test_01_exact_template():
    print("\n### ۱) متن دقیقاً همان قالب کارفرما")
    chat = fresh(-51001)
    group_stats.add_message(chat, 1, "ali", "علی")
    group_stats.add_message(chat, 1, "ali", "علی")
    group_stats.add_deleted_count(chat, 1, "ali", 4)

    text, entities = report.build_report(chat, member_count=3)

    expected = "\n".join([
        "📊 آمار کل گروه",
        "",
        "کل پیام‌ها: ۲",
        "پیام های حذف شده: ۴",
        "اخراج شده ها : ۰",
        "سکوت شدها : ۰",
        "تعداد اعضا گروه : ۳",
        "تعداد کل گیف های ارسالی : ۰",
        "",
        "↶ کاربران فعال گروه :",
        "⬟ کاربر ۱ : @ali",
    ] + [f"⬟ کاربر {report.fa_digits(i)} : ----" for i in range(2, 11)])
    check("متن کامل مطابق قالب است", text == expected,
          text.splitlines()[:4])
    check("عنوان درست است", text.startswith("📊 آمار کل گروه\n\n"))
    check("ستون‌های آماری همه حاضرند",
          all(label in text for label in (
              "کل پیام‌ها", "پیام های حذف شده", "اخراج شده ها :",
              "سکوت شدها :", "تعداد اعضا گروه :",
              "تعداد کل گیف های ارسالی :")))
    check("اعداد فارسی‌اند", "۰" in text and "0" not in text, text[:40])


# ---------------------------------------------------------------------------
# ۲) قالب‌بندی: کل متن Bold، هر خط آماری یک نقل‌قول، آفست UTF-16
# ---------------------------------------------------------------------------
def test_02_entities():
    print("\n### ۲) Bold کل متن + یک نقل‌قول برای هر خط آماری")
    chat = fresh(-51002)
    for index in range(3):
        group_stats.add_message(chat, 100 + index, f"user{index}", f"کاربر {index}")
    text, entities = report.build_report(chat, member_count=3)

    bold = [e for e in entities if isinstance(e, MessageEntityBold)]
    quotes = [e for e in entities if isinstance(e, MessageEntityBlockquote)]
    check("دقیقاً یک Bold برای کل متن",
          len(bold) == 1 and bold[0].offset == 0 and bold[0].length == u16(text),
          [(b.offset, b.length) for b in bold])
    check("۶ نقل‌قول (یکی برای هر خط آماری)", len(quotes) == 6, len(quotes))

    stat_lines = [
        "کل پیام‌ها", "پیام های حذف شده", "اخراج شده ها :",
        "سکوت شدها :", "تعداد اعضا گروه :", "تعداد کل گیف های ارسالی :",
    ]
    bodies = [slice16(text, q.offset, q.length) for q in quotes]
    check("هر نقل‌قول دقیقاً همان خط است",
          all(body.startswith(label) for body, label in zip(bodies, stat_lines)),
          bodies)
    check("نقل‌قول‌ها هم‌پوشانی ندارند",
          all(quotes[i].offset + quotes[i].length < quotes[i + 1].offset
              for i in range(len(quotes) - 1)),
          [(q.offset, q.length) for q in quotes])
    check("حداقل یک نقل‌قول روی «تعداد کل گیف های ارسالی» است",
          any("تعداد کل گیف های ارسالی" in body for body in bodies), bodies)


# ---------------------------------------------------------------------------
# ۳) رتبه‌بندی دقیق بر اساس تعداد پیام
# ---------------------------------------------------------------------------
def test_03_ranking():
    print("\n### ۳) رتبه‌بندی نزولی بر اساس تعداد پیام — بدون خطا")
    chat = fresh(-51003)
    counts = {501: 7, 502: 30, 503: 1, 504: 7, 505: 12}
    for user_id, count in counts.items():
        for _ in range(count):
            group_stats.add_message(chat, user_id, f"u{user_id}", f"کاربر {user_id}")

    text, _entities = report.build_report(chat, member_count=10)
    names = [line.split(" : ", 1)[1] for line in text.splitlines()
             if line.startswith("⬟")]
    expected_first = ["@u502", "@u505", "@u501", "@u504", "@u503"]
    check("ترتیب دقیقاً بر اساس بیشترین پیام است",
          names[:5] == expected_first, names[:5])

    # اجرای دوباره باید دقیقاً همان خروجی را بدهد (مرتب‌سازی قطعی)
    repeats = {report.report_text(chat, member_count=10) for _ in range(5)}
    check("خروجی تکرارپذیر است (بدون خطای ترتیب)", len(repeats) == 1)

    # تساوی در تعداد پیام: ترتیب بر اساس شناسه، قطعی
    tie = [entry for entry in group_stats.top_users(chat, limit=10)
           if entry[1]["messages"] == 7]
    check("دو کاربر با تعداد برابر هر دو در لیست‌اند",
          {user_id for user_id, _ in tie} == {"501", "504"},
          [user_id for user_id, _ in tie])


# ---------------------------------------------------------------------------
# ۴) کمتر از ده کاربر ⇒ ردیف‌های «----»
# ---------------------------------------------------------------------------
def test_04_empty_rows():
    print("\n### ۴) کمتر از ۱۰ کاربر ⇒ جای خالی با «----»")
    chat = fresh(-51004)
    group_stats.add_message(chat, 601, "only", "تنها")
    text, _entities = report.build_report(chat, member_count=3)
    rows = [line for line in text.splitlines() if line.startswith("⬟")]
    check("همیشه ۱۰ ردیف چاپ می‌شود", len(rows) == 10, len(rows))
    check("ردیف اول کاربر واقعی است", rows[0] == "⬟ کاربر ۱ : @only", rows[0])
    check("ردیف‌های خالی «----» هستند",
          all(row.endswith(" : ----") for row in rows[1:]), rows[1:])

    empty_chat = fresh(-51005)
    text2, _e2 = report.build_report(empty_chat, member_count=0)
    rows2 = [line for line in text2.splitlines() if line.startswith("⬟")]
    check("گروه بدون کاربر: هر ده ردیف «----»",
          len(rows2) == 10 and all(row.endswith(" : ----") for row in rows2),
          rows2[:2])


# ---------------------------------------------------------------------------
# ۵) نام کاربر: یوزرنیم، بعد نام نمایشی، هرگز شناسه
# ---------------------------------------------------------------------------
def test_05_names():
    print("\n### ۵) نام: یوزرنیم ← نام نمایشی (هرگز شناسهٔ عددی)")
    check("یوزرنیم مقدم است",
          report.display_name({"username": "ali", "name": "علی"}) == "@ali")
    check("بدون یوزرنیم: نام نمایشی",
          report.display_name({"username": "", "name": "مریم"}) == "مریم")
    check("یوزرنیم عددی نادیده گرفته می‌شود",
          report.display_name({"username": "123456", "name": ""}) == "----")
    check("«unknown» قدیمی نام نیست",
          report.display_name({"username": "unknown", "name": ""}) == "----")
    check("کاربر ناشناس: ----", report.display_name({}) == "----")

    chat = fresh(-51006)
    group_stats.add_message(chat, 777, "", "نام نمایشی")
    text, _e = report.build_report(chat, member_count=1)
    check("در قالب، نام نمایشی چاپ شده", "⬟ کاربر ۱ : نام نمایشی" in text, text)
    check("شناسهٔ عددی در هیچ ردیفی چاپ نشده", "777" not in text, text)


# ---------------------------------------------------------------------------
# ۶) شمارندهٔ گیف
# ---------------------------------------------------------------------------
def test_06_gif_counter():
    print("\n### ۶) «تعداد کل گیف های ارسالی» یک شمارندهٔ واقعی است")
    chat = fresh(-51007)
    for _ in range(5):
        group_stats.add_gif(chat, 801, "giffer", "گیف‌باز")
    group_stats.add_gif(chat)                      # پیام بدون فرستندهٔ مشخص
    group_stats.flush()

    group = group_stats.get_stats(chat)
    check("شمارندهٔ گروه ۶ شد", group["gifs"] == 6, group.get("gifs"))
    check("شمارندهٔ کاربر ۵ شد",
          group["users"]["801"]["gifs"] == 5, group["users"]["801"])
    text, _e = report.build_report(chat, member_count=2)
    check("عدد در قالب فارسی چاپ شده",
          "تعداد کل گیف های ارسالی : ۶" in text, text)
    check("صفر پیش‌فرض برای گروه تازه",
          group_stats.gif_count(fresh(-51008)) == 0)


# ---------------------------------------------------------------------------
# ۷) تعداد اعضا: ذخیره در موفقیت، استفاده از مقدار ذخیره‌شده در خطا
# ---------------------------------------------------------------------------
def test_07_members():
    print("\n### ۷) تعداد اعضا: ذخیره می‌شود و در خطا صفرِ دروغ نمی‌دهیم")
    chat = fresh(-51009)
    check("ذخیرهٔ عدد اعضا", group_stats.record_members(chat, 17) == 17)
    check("خواندن همان عدد", group_stats.member_count(chat) == 17)
    text, _e = report.build_report(chat, member_count=0)
    check("با member_count=0 از عدد ذخیره‌شده استفاده می‌شود",
          "تعداد اعضا گروه : ۱۷" in text, text)
    check("عدد نامعتبر ذخیره نمی‌شود",
          group_stats.record_members(chat, "نامعتبر") is None)
    check("عدد منفی ذخیره نمی‌شود", group_stats.record_members(chat, -3) is None)


# ---------------------------------------------------------------------------
# ۸) مسیر واقعی هندلر: «آمار گپ» و «آمار گروه»
# ---------------------------------------------------------------------------
def _load_handler_test_module():
    path = ROOT / "tests" / "test_user_message_purge.py"
    spec = importlib.util.spec_from_file_location("_stats_handler_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FullChannel:
    def __init__(self, count):
        self.full_chat = type("Full", (), {"participants_count": count})()


class _StatsClient:
    def __init__(self, member_count, fail=False):
        self.member_count = member_count
        self.fail = fail
        self.calls = []

    async def get_input_entity(self, chat_id):
        return "peer"

    async def __call__(self, request, *args, **kwargs):
        self.calls.append(request.__class__.__name__)
        if self.fail:
            raise RuntimeError("API down")
        return _FullChannel(self.member_count)


class _StatsEvent:
    def __init__(self, chat_id, sender, text):
        self.chat_id = chat_id
        self.sender = sender
        self.sender_id = sender.id
        self.is_private = False
        self.out = False
        self._chat = type("Chat", (), {"id": chat_id, "title": "گروه تست"})()
        self.message = type("Msg", (), {
            "id": 900, "message": text, "caption": "", "reply_to_msg_id": None,
            "reply_to": None, "entities": None, "file": None, "media": None,
            "fwd_from": None, "edit_date": None,
        })()
        self.reply_to = None
        self.replies = []
        self.reply_entities = []

    async def get_sender(self):
        return self.sender

    async def get_chat(self):
        return self._chat

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        self.reply_entities.append(kwargs.get("formatting_entities"))
        return type("Sent", (), {"id": 7000 + len(self.replies)})()


def test_08_handler_route():
    print("\n### ۸) مسیر واقعی هندلر برای «آمار گپ» و «آمار گروه»")
    import handlers.message_handler as mh

    helpers = _load_handler_test_module()

    for command in ("آمار گپ", "آمار گروه"):
        chat = fresh(-51010)
        for _ in range(4):
            group_stats.add_message(chat, 901, "ali", "علی")
        client = _StatsClient(member_count=3)
        bot, _queue = helpers._handler_bot(client)
        event = _StatsEvent(chat, helpers._HandlerUser(901, "ali", "علی"), command)

        async def scenario(ev=event, b=bot, c=chat):
            await mh.handle_new_message(b, ev)
            for _ in range(200):
                await asyncio.sleep(0)
                if ev.replies:
                    break

        asyncio.run(scenario())
        body = event.replies[-1] if event.replies else ""
        check(f"«{command}» پاسخ داد", bool(body), event.replies)
        check(f"«{command}»: قالب جدید (آمار کل گروه)", "📊 آمار کل گروه" in body,
              body[:60])
        check(f"«{command}»: گیف و اعضا در متن هستند",
              "تعداد کل گیف های ارسالی" in body and "تعداد اعضا گروه" in body)
        entities = (event.reply_entities[-1] or []) if event.reply_entities else []
        check(f"«{command}»: entityها ارسال شدند (Bold + نقل‌قول)",
              any(isinstance(e, MessageEntityBold) for e in entities)
              and any(isinstance(e, MessageEntityBlockquote) for e in entities),
              [type(e).__name__ for e in entities][:4])

    # سرور خطا بدهد ⇒ از عدد ذخیره‌شده استفاده می‌شود، قالب خراب نمی‌شود.
    chat = fresh(-51011)
    group_stats.record_members(chat, 42)
    client = _StatsClient(member_count=0, fail=True)
    bot, _queue = helpers._handler_bot(client)
    event = _StatsEvent(chat, helpers._HandlerUser(902, None, "سارا"), "آمار گروه")

    async def scenario2():
        await mh.handle_new_message(bot, event)
        for _ in range(200):
            await asyncio.sleep(0)
            if event.replies:
                break

    asyncio.run(scenario2())
    body = event.replies[-1] if event.replies else ""
    check("خطای سرور: قالب باز هم ارسال می‌شود", "📊 آمار کل گروه" in body, body[:40])
    check("خطای سرور: تعداد اعضا از حافظه (۴۲) خوانده شد",
          "تعداد اعضا گروه : ۴۲" in body, body)

    source = Path(mh.__file__).read_text(encoding="utf-8")
    check("هندلر از گزارش جدید استفاده می‌کند",
          "build_group_stats_report(" in source)
    check("چاپ‌های دیباگِ print حذف شد",
          "FINAL MEMBER COUNT" not in source)


# ---------------------------------------------------------------------------
# ۹) هزینهٔ ساخت قالب (بدون رگرسیون عملکرد)
# ---------------------------------------------------------------------------
def _gif_message(message_id=901):
    """پیامِ گیفِ واقعی (ساختار SPlusthon: document + DocumentAttributeAnimated)."""
    from splusthon.tl.types import (DocumentAttributeAnimated,
                                    MessageMediaDocument)

    document = type("Doc", (), {
        "mime_type": "video/mp4",
        "attributes": [DocumentAttributeAnimated()],
    })()
    media = MessageMediaDocument(document=document)
    return type("Msg", (), {
        "id": message_id, "message": "", "caption": "", "reply_to_msg_id": None,
        "reply_to": None, "entities": None, "file": None, "media": media,
        "document": document, "gif": True, "animation": None,
        "fwd_from": None, "edit_date": None,
    })()


def _photo_message(message_id=902):
    """پیامِ عکس بدون متن — همان مسیری که قبلاً هیچ شمارشی نداشت."""
    from splusthon.tl.types import MessageMediaPhoto

    return type("Msg", (), {
        "id": message_id, "message": "", "caption": "", "reply_to_msg_id": None,
        "reply_to": None, "entities": None, "file": None,
        "media": MessageMediaPhoto(), "document": None, "gif": False,
        "animation": None, "fwd_from": None, "edit_date": None,
    })()


def test_10_gif_counted_from_real_handler():
    print("\n### ۱۰) شمارش گیف از مسیر واقعی هندلر (نه فقط تابع خالص)")
    import handlers.message_handler as mh
    from modules import user_activity

    helpers = _load_handler_test_module()

    # --- الف) گیف ---
    chat = fresh(-51013)
    client = _StatsClient(member_count=2)
    bot, _queue = helpers._handler_bot(client)
    user = helpers._HandlerUser(1101, "gifuser", "گیف باز")
    event = _StatsEvent(chat, user, "")
    event.message = _gif_message()
    event.reply_to = None

    async def scenario():
        await mh.handle_new_message(bot, event)
        for _ in range(300):
            await asyncio.sleep(0)
            if group_stats.gif_count(chat):
                break

    asyncio.run(scenario())
    check("گیفِ واقعی در شمارندهٔ گروه ثبت شد",
          group_stats.gif_count(chat) == 1, group_stats.gif_count(chat))
    users = group_stats.get_stats(chat).get("users", {})
    check("شمارندهٔ گیف کاربر هم ثبت شد",
          users.get("1101", {}).get("gifs") == 1, users.get("1101"))
    check("فعالیت کاربر هم ثبت شد (gifs)",
          user_activity.get(chat, 1101).get("gifs", 0) >= 1,
          user_activity.get(chat, 1101))

    # --- ب) عکس بدون متن ---
    photo_chat = fresh(-51014)
    bot2, _queue2 = helpers._handler_bot(_StatsClient(member_count=2))
    photo_user = helpers._HandlerUser(1102, None, "عکاس")
    photo_event = _StatsEvent(photo_chat, photo_user, "")
    photo_event.message = _photo_message()

    async def scenario2():
        await mh.handle_new_message(bot2, photo_event)
        for _ in range(300):
            await asyncio.sleep(0)
            if user_activity.get(photo_chat, 1102).get("last"):
                break

    asyncio.run(scenario2())
    activity = user_activity.get(photo_chat, 1102)
    check("پیامِ فقط‌رسانه‌ای هم در فعالیت ثبت شد",
          bool(activity.get("last")), activity)
    check("شمارندهٔ گیف با عکس بالا نمی‌رود",
          group_stats.gif_count(photo_chat) == 0,
          group_stats.gif_count(photo_chat))

    # --- ج) پیام متنی همچنان در آمار پیام‌ها ثبت می‌شود ---
    text_chat = fresh(-51015)
    bot3, _queue3 = helpers._handler_bot(_StatsClient(member_count=2))
    text_event = _StatsEvent(text_chat, helpers._HandlerUser(1103, "txt", "متن"),
                             "سلام به همه")

    async def scenario3():
        await mh.handle_new_message(bot3, text_event)
        for _ in range(300):
            await asyncio.sleep(0)
            if group_stats.get_stats(text_chat).get("messages"):
                break

    asyncio.run(scenario3())
    group = group_stats.get_stats(text_chat)
    check("پیام متنی در «کل پیام‌ها» شمرده شد", group.get("messages") == 1,
          group.get("messages"))
    check("نام نمایشی برای رتبه‌بندی ذخیره شد",
          group["users"].get("1103", {}).get("name") == "متن",
          group["users"].get("1103"))


def test_09_performance():
    print("\n### ۹) هزینهٔ ساخت قالب (میکروثانیه)")
    chat = fresh(-51012)
    for index in range(10):
        for _ in range(index + 1):
            group_stats.add_message(chat, 1000 + index, f"u{index}", f"کاربر {index}")

    report.build_report(chat, member_count=10)       # warm-up
    rounds = 2000
    start = time.perf_counter()
    for _ in range(rounds):
        report.build_report(chat, member_count=10)
    per_call = (time.perf_counter() - start) / rounds * 1_000_000
    print(f"  ⏱ build_report(۱۰ کاربر) : {per_call:.2f} µs")
    check("ساخت قالب زیر ۲ میلی‌ثانیه است", per_call < 2000, per_call)

    names = [f"@user{index}" for index in range(10)]
    start = time.perf_counter()
    for _ in range(rounds):
        for name in names:
            report.display_name({"username": name, "name": ""})
    per_name = (time.perf_counter() - start) / (rounds * len(names)) * 1_000_000
    print(f"  ⏱ display_name         : {per_name:.2f} µs")
    check("انتخاب نام زیر ۱۰۰ میکروثانیه است", per_name < 100, per_name)


def main():
    print("=" * 62)
    print("🧪 تست «📊 آمار کل گروه» و شمارندهٔ گیف")
    print("=" * 62)
    test_01_exact_template()
    test_02_entities()
    test_03_ranking()
    test_04_empty_rows()
    test_05_names()
    test_06_gif_counter()
    test_07_members()
    test_08_handler_route()
    test_10_gif_counted_from_real_handler()
    test_09_performance()
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
