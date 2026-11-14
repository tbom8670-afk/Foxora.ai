# -*- coding: utf-8 -*-
"""🧪 تست قابلیت «تایم جفت» ( modules/mirror_time ).

اجرا:

    PYTHONPATH=. python3 tests/test_mirror_time.py

پوشش:

 ۱. تشخیصِ ساعتِ جفت و محاسبهٔ تایمِ بعدی (به وقتِ تهران)
 ۲. قالبِ پیام: «♡ ↻ تایم : 5:5 🦊🦋» و Bold روی کلِ متن (UTF-16)
 ۳. ارسالِ یک‌بار‌مصرف برای هر گروه — و تکرارنشدن پس از ری‌استارت
 ۴. پاسخِ «فدات» فقط روی ریپلایِ همان پیامِ تایم (نه گفت‌وگوی معمولی)
 ۵. پیدا‌کردنِ کارت با متن وقتی ارسال شناسه برنمی‌گرداند
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# محیط آزمون: فایل‌های runtime در پوشهٔ موقت (state واقعی دست نخورد).
_TMP = tempfile.mkdtemp(prefix="mirror_time_test_")
os.environ.setdefault("BOT_HOME", _TMP)
os.environ.setdefault("BOT_INSTANCE", "mirror_time_test")

from modules import group_storage  # noqa: E402
from modules.mirror_time import service, state, texts  # noqa: E402
from modules.time_utils import TEHRAN  # noqa: E402

PASS = 0
FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  ✅ %s" % label)
    else:
        FAIL += 1
        print("  ❌ %s %s" % (label, detail))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# ابزارِ تست
# ---------------------------------------------------------------------------
class Sent:
    def __init__(self, mid):
        self.id = mid


class FakeClient:
    def __init__(self, next_id=500, history=None, return_id=True):
        self.sent = []
        self.history = history or []
        self.next_id = next_id
        self.return_id = return_id

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        if not self.return_id:
            return None
        mid = self.next_id
        self.next_id += 1
        return Sent(mid)

    async def get_messages(self, peer, limit=20):
        return list(self.history)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def log_info(self, message):
        self.lines.append(("info", message))

    def log_error(self, message):
        self.lines.append(("error", message))


class FakeBot:
    def __init__(self, client):
        self.client = client
        self.logger = FakeLogger()
        # عمداً outgoing_sender ندارد: مسیرِ مستقیم تست می‌شود.


class FakeEvent:
    def __init__(self, chat_id, reply_to=None):
        self.chat_id = chat_id
        self.replies = []
        self.message = type("M", (), {})()
        if reply_to is not None:
            self.message.reply_to_msg_id = reply_to

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return Sent(9001)


def _at(hour, minute, second=0, day=13):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TEHRAN)


# ---------------------------------------------------------------------------
# ۱) تشخیصِ ساعتِ جفت
# ---------------------------------------------------------------------------
print("\n=== ۱) تشخیصِ ساعتِ جفت ===")
for hour in (1, 2, 5, 9, 10, 11, 12, 13, 23, 0):
    check("ساعتِ %d:%d جفت است" % (hour, hour),
          service.is_pair_time(_at(hour, hour)))
check("۵:۶ جفت نیست", not service.is_pair_time(_at(5, 6)))
check("۶:۵ جفت نیست", not service.is_pair_time(_at(6, 5)))

check("تایمِ بعدیِ ۵:۳ = ۵:۵",
      service.next_pair_time(_at(5, 3)) == _at(5, 5),
      service.next_pair_time(_at(5, 3)))
check("تایمِ بعدیِ ۵:۶ = ۶:۶",
      service.next_pair_time(_at(5, 6)) == _at(6, 6),
      service.next_pair_time(_at(5, 6)))
check("تایمِ بعدیِ ۵:۵:۲۰ = ۶:۶ (همان دقیقه دوباره صف نمی‌شود)",
      service.next_pair_time(_at(5, 5, 20)) == _at(6, 6),
      service.next_pair_time(_at(5, 5, 20)))
check("تایمِ بعدیِ ۲۳:۳۰ = فردا ۰:۰",
      service.next_pair_time(_at(23, 30)) == _at(0, 0, day=14),
      service.next_pair_time(_at(23, 30)))
check("تایمِ بعدیِ ۹:۵۹ = ۱۰:۱۰",
      service.next_pair_time(_at(9, 59)) == _at(10, 10))

# ---------------------------------------------------------------------------
# ۲) قالبِ پیام و Bold
# ---------------------------------------------------------------------------
print("\n=== ۲) قالبِ پیام و Bold ===")
text = texts.render(5, 5)
check("متن دقیقاً «♡ ↻ تایم : 5:5 🦊🦋» است",
      text == "♡ ↻ تایم : 5:5 🦊🦋", repr(text))
payload_text, entities = texts.payload(5, 5)
check("payload همان متن را می‌دهد", payload_text == text)
check("یک entity دارد", len(entities) == 1, entities)
entity = entities[0]
check("نوعِ entity = Bold", type(entity).__name__ == "MessageEntityBold",
      type(entity).__name__)
check("offset صفر است", entity.offset == 0)
check("طولِ entity = طولِ UTF-16 متن",
      entity.length == len(text.encode("utf-16-le")) // 2,
      (entity.length, len(text.encode("utf-16-le")) // 2))
check("بدونِ صفرِ پیشتاز (۵:۵ ⇒ 5:5)", texts.render(5, 5).count("0") == 0)
check("تایمِ عصر ۱۸:۱۸ هم تشخیص داده می‌شود", service.is_pair_time(_at(18, 18)))
check("تایمِ بعدیِ ۱۸:۰۰ = ۱۸:۱۸",
      service.next_pair_time(_at(18, 0)) == _at(18, 18),
      service.next_pair_time(_at(18, 0)))
check("۱۸:۱۸ ⇒ متنِ «6:6» (ساعتِ ۱۲ساعته)",
      texts.render(18, 18) == "♡ ↻ تایم : 6:6 🦊🦋", repr(texts.render(18, 18)))
check("۱۳:۱۳ ⇒ متنِ «1:1»", texts.render(13, 13) == "♡ ↻ تایم : 1:1 🦊🦋",
      repr(texts.render(13, 13)))
check("۲۳:۲۳ ⇒ متنِ «11:11»", texts.render(23, 23) == "♡ ↻ تایم : 11:11 🦊🦋",
      repr(texts.render(23, 23)))
check("۶:۶ صبح و عصر هر دو «6:6» هستند",
      texts.render(6, 6) == texts.render(18, 18))
check("حالتِ ۲۴ساعته انتخاب‌شدنی است (۱۸:۱۸)",
      texts.render(18, 18, "24") == "♡ ↻ تایم : 18:18 🦊🦋",
      repr(texts.render(18, 18, "24")))
check("پیش‌فرض ۱۲ساعته است", texts.clock_mode() == "12")

# ---------------------------------------------------------------------------
# ۳) ارسالِ یک‌بار‌مصرف + ری‌استارت
# ---------------------------------------------------------------------------
print("\n=== ۳) ارسالِ تایم جفت (یک بار، حتی بعد از ری‌استارت) ===")
state.reset()
group_storage.activate_group(-100111, "گروه الف")
group_storage.activate_group(-100222, "گروه ب")
group_storage.activate_group(-100333, "گروه خاموش")
_group_data = group_storage.load_groups()
_group_data["-100333"]["active"] = False
group_storage.save_groups(_group_data)

client = FakeClient()
bot = FakeBot(client)
moment = _at(5, 5)

first = run(service.send_slot(bot, moment, logger=bot.logger))
check("برای دو گروهِ فعال دقیقاً دو ارسال", first == 2, first)
check("گروهِ خاموش پیامی نگرفت",
      all(item["chat_id"] != -100333 for item in client.sent), client.sent)
check("متنِ ارسالی همان قالب است",
      all(item["text"] == "♡ ↻ تایم : 5:5 🦊🦋" for item in client.sent),
      [item["text"] for item in client.sent])
check("entity همراهِ ارسال رفته است",
      all(item["kwargs"].get("formatting_entities") for item in client.sent))

second = run(service.send_slot(bot, moment, logger=bot.logger))
check("همان تایم دوباره فرستاده نمی‌شود", second == 0
      and len(client.sent) == 2, len(client.sent))

# شبیه‌سازیِ ری‌استارت: کشِ حافظه پاک می‌شود، فایل می‌ماند.
state._cache = None
state._cache_mtime = None
check("وضعیت از دیسک خوانده می‌شود (ری‌استارت)",
      state.already_sent(state.slot_key(moment), -100111))
third = run(service.send_slot(bot, moment, logger=bot.logger))
check("بعد از ری‌استارت هم تکرار نمی‌شود", third == 0
      and len(client.sent) == 2, len(client.sent))
check("شناسهٔ کارت ذخیره شده است",
      state.known_ids(-100111) == {500}, state.known_ids(-100111))

# تایمِ بعدی تازه است
later = run(service.send_slot(bot, _at(6, 6), logger=bot.logger))
check("تایمِ بعدی (۶:۶) تازه ارسال می‌شود", later == 2, later)

# ---------------------------------------------------------------------------
# ۴) پاسخِ اختصاصی «فدات»
# ---------------------------------------------------------------------------
print("\n=== ۴) پاسخِ «فدات» فقط روی پیامِ تایم ===")
state.reset()
client2 = FakeClient(next_id=777)
bot2 = FakeBot(client2)
run(service.send_slot(bot2, _at(7, 7), logger=bot2.logger))
card_id = min(state.known_ids(-100111))
check("شناسهٔ کارت ۷۷۷ ثبت شد", card_id == 777, card_id)

event = FakeEvent(-100111, reply_to=card_id)
handled = run(service.handle_reply(bot2, event, -100111, "فدات",
                                  logger=bot2.logger))
check("ریپلای «فدات» روی کارت ⇒ پاسخ داده می‌شود", handled is True)
check("پاسخ دقیقاً «فدامون 🥲» است",
      event.replies == ["فدامون 🥲"], event.replies)

for variant in ("فداتم", "فدات شم", "فدات ربات", "فداتت 🥺", "فدات"):
    ev = FakeEvent(-100111, reply_to=card_id)
    ok = run(service.handle_reply(bot2, ev, -100111, variant,
                                 logger=bot2.logger))
    check("«%s» هم پاسخ می‌گیرد" % variant, ok is True and ev.replies,
          ev.replies)

plain = FakeEvent(-100111, reply_to=None)
check("«فدات» بدونِ ریپلای ⇒ هیچ پاسخی",
      run(service.handle_reply(bot2, plain, -100111, "فدات")) is False
      and plain.replies == [])

other = FakeEvent(-100111, reply_to=4242)
check("ریپلای «فدات» روی پیامِ دیگرِ ربات ⇒ هیچ پاسخی",
      run(service.handle_reply(bot2, other, -100111, "فدات")) is False
      and other.replies == [])

unrelated = FakeEvent(-100111, reply_to=card_id)
check("پیامِ بی‌ربط به‌عنوان ریپلای ⇒ هیچ پاسخی",
      run(service.handle_reply(bot2, unrelated, -100111, "سلام")) is False
      and unrelated.replies == [])

nocard = FakeEvent(-100555, reply_to=1)
check("گروهی که کارتِ تایم ندارد ⇒ هیچ پاسخی",
      run(service.handle_reply(bot2, nocard, -100555, "فدات")) is False
      and nocard.replies == [])

# ---------------------------------------------------------------------------
# ۵) ارسالِ بدونِ شناسه (این نسخه اغلب id برنمی‌گرداند)
# ---------------------------------------------------------------------------
print("\n=== ۵) کارت بدونِ شناسه: پیدا‌شدن با متن ===")
state.reset()
no_id_client = FakeClient(return_id=False)
no_id_bot = FakeBot(no_id_client)
run(service.send_slot(no_id_bot, _at(9, 9), logger=no_id_bot.logger))
check("ارسال انجام شد حتی بدونِ شناسه", len(no_id_client.sent) == 2,
      len(no_id_client.sent))
check("شناسه ثبت نشده است (id=0)", state.known_ids(-100111) == set())

no_id_client.history = [Sent(1234), Sent(1233)]
no_id_client.history[0].message = "♡ ↻ تایم : 9:9 🦊🦋"
no_id_client.history[1].message = "پیامِ دیگر"
ev = FakeEvent(-100111, reply_to=1234)
ok = run(service.handle_reply(no_id_bot, ev, -100111, "فدات",
                              logger=no_id_bot.logger))
check("کارت از تاریخچه پیدا و پاسخ داده شد", ok is True and ev.replies)
check("شناسهٔ پیداشده ذخیره شد", state.known_ids(-100111) == {1234},
      state.known_ids(-100111))

# ---------------------------------------------------------------------------
print("\n=== ۶) خودِ حلقه: ارسال سرِ لحظه و فقط یک بار ===")
state.reset()
clock = {"now": _at(9, 58, 58)}
service.now_local = lambda: clock["now"]          # ساعتِ ساختگی برای تست
service.SLEEP_CHUNK = 0.05                       # تا تست ثانیه‌ها معطل نشود
loop_client = FakeClient(next_id=900)
loop_bot = FakeBot(loop_client)


async def _loop_case():
    task = asyncio.create_task(service.watcher(loop_bot, logger=loop_bot.logger))
    await asyncio.sleep(0.2)
    check("حلقه تا لحظهٔ تایم چیزی نمی‌فرستد", len(loop_client.sent) == 0,
          len(loop_client.sent))
    clock["now"] = _at(9, 59, 0)                  # ۵۹:۹ جفت نیست
    await asyncio.sleep(1.2)
    check("۹:۵۹ پیامی فرستاده نمی‌شود", len(loop_client.sent) == 0)
    clock["now"] = _at(10, 10, 0)                 # سرِ لحظهٔ تایم جفت
    await asyncio.sleep(1.2)
    check("در لحظهٔ ۱۰:۱۰ برای دو گروه فعال ارسال شد",
          len(loop_client.sent) == 2, len(loop_client.sent))
    check("متنِ ارسال ۱۰:۱۰ است",
          all(item["text"] == "♡ ↻ تایم : 10:10 🦊🦋"
              for item in loop_client.sent),
          [item["text"] for item in loop_client.sent])
    clock["now"] = _at(10, 10, 20)                # همان دقیقه، ۲۰ ثانیه بعد
    await asyncio.sleep(1.2)
    check("در همان دقیقه دوباره ارسال نمی‌شود", len(loop_client.sent) == 2,
          len(loop_client.sent))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


run(_loop_case())

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print("نتیجه: %d پاس / %d شکست" % (PASS, FAIL))
print("=" * 60)
sys.exit(1 if FAIL else 0)
