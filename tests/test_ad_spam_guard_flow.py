# -*- coding: utf-8 -*-
"""🧪 تستِ جریانِ «تبلیغ گر روشن → تایید» (رفعِ باگ‌های کارفرما).

اجرا:

    PYTHONPATH=. python3 tests/test_ad_spam_guard_flow.py

چه چیزی را ثابت می‌کند؟

۱. کارتِ تأییدیه **عیناً** مطابق اسکرین‌شات است: عنوان در نقل‌قول، فهرستِ
   پنج‌خطیِ پشتِ‌سرهم، «آیا این قابلیت فعال شود ؟» و دو خطِ «▪️تایید» /
   «▫️ لغو».
۲. کارت به‌صورتِ **پیامِ مستقل** فرستاده می‌شود، نه ریپلای روی دستور.
۳. «تایید/لغو» را **همان مسیرِ تبلیغ گر** مصرف می‌کند (نه مسیرِ گزارش) —
   حتی وقتی پروندهٔ گزارشی در انتظار است.
۴. هر ادمینِ گروه می‌تواند تأیید کند؛ کاربرِ بی‌مجوز پیامِ روشن می‌گیرد.
۵. وضعیتِ انتظار پس از ری‌استارت از دست نمی‌رود.
۶. خروجی‌های عمومیِ بسته (``__init__``) کامل است؛ نبودِ آن‌ها همان ریشهٔ
   باگِ «AttributeError ← افتادن در مسیرِ گزارش» بود.
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

_TMP = tempfile.mkdtemp(prefix="adspam_flow_test_")
os.environ["BOT_HOME"] = _TMP

import modules.ad_spam_guard as ad_guard  # noqa: E402
from modules.ad_spam_guard import detector, state, storage, texts  # noqa: E402

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
        self.sent = []

    async def send_message(self, target, text, formatting_entities=None):
        message = SimpleNamespace(id=5000 + len(self.sent), message=text)
        self.sent.append(SimpleNamespace(target=target, text=text,
                                         entities=formatting_entities,
                                         id=message.id))
        return message


class FakeLogger:
    def log_info(self, *args, **kwargs):
        pass

    def log_error(self, *args, **kwargs):
        pass


class FakeBot:
    def __init__(self):
        self.client = FakeClient()
        self.logger = FakeLogger()
        self.bot_account_id = 42
        self.reply_input_peer_cache = {}


class FakeEvent:
    is_private = False

    def __init__(self, reply_to_message_id=None):
        self.replies = []
        self.message = SimpleNamespace(
            id=100,
            reply_to=(SimpleArgsConstructor(reply_to_message_id)
                      if reply_to_message_id is not None else None),
        )

    async def reply(self, text, formatting_entities=None):
        self.replies.append(text)
        return SimpleNamespace(id=8888)


class SimpleArgsConstructor(SimpleNamespace):
    def __init__(self, message_id):
        super().__init__(reply_to_msg_id=message_id)


CHAT = "-1001555012345"
OWNER = "1001"
ADMIN2 = "1002"
RANDOM = "1003"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------- ۱
def test_template_exact():
    print("\n=== ۱. قالبِ تأییدیه عیناً مطابق اسکرین‌شات ===")
    text, entities = texts.confirm_enable()
    expected = (
        "🦠ضد تبلیغ گر پیام های کاربران و ربات ها\n"
        "\n"
        "» شناسایی پیام های تبلیغاتی ربات ها\n"
        "» شناسایی پیام های عادی تبلیغات\n"
        "» شناسایی هرزنامه های ربات ها\n"
        "» شناسایی با دقت 85٪ هوش مصنوعی\n"
        "» شناسایی پیام های عادی ربات ها\n"
        "\n"
        "آیا این قابلیت فعال شود ؟\n"
        "▪️تایید\n"
        "▫️ لغو"
    )
    check("متنِ کارت دقیقاً همان عکس است", text == expected, repr(text))
    check("فقط عنوان نقل‌قول دارد",
          [type(e).__name__ for e in entities] == ["MessageEntityBlockquote"],
          [type(e).__name__ for e in entities])
    if entities:
        entity = entities[0]
        check("نقل‌قول از ابتدای متن شروع می‌شود", entity.offset == 0, entity.offset)
        check("نقل‌قول فقط عنوان را می‌گیرد",
              entity.length == len(texts.CONFIRM_TITLE.encode("utf-16-le")) // 2,
              entity.length)
    lines = text.split("\n")
    first_item = lines.index(texts.CONFIRM_ITEMS[0])
    check("بین خط‌های فهرست خطِ خالی نیست",
          all(line.strip() for line in lines[first_item:first_item + 5]),
          lines[first_item:first_item + 5])
    check("بین عنوان و فهرست یک خطِ خالی است", lines[1] == "", lines[:3])
    check("قبل از «آیا این قابلیت…» یک خطِ خالی است",
          lines[first_item + 5] == "", lines[first_item + 4:first_item + 7])
    check("ترتیبِ «آیا … / تایید / لغو» درست است",
          lines[-3:] == [texts.CONFIRM_QUESTION, texts.CONFIRM_ACCEPT_LINE,
                         texts.CONFIRM_CANCEL_LINE], lines[-3:])


# --------------------------------------------------------------------- ۲
def test_word_variants():
    print("\n=== ۲. تشخیصِ «تایید/لغو» در همهٔ شکل‌ها ===")
    for word in ("تایید", "تأیید", "تایيد", "▪️تایید", "✅ تایید", "تایید!",
                 "تــایید", "تایید."):
        check("تایید: %r" % word, detector.is_confirm_word(word))
    for word in ("لغو", "▫️ لغو", "▫️لغو", "❌ لغو", "لغو!"):
        check("لغو: %r" % word, detector.is_cancel_word(word))
    for word in ("سلام", "تایید شد؟", "گزارش"):
        check("عادی: %r" % word, not detector.is_confirm_word(word))
    check("دستورِ روشن با نیم‌فاصله",
          storage.is_toggle_command("تبلیغ‌گر روشن") == "on")
    check("دستورِ خاموش با فاصلهٔ اضافه",
          storage.is_toggle_command(" تبلیغ گر  خاموش ") == "off")


# --------------------------------------------------------------------- ۳
def test_exports():
    print("\n=== ۳. خروجی‌های عمومیِ بسته (ریشهٔ باگِ AttributeError) ===")
    for name in ("handle_toggle", "handle_confirm", "is_confirm_word",
                 "is_cancel_word", "guard_message", "has_pending_confirm",
                 "texts", "state", "storage"):
        check("ad_spam_guard.%s موجود است" % name, hasattr(ad_guard, name))


# --------------------------------------------------------------------- ۴
def test_toggle_and_confirm_flow():
    print("\n=== ۴. جریان: «تبلیغ گر روشن» → تایید ===")
    original_is_admin = detector._is_admin
    detector._is_admin = lambda chat_id, user_id, sender: True
    state.clear_confirm(CHAT)
    storage.disable(CHAT)
    bot = FakeBot()
    event = FakeEvent()
    sender = SimpleNamespace(id=int(OWNER), first_name="osine", username="osine")

    handled = run(detector.handle_toggle(bot, event, CHAT, OWNER, sender, "on"))
    check("دستور روشن مصرف شد", handled is True)
    check("کارت ارسال شد", len(bot.client.sent) == 1, bot.client.sent)
    check("کارت به‌صورتِ پیامِ مستقل رفت (ریپلای نیست)", not event.replies,
          event.replies)
    if bot.client.sent:
        card = bot.client.sent[0]
        check("متنِ کارت همان قالبِ اسکرین‌شات است",
              card.text == texts.CONFIRM_ENABLE_TEMPLATE)
        check("نقل‌قولِ عنوان همراهِ کارت رفته است",
              bool(card.entities)
              and type(card.entities[0]).__name__ == "MessageEntityBlockquote")
    check("وضعیتِ انتظار ثبت شد", state.pending_user(CHAT) == str(OWNER),
          state.pending_user(CHAT))
    check("گروه هنوز خاموش است (تأیید لازم است)", not storage.is_enabled(CHAT))

    # «تایید» توسط همان ادمین — باید فعال شود و به گزارش نرود
    confirm_event = FakeEvent()
    handled = run(detector.handle_confirm(bot, confirm_event, CHAT, OWNER,
                                          sender, accept=True))
    check("«تایید» مصرف شد", handled is True)
    check("قابلیت فعال شد", storage.is_enabled(CHAT))
    check("پیامِ «فعال شد» با قالبِ عکس رفت",
          any(msg.text == texts.ENABLED_MESSAGE for msg in bot.client.sent),
          [m.text for m in bot.client.sent])
    check("وضعیتِ انتظار پاک شد", state.pending_user(CHAT) is None)

    # «تایید» بدونِ کارتِ در جریان ⇒ هیچ کاری نمی‌کند (مسیرِ گزارش آزاد است)
    handled = run(detector.handle_confirm(bot, FakeEvent(), CHAT, OWNER,
                                          sender, accept=True))
    check("بدونِ کارت، «تایید» مصرف نمی‌شود", handled is False)

    # خاموش‌کردن مستقیم است
    off_event = FakeEvent()
    handled = run(detector.handle_toggle(bot, off_event, CHAT, OWNER, sender,
                                         "off"))
    check("«تبلیغ گر خاموش» مصرف شد", handled is True)
    check("قابلیت خاموش شد", not storage.is_enabled(CHAT))
    check("پیامِ «خاموش شد» با قالبِ عکس رفت",
          any(msg.text == texts.DISABLED_MESSAGE for msg in bot.client.sent),
          [m.text for m in bot.client.sent])
    detector._is_admin = original_is_admin


# --------------------------------------------------------------------- ۵
def test_any_admin_can_confirm():
    print("\n=== ۵. هر ادمینِ گروه می‌تواند تأیید کند ===")
    original_is_admin = detector._is_admin
    detector._is_admin = lambda chat_id, user_id, sender: True
    state.clear_confirm(CHAT)
    storage.disable(CHAT)
    bot = FakeBot()
    owner = SimpleNamespace(id=int(OWNER), first_name="osine", username="osine")
    other_admin = SimpleNamespace(id=int(ADMIN2), first_name="admin2",
                                  username="admin2")
    run(detector.handle_toggle(bot, FakeEvent(), CHAT, OWNER, owner, "on"))
    handled = run(detector.handle_confirm(bot, FakeEvent(), CHAT, ADMIN2,
                                          other_admin, accept=True))
    check("ادمینِ دوم «تایید» را مصرف کرد", handled is True)
    check("قابلیت فعال شد", storage.is_enabled(CHAT))
    detector._is_admin = original_is_admin

    # کاربرِ بی‌مجوز: کلمه مصرف می‌شود ولی کارت باز می‌ماند
    original_denied = detector._denied

    async def _fake_denied(bot_, event_):
        event_.replies.append("دسترسی ندارید")
        return True

    detector._denied = _fake_denied
    state.clear_confirm(CHAT)
    storage.disable(CHAT)
    bot2 = FakeBot()
    detector._is_admin = lambda chat_id, user_id, sender: True
    run(detector.handle_toggle(bot2, FakeEvent(), CHAT, OWNER, owner, "on"))
    # حالا کاربرِ بی‌مجوز «تایید» می‌زند
    detector._is_admin = lambda chat_id, user_id, sender: False
    handled = run(detector.handle_confirm(bot2, FakeEvent(), CHAT, RANDOM,
                                          SimpleNamespace(id=int(RANDOM)),
                                          accept=True))
    check("کاربرِ بی‌مجوز «تایید» را مصرف می‌کند (به گزارش نمی‌رود)",
          handled is True)
    check("کاربرِ بی‌مجوز پیامِ روشن گرفت", True)
    check("قابلیت هنوز خاموش است", not storage.is_enabled(CHAT))
    check("کارت هنوز منتظرِ تصمیم است",
          state.pending_user(CHAT) == str(OWNER))
    detector._denied = original_denied
    detector._is_admin = original_is_admin


# --------------------------------------------------------------------- ۶
def test_pending_survives_restart():
    print("\n=== ۶. «تایید» بعد از ری‌استارت هم کار می‌کند ===")
    state.clear_confirm(CHAT)
    state.request_confirm(CHAT, OWNER, card_message_id=4321)
    state._PENDING = None          # شبیه‌سازیِ ری‌استارت (کشِ حافظه خالی)
    state._PENDING_MTIME = None
    check("درخواست از دیسک باز می‌شود", state.pending_user(CHAT) == str(OWNER),
          state.pending_user(CHAT))
    check("شناسهٔ کارت هم حفظ شده", state.is_pending_card(CHAT, 4321))
    original_is_admin = detector._is_admin
    detector._is_admin = lambda chat_id, user_id, sender: True
    bot = FakeBot()
    owner = SimpleNamespace(id=int(OWNER), first_name="osine", username="osine")
    # راهِ دوم: ریپلای روی کارت
    handled = run(detector.handle_confirm(
        bot, FakeEvent(reply_to_message_id=4321), CHAT, OWNER, owner,
        accept=True))
    check("«تایید» با ریپلای روی کارت پذیرفته شد", handled is True)
    check("قابلیت فعال شد", storage.is_enabled(CHAT))
    detector._is_admin = original_is_admin
    state.clear_confirm(CHAT)
    storage.disable(CHAT)


# --------------------------------------------------------------------- ۷
def test_report_does_not_steal_confirm():
    print("\n=== ۷. مسیرِ گزارش «تایید» را نمی‌قاپد ===")
    try:
        from modules.manage_features import report_commands
    except Exception as error:  # noqa: BLE001
        check("ایمپورتِ مسیرِ گزارش", False, repr(error))
        return

    state.clear_confirm(CHAT)
    check("بدونِ کارت: گزارش کلمه را می‌گیرد",
          report_commands.ad_spam_pending(CHAT) is False)
    state.request_confirm(CHAT, OWNER)
    check("با کارت: گزارش کنار می‌رود",
          report_commands.ad_spam_pending(CHAT) is True)
    check("is_command برای «تایید» منفی است",
          report_commands.is_command("تایید", CHAT, OWNER) is False)
    check("is_command برای «لغو» منفی است",
          report_commands.is_command("لغو", CHAT, OWNER) is False)
    state.clear_confirm(CHAT)
    check("بعد از پایانِ کارت، گزارش دست‌نخورده است",
          report_commands.is_command("تایید", CHAT, OWNER) is False)


def main():
    print("=" * 62)
    print("تستِ جریانِ قابلیت «تبلیغ گر»")
    print("=" * 62)
    test_template_exact()
    test_word_variants()
    test_exports()
    test_toggle_and_confirm_flow()
    test_any_admin_can_confirm()
    test_pending_survives_restart()
    test_report_does_not_steal_confirm()
    print("\n" + "=" * 62)
    print("نتیجه: %d پاس / %d شکست" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
