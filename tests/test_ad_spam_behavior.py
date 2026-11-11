# -*- coding: utf-8 -*-
"""🧪 تستِ تشخیصِ «تبلیغِ کاربرنما» (behavior-based).

اجرا:

    PYTHONPATH=. python3 tests/test_ad_spam_behavior.py

سناریوی اصلی، عیناً همان چیزی است که کارفرما فرستاد: حسابی با نامِ کاملاً
عادی («نیکا») به چند کاربرِ مختلف ریپلای می‌کند («سلام 👋»، «تایید شماره
بدم»، «هوا چطوره خیلی سرد شده»، «ف میخوای ؟»، «خاخ») و بعد خطِ خدمات
می‌نویسد («خدمات میدم»). هیچ مارکرِ هویتی هم ندارد.

قاعده: پیامِ «خدمات میدم» باید حذف شود و **کلِ ردِ** همان کمپین (۵ پیامِ
قبلیِ همان کاربر) هم پاک شود؛ ولی کاربرِ عادی که یک‌بار چنین جمله‌ای
می‌نویسد باید سالم بماند.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="adspam_behavior_test_")
os.environ["BOT_HOME"] = _TMP

from modules.ad_spam_guard import behavior, detector, storage  # noqa: E402
from modules.ad_spam_guard import patterns as P  # noqa: E402

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


class FakeClient:
    def __init__(self):
        self.deleted = []
        self.sent = []

    async def delete_messages(self, chat_id, ids):
        self.deleted.extend(int(i) for i in ids)
        return True

    async def send_message(self, target, text, formatting_entities=None):
        self.sent.append(text)
        return SimpleNamespace(id=9000 + len(self.sent), message=text)


class FakeLogger:
    def log_info(self, *a, **k):
        pass

    def log_error(self, *a, **k):
        pass


class FakeBot:
    def __init__(self):
        self.client = FakeClient()
        self.logger = FakeLogger()
        self.bot_account_id = 42
        self.reply_input_peer_cache = {}


class FakeEvent:
    is_private = False

    def __init__(self, text, message_id=1, reply_to_user=None):
        self.replies = []
        reply_to = None
        if reply_to_user is not None:
            reply_to = SimpleNamespace(from_id=SimpleNamespace(user_id=reply_to_user))
        self.message = SimpleNamespace(id=message_id, message=text,
                                      reply_to=reply_to)

    async def reply(self, text, formatting_entities=None):
        self.replies.append(text)
        return SimpleNamespace(id=999)


CHAT = "-1001999000111"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def fresh(clock_holder):
    behavior.TRACKER.reset()
    detector.bind_queue(None)
    clock_holder[0] = 0.0
    behavior.TRACKER._clock = lambda: clock_holder[0]


def no_admin(monkeypatch_value=False):
    """ادمین‌ها در این تست معاف نشوند تا رفتارِ موتور سنجیده شود."""
    import modules.admin_tools as admin_tools

    admin_tools.has_admin_permission = lambda *a, **k: monkeypatch_value


# --------------------------------------------------------------------- ۱
def test_sequence_from_screenshot():
    print("\n=== ۱. سناریوی عکسِ کارفرما (رباتِ کاربرنما) ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    # نام/یوزرنیمِ کاملاً عادی — هیچ مارکرِ هویتی
    sender = SimpleNamespace(id=777, first_name="نیکا", last_name=None,
                             username="nika_1990")

    messages = [
        ("سلام 👋", 11),
        ("تایید شماره بدم", 12),
        ("هوا چطوره خیلی سرد شده", 13),
        ("ف میخوای ؟", 14),
        ("خاخ", None),
        ("خدمات میدم", None),
    ]

    handled = []
    for index, (text, target) in enumerate(messages):
        clock[0] = index * 8.0
        event = FakeEvent(text, message_id=500 + index, reply_to_user=target)
        handled.append(run(detector.guard_message(
            bot, event, CHAT, 777, sender, 500 + index)))

    check("سه پیامِ اول (شروعِ گپ) چیزی را حذف نمی‌کنند",
          handled[:3] == [False, False, False], handled)
    check("از چهارمی به بعد (الگویِ رباتی) گرفته می‌شود",
          all(handled[3:]), handled)
    check("پیامِ «خدمات میدم» گرفته شد", handled[-1] is True, handled)
    check("همان پیام حذف شد", 505 in bot.client.deleted, bot.client.deleted)
    check("ردِ کمپین (۵ پیامِ قبلی) هم پاک شد",
          all(mid in bot.client.deleted for mid in (500, 501, 502, 503, 504)),
          bot.client.deleted)
    check("پیامِ هشدار با قالبِ عکس رفت",
          any("دلیل : تبلیغات گروهی" in text for text in bot.client.sent),
          bot.client.sent)

    # پیام بعدیِ همان حساب (خدماتش تمام شده) نباید دوباره جریمه شود؟
    # خیر: هر پیامِ تبلیغاتیِ بعدی هم گرفته می‌شود.
    clock[0] = 60.0
    event = FakeEvent("اکانت میفروشیم", message_id=506)
    check("پیامِ تبلیغاتیِ بعدی هم گرفته می‌شود",
          run(detector.guard_message(bot, event, CHAT, 777, sender, 506)) is True)


def test_normal_user_is_safe():
    print("\n=== ۲. کاربرِ عادی سالم می‌ماند ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=900, first_name="رضا", username="reza")

    # یک‌بار «خدمات میدم» در گفت‌وگوی عادی
    event = FakeEvent("خدمات میدم", message_id=600)
    check("یک پیامِ تنها با جملهٔ خدمات حذف نمی‌شود",
          run(detector.guard_message(bot, event, CHAT, 900, sender, 600)) is False)
    check("هیچ پیامی حذف نشد", bot.client.deleted == [], bot.client.deleted)

    # کاربرِ خوش‌صحبت که به دو نفر ریپلای می‌کند
    fresh(clock)
    storage.enable(CHAT)
    for index, (text, target) in enumerate([
        ("سلام", 11), ("خوبی؟", 12), ("خوبم مرسی", 11),
    ]):
        clock[0] = index * 20.0
        event = FakeEvent(text, message_id=700 + index, reply_to_user=target)
        run(detector.guard_message(bot, event, CHAT, 900, sender, 700 + index))
    check("گپِ عادیِ چندنفره حذف نمی‌شود", bot.client.deleted == [],
          bot.client.deleted)

    # تبلیغ با آیدی ولی در یک پیام و بدونِ رفتارِ رباتی
    fresh(clock)
    storage.enable(CHAT)
    event = FakeEvent("سلام، من خدمات میدم @reza_support", message_id=800)
    check("تبلیغِ یک‌پیامیِ کاربرِ عادی حذف نمی‌شود (ضدآفتِ مثبت)",
          run(detector.guard_message(bot, event, CHAT, 900, sender, 800)) is False,
          bot.client.deleted)


def test_repeat_loop():
    print("\n=== ۳. حلقهٔ تکرارِ عینِ یک متن ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=901, first_name="spam", username="spam")
    results = []
    for index in range(4):
        clock[0] = index * 15.0
        event = FakeEvent("بیا پیوی", message_id=850 + index)
        results.append(run(detector.guard_message(
            bot, event, CHAT, 901, sender, 850 + index)))
    check("پس از تکرار، پیام گرفته می‌شود", any(results), results)
    check("حداقل یک پیام حذف شد", bool(bot.client.deleted), bot.client.deleted)


def test_reply_bot_without_cta():
    print("\n=== ۴. رباتِ ریپلای‌زن (بدونِ خطِ تبلیغ) ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=902, first_name="n", username="n")
    results = []
    for index, target in enumerate([11, 12, 13, 14, 15, 16]):
        clock[0] = index * 10.0
        event = FakeEvent("سلام 👋", message_id=900 + index,
                          reply_to_user=target)
        results.append(run(detector.guard_message(
            bot, event, CHAT, 902, sender, 900 + index)))
    check("ریپلای به ۶ کاربرِ مختلف در یک دقیقه ⇒ گرفته می‌شود",
          results[-1] is True, results)


def test_admin_exempt():
    print("\n=== ۵. ادمینِ گروه معاف است ===")
    clock = [0.0]
    fresh(clock)
    no_admin(True)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=903, first_name="admin", username="admin")
    for index, (text, target) in enumerate([
        ("سلام 👋", 11), ("تایید شماره بدم", 12), ("ف میخوای ؟", 13),
        ("خدمات میدم", None),
    ]):
        clock[0] = index * 5.0
        event = FakeEvent(text, message_id=950 + index, reply_to_user=target)
        run(detector.guard_message(bot, event, CHAT, 903, sender, 950 + index))
    check("پیامِ ادمین حذف نمی‌شود", bot.client.deleted == [], bot.client.deleted)


def test_memory_bounds():
    print("\n=== ۶. حافظه محدود می‌ماند ===")
    tracker = behavior.BehaviorTracker(clock=lambda: 0.0, max_keys=3,
                                       max_records=4)
    for user in range(10):
        tracker.observe("-1", str(user), user, "سلام")
    check("سقفِ تعداد کاربران رعایت می‌شود", len(tracker._store) <= 3,
          len(tracker._store))
    tracker.observe("-1", "0", 100, "سلام")
    check("سقفِ رکوردهای هر کاربر رعایت می‌شود",
          max(len(b) for b in tracker._store.values()) <= 4)
    tracker.forget_chat("-1")
    check("forget_chat وضعیتِ گروه را پاک می‌کند", len(tracker._store) == 0)



def test_bio_bait_single_greeting():
    print("\n=== ۷. «سلام»ِ تکراری برای بیو-چک ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=910, first_name="نیکا", username="nika_1990")

    # یک «سلام»ِ تنها ⇒ دست‌نخورده
    clock[0] = 0.0
    event = FakeEvent("سلام 👋", message_id=1000, reply_to_user=11)
    check("یک سلامِ تنها حذف نمی‌شود",
          run(detector.guard_message(bot, event, CHAT, 910, sender, 1000)) is False)

    # همان «سلام» به کاربرِ دوم ⇒ تشخیص
    clock[0] = 12.0
    event = FakeEvent("سلام 👋", message_id=1001, reply_to_user=12)
    check("تکرارِ همان سلام برای نفرِ دوم گرفته می‌شود",
          run(detector.guard_message(bot, event, CHAT, 910, sender, 1001)) is True,
          bot.client.deleted)


def test_endless_repetitive_chatter():
    print("\n=== ۸. گپِ تکراریِ بی‌پایان (بدونِ کلمهٔ تبلیغ) ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=911, first_name="n", username="n")
    seq = ["اوف", "داغم", "اوف", "گرمه", "اوف", "داغم"]
    results = []
    for index, text in enumerate(seq):
        clock[0] = index * 20.0
        event = FakeEvent(text, message_id=1100 + index, reply_to_user=11)
        results.append(run(detector.guard_message(
            bot, event, CHAT, 911, sender, 1100 + index)))
    check("گپِ تکراریِ بی‌پایان گرفته می‌شود", any(results), results)
    check("پیام‌ها پاک شدند", bool(bot.client.deleted), bot.client.deleted)


def test_fast_replier():
    print("\n=== ۹. ریپلای‌های سریعِ پشتِ‌سرهم ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=912, first_name="n", username="n")
    seq = [("سلام", 11), ("خوبی", 12), ("چخبر", 13), ("خاخ", 14)]
    results = []
    for index, (text, target) in enumerate(seq):
        clock[0] = index * 2.0          # هر ۲ ثانیه یک پیام
        event = FakeEvent(text, message_id=1200 + index, reply_to_user=target)
        results.append(run(detector.guard_message(
            bot, event, CHAT, 912, sender, 1200 + index)))
    check("ریپلایِ سریع به چند نفر گرفته می‌شود", any(results), results)


def test_warn_template():
    print("\n=== ۱۰. قالبِ پیام هشدار (مطابقِ اسکرین‌شات) ===")
    from modules.ad_spam_guard import texts

    text, entities = texts.warn_payload("@Linx_cox", 1, threshold=5)
    expected = (
        "⚠️ کاربر  @Linx_cox\n"
        "پیام شما پاک شد | دلیل : تبلیغات گروهی\n"
        "\n"
        "تعداد اخطار : 1/5\n"
        "انجام دهنده : هوش مصنوعی ضد تبلیغ"
    )
    check("متنِ هشدار دقیقاً همان عکس است", text == expected, repr(text))
    kinds = [type(e).__name__ for e in entities]
    check("کلِ متن Bold است", "MessageEntityBold" in kinds, kinds)
    bold = [e for e in entities if type(e).__name__ == "MessageEntityBold"]
    check("Bold از ابتدا تا انتهای متن است",
          bold and bold[0].offset == 0
          and bold[0].length == len(text.encode("utf-16-le")) // 2, bold)
    check("خطِ عنوان نقل‌قول هم دارد", "MessageEntityBlockquote" in kinds, kinds)
    quote = [e for e in entities
             if type(e).__name__ == "MessageEntityBlockquote"]
    check("نقل‌قول فقط خطِ عنوان را می‌گیرد",
          quote and quote[0].offset == 0
          and quote[0].length == len("⚠️ کاربر  @Linx_cox".encode("utf-16-le")) // 2,
          quote)
    check("شمارنده به‌شکلِ count/threshold است",
          "تعداد اخطار : 3/7" in texts.render_warn("@x", 3, threshold=7))
    check("دلیل قابلِ تغییر است",
          "دلیل : کلمه ممنوعه" in texts.render_warn("@x", 2, "کلمه ممنوعه"))


def test_bot_reply_spam():
    print("\n=== ۱۱. ریپلایِ تکراری به خودِ رباتِ روباه ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()                      # bot_account_id = 42
    sender = SimpleNamespace(id=913, first_name="نیکا", username="nika_1990")

    clock[0] = 0.0
    first = FakeEvent("سلام", message_id=1300, reply_to_user=42)
    check("یک ریپلای به ربات حذف نمی‌شود",
          run(detector.guard_message(bot, first, CHAT, 913, sender, 1300)) is False)

    clock[0] = 9.0
    second = FakeEvent("سلام", message_id=1301, reply_to_user=42)
    check("تکرارِ همان جمله برای ربات گرفته می‌شود",
          run(detector.guard_message(bot, second, CHAT, 913, sender, 1301)) is True,
          bot.client.deleted)


def test_same_target_repeat():
    print("\n=== ۱۲. تکرارِ یک جمله برای یک مخاطب ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)
    bot = FakeBot()
    sender = SimpleNamespace(id=914, first_name="n", username="n")
    results = []
    for index in range(3):
        clock[0] = index * 25.0
        event = FakeEvent("خاخ", message_id=1400 + index, reply_to_user=777)
        results.append(run(detector.guard_message(
            bot, event, CHAT, 914, sender, 1400 + index)))
    check("سه بار «خاخ» برای یک نفر گرفته می‌شود", any(results), results)


def test_warning_counter_uses_real_system():
    print("\n=== ۱۳. تعداد اخطار از سیستم اخطارِ ربات ===")
    clock = [0.0]
    fresh(clock)
    no_admin(False)
    storage.enable(CHAT)

    class Tracker:
        def __init__(self):
            self.counts = {}

        def increment(self, chat_id, user_id):
            key = (str(chat_id), str(user_id))
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

    class BotWithTracker(FakeBot):
        def __init__(self):
            super().__init__()
            self.tracker = Tracker()

    bot = BotWithTracker()
    sender = SimpleNamespace(id=915, first_name="n", username="n")
    for index in range(2):
        clock[0] = index * 10.0
        event = FakeEvent("سلام", message_id=1500 + index, reply_to_user=42)
        run(detector.guard_message(bot, event, CHAT, 915, sender, 1500 + index))

    check("شمارنده در سیستم اخطارِ ربات بالا رفت",
          bot.tracker.counts.get((CHAT, "915"), 0) >= 1, bot.tracker.counts)
    check("پیامِ هشدار «1/5» نشان می‌دهد",
          any("تعداد اخطار : 1/5" in text for text in bot.client.sent),
          bot.client.sent)


def main():
    print("=" * 62)
    print("تستِ تشخیصِ رفتارِ ربات‌های کاربرنما")
    print("=" * 62)
    test_sequence_from_screenshot()
    test_normal_user_is_safe()
    test_repeat_loop()
    test_reply_bot_without_cta()
    test_admin_exempt()
    test_memory_bounds()
    test_bio_bait_single_greeting()
    test_endless_repetitive_chatter()
    test_fast_replier()
    test_warn_template()
    test_bot_reply_spam()
    test_same_target_repeat()
    test_warning_counter_uses_real_system()
    print("\n" + "=" * 62)
    print("نتیجه: %d پاس / %d شکست" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
