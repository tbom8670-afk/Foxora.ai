# -*- coding: utf-8 -*-
"""⚖️ تست «بازی برای همه» — «شرکت» کاربر عادی در بدترین شرایط هم ثبت شود.

گزارش کارفرما: فقط خودش (مالک) می‌توانست «شرکت» بزند و بقیهٔ اعضا هیچ
پاسخی نمی‌گرفتند؛ چون پیام کاربر عادی پیش از رسیدن به بازی در لایهٔ
ضداسپم/فیلتر هندلر بلعیده می‌شد (مالک از ضداسپم معاف است، پس برای او
کار می‌کرد).

این فایل همان مسیر واقعی ``handlers.message_handler.handle_new_message``
را صدا می‌زند (نه روتر بازی را) و سه سناریو را می‌سنجد:

1. کاربر عادی که در قفل ضداسپم است → «شرکت» باید ثبت شود.
2. کاربر عادی که پیامش در شمارندهٔ هرزنگاری «تکراری» دیده می‌شود.
3. عضو غیربازیکن وسط دادگاه → نباید بازی را جلو ببرد.
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from modules.court_game import game, storage  # noqa: E402

CHAT = -100777123

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
    """رویداد ساختگی با همان قرارداد event سروش/کلاینت."""

    def __init__(self, text, user_id, username=None, chat_id=CHAT, message_id=1):
        self.message = Msg(text, message_id)
        self.chat_id = chat_id
        self.is_private = False
        self.replies = []
        self.sender_id = user_id
        self.sender = types.SimpleNamespace(
            id=user_id, first_name=f"u{user_id}", last_name=None,
            username=username,
        )

    async def get_chat(self):
        return types.SimpleNamespace(id=self.chat_id)

    async def get_sender(self):
        return self.sender

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def respond(self, text, **kwargs):
        self.replies.append(text)

    def said(self, needle):
        return any(needle in message for message in self.replies)


def build_handler_bot(locked_keys):
    """ربات ساختگیِ تست‌های پروژه + قفل ضداسپم واقعی‌شکل برای کاربران عادی."""
    from test_admin_commands import build_bot

    bot = build_bot()
    bot.message_delete_queue = types.SimpleNamespace(
        enqueue=lambda *args, **kwargs: None
    )
    # قفل ضداسپم: هر کاربری که در این مجموعه باشد، پیامش در حالت عادی
    # (بدون مسیر مستقل بازی) حذف و بلعیده می‌شود — دقیقاً مثل ربات واقعی.
    bot.is_spam_locked = lambda key: key in locked_keys
    bot.clear_spam_lock = lambda key: None
    bot.spam_lock = locked_keys
    bot.moderation_notification_guard = set()
    bot.reply_input_peer_cache = {}
    bot.count_message = lambda *args, **kwargs: None
    bot.punished_users = set()
    bot.handle_auto_spam = lambda *args, **kwargs: None
    bot.reward_game = lambda *args, **kwargs: None
    bot.bot_account_id = 999
    bot.detector.check_banned_words = lambda *args, **kwargs: (False, None)
    bot.detector.should_delete = lambda *args, **kwargs: False
    bot.detector.analyze = lambda *args, **kwargs: (False, None)
    bot.detector.has_public_username = lambda *args, **kwargs: False
    return bot


def fresh_storage():
    tmp = Path(tempfile.mkdtemp())
    storage.set_base_dir(tmp / "court")
    storage.ensure_dirs()
    storage.write_default_settings(force=True)
    settings_path = storage.config_dir() / "settings.json"
    settings = {}
    import json
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.update({"join_seconds": 60.0, "step_seconds": 60.0})
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    import handlers.court_game_router as court_router
    court_router._RECOVERED[0] = False
    game.reset_all()
    return tmp


def test_locked_user_can_join():
    print("\n### ۱ «شرکت» کاربر عادیِ قفل‌شده در ضداسپم")
    import handlers.message_handler as message_handler

    async def scenario():
        fresh_storage()
        locked = {(CHAT, 22)}
        bot = build_handler_bot(locked)

        starter = Ev("دادگاه", 11, "owner_starter", message_id=1)
        await message_handler.handle_new_message(bot, starter)
        check("دستور دادگاه پاسخ گرفت", starter.said("دادگاه روباه"),
              f"-> {starter.replies}")

        normal = Ev("شرکت", 21, "normal_user", message_id=2)
        await message_handler.handle_new_message(bot, normal)
        check("کاربر عادی اول ثبت شد (۱ از ۵)", normal.said("۱ از ۵"),
              f"-> {normal.replies}")

        # کاربر عادی که در قفل ضداسپم است — قلب گزارش کارفرما.
        locked_user = Ev("شرکت", 22, None, message_id=3)
        await message_handler.handle_new_message(bot, locked_user)
        check("کاربر قفل‌شده در ضداسپم هم ثبت شد (۲ از ۵)",
              locked_user.said("۲ از ۵"), f"-> {locked_user.replies}")
        check("شمارش در حافظه هم ۲ نفر است",
              game.player_count(CHAT) == 2, f"-> {game.player_count(CHAT)}")
        check("هیچ خطایی در لاگ ثبت نشد",
              not bot.logger.errors, f"-> {bot.logger.errors[-3:]}")

        third = Ev("شرکت", 23, "third_user", message_id=4)
        await message_handler.handle_new_message(bot, third)
        check("کاربر سوم عادی هم ثبت شد (۳ از ۵)", third.said("۳ از ۵"),
              f"-> {third.replies}")
        game.reset_all(CHAT)

    asyncio.run(scenario())


def test_join_word_not_stolen_from_other_games():
    print("\n### ۲ «شرکت» بدون پروندهٔ دادگاه، دست بازی‌های دیگر می‌ماند")
    import handlers.court_game_router as court_router

    async def scenario():
        fresh_storage()
        check("بدون پرونده، «شرکت» مسیر نمی‌شود",
              not court_router.should_route(CHAT, "شرکت", 21))
        check("دستور «دادگاه» همیشه مسیر می‌شود",
              court_router.should_route(CHAT, "دادگاه", 21))
        check("«دادگاه روباه» هم مسیر می‌شود",
              court_router.should_route(CHAT, "دادگاه روباه", 21))
        check("پیام عادی مسیر نمی‌شود",
              not court_router.should_route(CHAT, "سلام", 21))

        game.start(CHAT)
        check("در ثبت‌نام، «شرکت» مسیر می‌شود",
              court_router.should_route(CHAT, "شرکت", 21))
        check("در ثبت‌نام، پیام دیگر مسیر نمی‌شود",
              not court_router.should_route(CHAT, "سلام", 21))
        game.reset_all(CHAT)

    asyncio.run(scenario())


def test_only_players_reach_court():
    print("\n### ۳ فقط پنج بازیکن پرونده، پیامشان به دادگاه می‌رسد")
    import handlers.court_game_router as court_router

    async def scenario():
        fresh_storage()
        session = game.start(CHAT)
        players = [21, 22, 23, 24, 25]
        for user_id in players:
            user = types.SimpleNamespace(
                id=user_id, first_name=f"u{user_id}", last_name=None,
                username=None,
            )
            game.join(CHAT, user_id, user)
        # رفتن به مرحلهٔ جرم: همان state زندهٔ بازی (session_of کپی می‌دهد)
        game._STORE.get(CHAT)["phase"] = game.PHASE_CRIME
        check("مرحلهٔ پرونده جرم است", game.phase(CHAT) == game.PHASE_CRIME)
        check("بازیکن پرونده مسیر می‌شود",
              court_router.should_route(CHAT, "دزدی", players[0]))
        check("عضو غیربازیکن مسیر نمی‌شود",
              not court_router.should_route(CHAT, "سلام", 999))
        game.reset_all(CHAT)

    asyncio.run(scenario())


def test_router_is_wired_before_spam_pipeline():
    print("\n### ۴ مسیر زودهنگام در هندلر، پیش از ضداسپم وصل شده")
    source = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    early = source.find("COURT EARLY ROUTE")
    spam_lock = source.find("spam_lock_key = (chat_id, user_id)")
    burst = source.find("if burst_key in bot.spam_burst_users")
    check("بلوک مسیر زودهنگام وجود دارد", early != -1)
    check("پیش از قفل ضداسپم است", -1 < early < spam_lock,
          f"-> early={early} spam_lock={spam_lock}")
    check("پیش از کنترل هرزنگاری تکراری است", -1 < early < burst,
          f"-> early={early} burst={burst}")
    check("مسیر زودهنگام از روتر دادگاه استفاده می‌کند",
          ".should_route(chat_id, _court_text, user_id)" in source
          and ".route_early(" in source)
    check("گارد «سرگرمی خاموش» در مسیر زودهنگام رعایت می‌شود",
          "entertainment_control.guard(" in (
              (ROOT / "handlers" / "court_game_router.py")
              .read_text(encoding="utf-8")))
    check("هندلر همچنان فقط یک بار گارد عمومی دارد",
          source.count("entertainment_control.guard(") == 1)


def main():
    print("=" * 56)
    print("⚖️ تست «شرکت» برای همهٔ اعضا (مسیر واقعی هندلر پیام)")
    print("=" * 56)
    test_locked_user_can_join()
    test_join_word_not_stolen_from_other_games()
    test_only_players_reach_court()
    test_router_is_wired_before_spam_pipeline()
    print("\n" + "=" * 56)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 56)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
