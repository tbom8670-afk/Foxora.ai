# -*- coding: utf-8 -*-
"""⚖️ تست «حکم بدون تبعیض» — پیام محکوم حذف می‌شود، هر نقش و سطحی که باشد.

قرارداد کارفرما: در ۶۰ ثانیهٔ اجرای حکم، پیام متهمِ محکوم باید پاک شود؛
حتی اگر مالک ربات، مالک گروه، ادمین ثبت‌شده، ادمین واقعی گروه یا VIP باشد.

این فایل می‌سنجد:

1. تشخیص پیام، در **اولین نقطهٔ ورود پیام گروه** انجام می‌شود (پیش از هر
   گیت/بای‌پس ادمین و VIP) — نه بعد از آن.
2. هیچ‌جای ماژول مجازات، نقش/سطح کاربر را نمی‌پرسد؛ پرچم‌های ادمینی و VIP
   هیچ اثری در حذف ندارند.
3. حذف با اولویت ۰ (حکم دادگاه) به صف می‌رود تا با «مکث حذف خودکار» رد
   نشود، و در صورت شکست یک تلاش مستقیم دوم انجام می‌شود.
4. هر پیام فقط یک بار به صف می‌رود (بدون حذف تکراری).
5. فقط پیام همان کاربر محکوم حذف می‌شود؛ بقیهٔ اعضا دست‌نخورده‌اند.
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from modules.court_game import punishment  # noqa: E402

CHAT = -100777555
PUNISHED = 4242
OTHER = 5151

PASSED = 0
FAILED = 0


def check(label, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class Msg:
    def __init__(self, text, message_id):
        self.message = text
        self.id = message_id
        self.file = None
        self.caption = None
        self.sender_id = None
        self.from_id = None
        self.edit_date = None
        self.reply_to = None
        self.entities = None


class Ev:
    def __init__(self, text, user_id, message_id=1, chat_id=CHAT):
        self.message = Msg(text, message_id)
        self.chat_id = chat_id
        self.is_private = False
        self.sender_id = user_id
        self.sender = types.SimpleNamespace(
            id=user_id, first_name=f"u{user_id}", last_name=None, username=None)
        self.replies = []

    async def get_chat(self):
        return types.SimpleNamespace(id=self.chat_id)

    async def get_sender(self):
        return self.sender

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def respond(self, text, **kwargs):
        self.replies.append(text)


class Logger:
    def __init__(self):
        self.info = []
        self.errors = []

    def log_info(self, message):
        self.info.append(message)

    def log_error(self, message):
        self.errors.append(message)


class Queue:
    """صف حذف ساختگی با همان قرارداد ``enqueue`` ربات."""

    def __init__(self, succeed=True):
        self.calls = []
        self.succeed = succeed

    def enqueue(self, chat_id, message_ids, *, priority=1, rpc_peer=None):
        ids = list(message_ids)
        self.calls.append({"chat_id": chat_id, "ids": ids,
                           "priority": priority})
        future = asyncio.get_running_loop().create_future()
        future.set_result((len(ids), []) if self.succeed else (0, ids))
        return future

    @property
    def ids(self):
        return [message_id for call in self.calls for message_id in call["ids"]]


class Client:
    def __init__(self):
        self.deleted = []

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, list(ids)))


def build_bot(queue=None):
    bot = types.SimpleNamespace()
    bot.logger = Logger()
    bot.client = Client()
    bot.message_delete_queue = queue or Queue()
    # پرچم‌های نقش/سطح: هیچ‌کدام نباید در حذف اثر داشته باشند.
    bot.is_admin = lambda *args, **kwargs: True
    bot.is_group_admin = lambda *args, **kwargs: True
    bot.is_global_owner = lambda *args, **kwargs: True
    bot.is_spam_locked = lambda *args, **kwargs: True
    return bot


def test_privileged_user_message_is_deleted():
    print("\n### ۱ پیام محکوم حذف می‌شود، هر نقشی که داشته باشد")

    async def scenario():
        punishment.reset_all()
        queue = Queue()
        bot = build_bot(queue)
        punishment.start(CHAT, PUNISHED, "COURT-P", seconds=60)
        observed = punishment.observe_event(
            bot, Ev("سلام، من مالک گروه هستم", PUNISHED, message_id=900))
        check("پیام کاربر محکوم برای حذف ثبت شد", observed is True)
        await asyncio.sleep(0.05)
        check("پیام به صف حذف رفت", queue.ids == [900], f"-> {queue.calls}")
        check("حذف با اولویت ۰ (حکم دادگاه) انجام شد",
              queue.calls and queue.calls[0]["priority"] == 0,
              f"-> {queue.calls}")
        stats = punishment.finish(CHAT, PUNISHED)
        check("آمار حذف موفق ثبت شد",
              stats and stats["deleted"] == 1 and stats["failed"] == 0,
              f"-> {stats}")
        punishment.reset_all()

    asyncio.run(scenario())


def test_no_role_is_consulted_in_the_code():
    print("\n### ۲ هیچ بررسی نقشی در ماژول مجازات نیست")
    source = (ROOT / "modules" / "court_game" / "punishment.py"
              ).read_text(encoding="utf-8")
    for needle in ("is_admin", "is_global_owner", "vip", "is_vip",
                   "owner_id", "admins"):
        check(f"«{needle}» در ماژول مجازات وجود ندارد",
              needle not in source, f"-> {needle}")


def test_hook_runs_before_any_admin_gate():
    print("\n### ۳ قلاب پیش از هر گیت ادمین/VIP اجرا می‌شود")
    core = (ROOT / "core" / "bot_working_split_ok.py").read_text(encoding="utf-8")
    check("قلاب مجازات در ورودی پیام‌های گروه هست",
          "_court_punish.observe_event" in core)
    hook_at = core.index("_court_punish.observe_event")
    gate_at = core.index("priority, kind = classify_priority(raw_text, event)")
    check("قلاب قبل از تصمیم‌گیری خط پیام اجرا می‌شود", hook_at < gate_at,
          f"-> hook={hook_at} gate={gate_at}")
    handler = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    check("قلاب دوم در هندلر پیام هم هست",
          "punishment as _court_punishment" in handler
          or "observe_event" in handler)


def test_admin_and_vip_bypass_does_not_save_the_message():
    print("\n### ۴ مسیر واقعی هندلر: ادمین/VIP هم بخشیده نمی‌شود")

    async def scenario():
        import handlers.message_handler as message_handler
        from test_court_join_all_users import build_handler_bot

        punishment.reset_all()
        bot = build_handler_bot(set(), game_answer=True)
        queue = Queue()
        bot.message_delete_queue = queue
        bot.is_admin = lambda *args, **kwargs: True
        bot.is_group_admin = lambda *args, **kwargs: True
        bot.is_global_owner = lambda *args, **kwargs: True

        # VIP: اگر ماژول VIP در دسترس است، همه را VIP علامت می‌زنیم.
        vip_patched = False
        try:
            from modules import vip_storage
            original_vip = vip_storage.is_vip
            vip_storage.is_vip = lambda *args, **kwargs: True
            vip_patched = True
        except Exception:
            original_vip = None

        try:
            punishment.start(CHAT, PUNISHED, "COURT-P", seconds=60)
            event = Ev("من ادمینم، پیامم را پاک نکن", PUNISHED, message_id=777)
            await message_handler.handle_new_message(bot, event)
            await asyncio.sleep(0.05)
            check("پیام ادمین/VIP برای حذف ثبت شد",
                  777 in queue.ids, f"-> {queue.calls}")

            # فقط همان کاربر؛ بقیهٔ اعضا دست‌نخورده
            other = Ev("سلام عادی", OTHER, message_id=778)
            await message_handler.handle_new_message(bot, other)
            await asyncio.sleep(0.05)
            check("پیام کاربر دیگر حذف نشد", 778 not in queue.ids,
                  f"-> {queue.calls}")
        finally:
            if vip_patched:
                vip_storage.is_vip = original_vip
            punishment.reset_all()

    asyncio.run(scenario())


def test_same_message_is_not_deleted_twice():
    print("\n### ۵ هر پیام فقط یک بار به صف می‌رود")

    async def scenario():
        punishment.reset_all()
        queue = Queue()
        bot = build_bot(queue)
        punishment.start(CHAT, PUNISHED, "COURT-P", seconds=60)
        event = Ev("یک پیام", PUNISHED, message_id=901)
        first = punishment.observe_event(bot, event)
        second = punishment.observe_event(bot, event)
        await asyncio.sleep(0.05)
        check("اولین مشاهده ثبت شد", first is True)
        check("مشاهدهٔ تکراری نادیده گرفته شد", second is False)
        check("فقط یک بار در صف رفت", queue.ids == [901], f"-> {queue.calls}")
        punishment.reset_all()

    asyncio.run(scenario())


def test_direct_retry_when_queue_fails():
    print("\n### ۶ اگر صف حذف نکرد، تلاش مستقیم دوم انجام می‌شود")

    async def scenario():
        punishment.reset_all()
        queue = Queue(succeed=False)
        bot = build_bot(queue)
        punishment.start(CHAT, PUNISHED, "COURT-P", seconds=60)
        punishment.observe_event(bot, Ev("پیام", PUNISHED, message_id=902))
        await asyncio.sleep(0.1)
        check("تلاش مستقیم دوم انجام شد",
              (CHAT, [902]) in [(chat, ids) for chat, ids in bot.client.deleted],
              f"-> {bot.client.deleted}")
        stats = punishment.finish(CHAT, PUNISHED)
        check("حذف مستقیم به‌عنوان موفقیت ثبت شد",
              stats and stats["deleted"] >= 1, f"-> {stats}")
        punishment.reset_all()

    asyncio.run(scenario())


def test_window_expiry_stops_deletion():
    print("\n### ۷ بعد از پایان پنجره هیچ حذفی انجام نمی‌شود")

    async def scenario():
        punishment.reset_all()
        queue = Queue()
        bot = build_bot(queue)
        punishment.start(CHAT, PUNISHED, "COURT-P", seconds=0.1)
        await asyncio.sleep(0.15)
        result = punishment.observe_event(bot, Ev("پیام", PUNISHED,
                                                  message_id=903))
        check("پیام بعد از پایان پنجره برای حذف ثبت نشد", result is False)
        check("صف دست‌نخورده ماند", queue.calls == [], f"-> {queue.calls}")
        punishment.reset_all()

    asyncio.run(scenario())


def main():
    print("=" * 58)
    print("⚖️ تست حکم بدون تبعیض (ادمین/مالک/VIP هم حذف می‌شوند)")
    print("=" * 58)
    test_privileged_user_message_is_deleted()
    test_no_role_is_consulted_in_the_code()
    test_hook_runs_before_any_admin_gate()
    test_admin_and_vip_bypass_does_not_save_the_message()
    test_same_message_is_not_deleted_twice()
    test_direct_retry_when_queue_fails()
    test_window_expiry_stops_deletion()
    print("\n" + "=" * 58)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 58)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
