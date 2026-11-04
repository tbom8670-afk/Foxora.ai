# -*- coding: utf-8 -*-
"""⚖️ تست «بازی برای همه» — «شرکت» اعضای عادی هرگز بلعیده نشود.

گزارش کارفرما: فقط مالک/ادمین می‌توانست بازی کند و پیام بقیهٔ اعضا هیچ
جوابی نمی‌گرفت.

علت واقعی (همان‌جا که بقیهٔ بازی‌ها ثبت شده‌اند): واژهٔ ثبت‌نام بازی‌ها
«شرکت» در فهرست دستورهای بازیِ `modules/group_dispatch.py` نبود. پس پیام
عضو عادی روی «خط عادی» می‌رفت، در صف شلوغ/لایهٔ هرزنگاری بررسی و حذف
می‌شد، و هرگز به بازی نمی‌رسید؛ مالک و ادمین‌ها از این لایه معاف‌اند و
تنها آن‌ها می‌توانستند بازی کنند.

این فایل همان قراردادها را می‌سنجد:

1. «شرکت» و «دادگاه» روی خط «دستور» (command lane) هستند.
2. پیام بازیکنِ بازیِ در جریان، در لایهٔ هرزنگاری حذف نمی‌شود.
3. پیام عضو غیربازیکن همچنان مسیر عادی moderation خودش را دارد.
4. مسیر واقعی هندلر پیام: پنج عضو عادی پشت سر هم ثبت‌نام می‌کنند.
"""
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from modules import light_spam_ingest  # noqa: E402
from modules import group_dispatch  # noqa: E402
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


def fresh_storage(join_seconds=60.0):
    tmp = Path(tempfile.mkdtemp())
    storage.set_base_dir(tmp / "court")
    storage.ensure_dirs()
    storage.write_default_settings(force=True)
    settings_path = storage.config_dir() / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.update({"join_seconds": join_seconds, "step_seconds": 60.0})
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    import handlers.court_game_router as court_router
    court_router._RECOVERED[0] = False
    game.reset_all()
    return tmp


def build_handler_bot(locked_keys, game_answer=True):
    from test_admin_commands import build_bot

    bot = build_bot()
    bot.message_delete_queue = types.SimpleNamespace(
        enqueue=lambda *args, **kwargs: None
    )
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
    bot._light_game_answer_active = lambda chat_id, user_id: game_answer
    return bot


def test_join_word_is_a_game_command_lane():
    print("\n### ۱ «شرکت» و «دادگاه» روی خط دستورهای بازی‌اند")
    for word in ("شرکت", "دادگاه", "دادگاه روباه"):
        priority, kind = group_dispatch.classify_priority(word)
        check(f"«{word}» خط دستور است",
              kind == group_dispatch.LANE_COMMAND
              and priority == group_dispatch.PRIORITY_COMMAND,
              f"-> {(priority, kind)}")
    check("«شرکت» در فهرست دستورهای بازی روتر است",
          "شرکت" in __import__("handlers.fox_games_router",
                               fromlist=["FOX_GAME_COMMANDS"]).FOX_GAME_COMMANDS)
    priority, kind = group_dispatch.classify_priority("امروز هوا خوبه")
    check("پیام عادی بدون بازی فعال، خط عادی می‌ماند",
          kind == group_dispatch.LANE_NORMAL, f"-> {(priority, kind)}")


def test_player_message_is_not_treated_as_spam():
    print("\n### ۲ پاسخ بازیکنِ بازیِ در جریان هرزنگاری محسوب نمی‌شود")

    async def scenario():
        fresh_storage()
        locked = {(CHAT, 22)}
        # کاربر ۲۲ در قفل هرزنگاری است، ولی وسط بازی است.
        bot = build_handler_bot(locked, game_answer=True)
        event = Ev("سلام", 22, "locked_user", message_id=5)
        result = light_spam_ingest.ingest_event(bot, event)
        check("پاسخ بازیکن با بازی فعال، drop نمی‌شود",
              result is not None and result.skip_heavy is False,
              f"-> {result}")
        check("دلیل ثبت‌شده game_answer است",
              getattr(result, "reason", "") == "game_answer",
              f"-> {getattr(result, 'reason', None)}")

        # همان کاربر، وقتی هیچ بازی‌ای در جریان نیست → مسیر عادی هرزنگاری
        bot2 = build_handler_bot(locked, game_answer=False)
        # قفل هرزنگاری در لایهٔ ingest با کلید نرمال‌شده بررسی می‌شود
        bot2.is_spam_locked = lambda key: True
        event2 = Ev("سلام", 22, "locked_user", message_id=6)
        result2 = light_spam_ingest.ingest_event(bot2, event2)
        check("بدون بازی، همان کاربر مثل قبل بررسی می‌شود",
              result2 is not None and result2.skip_heavy is True,
              f"-> {result2}")

    asyncio.run(scenario())


def test_only_players_take_the_game_lane():
    print("\n### ۳ فقط بازیکنان پرونده پیامشان روی خط بازی می‌رود")

    async def scenario():
        fresh_storage()
        session = game.start(CHAT)
        players = [31, 32, 33, 34, 35]
        for user_id in players:
            user = types.SimpleNamespace(
                id=user_id, first_name=f"u{user_id}", last_name=None,
                username=None,
            )
            game.join(CHAT, user_id, user)
        game._STORE.get(CHAT)["phase"] = game.PHASE_CRIME

        def fake_active(_chat_id):
            return True

        import handlers.fox_games_router as fox_router
        original = fox_router.any_active
        fox_router.any_active = fake_active
        try:
            player_event = Ev("دزدی", players[0], message_id=7)
            priority, kind = group_dispatch.classify_priority(
                "دزدی", player_event)
            check("پیام بازیکن → خط دستور",
                  kind == group_dispatch.LANE_COMMAND, f"-> {(priority, kind)}")
            outsider_event = Ev("سلام", 999, message_id=8)
            priority2, kind2 = group_dispatch.classify_priority(
                "سلام", outsider_event)
            check("پیام عضو غیربازیکن → خط عادی",
                  kind2 == group_dispatch.LANE_NORMAL, f"-> {(priority2, kind2)}")
        finally:
            fox_router.any_active = original
            game.reset_all(CHAT)

    asyncio.run(scenario())


def test_all_normal_users_can_join_through_handler():
    print("\n### ۴ مسیر واقعی هندلر پیام: پنج عضو عادی ثبت‌نام می‌کنند")

    async def scenario():
        import handlers.message_handler as message_handler
        fresh_storage()
        bot = build_handler_bot(set(), game_answer=True)

        starter = Ev("دادگاه", 11, "owner_starter", message_id=1)
        await message_handler.handle_new_message(bot, starter)
        check("دستور دادگاه پاسخ گرفت", starter.said("دادگاه روباه"),
              f"-> {starter.replies}")

        for index, user_id in enumerate((21, 22, 23, 24, 25), start=2):
            event = Ev("شرکت", user_id, f"user{user_id}", message_id=index)
            await message_handler.handle_new_message(bot, event)
            label = {1: "۱", 2: "۲", 3: "۳", 4: "۴", 5: "۵"}[index - 1]
            check(f"عضو {user_id} ثبت شد ({label} از ۵)",
                  event.said(f"({label} از ۵)"), f"-> {event.replies}")
        check("پنج نفر در پرونده‌اند", game.player_count(CHAT) == 5,
              f"-> {game.player_count(CHAT)}")
        check("هیچ خطایی در لاگ نیست", not bot.logger.errors,
              f"-> {bot.logger.errors[-3:]}")
        game.reset_all(CHAT)

    asyncio.run(scenario())


def test_handler_core_is_left_alone():
    print("\n### ۵ هستهٔ هندلر پیام دست‌نخورده است")
    source = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    check("هیچ قلاب زودهنگام دادگاه در هندلر نیست",
          "COURT EARLY ROUTE" not in source)
    check("شرط routing بازی‌ها همان شرط اصلی است",
          "clean_text in FOX_GAME_COMMANDS or fox_game_active(chat_id)" in source)
    dispatch = (ROOT / "modules" / "group_dispatch.py").read_text(encoding="utf-8")
    check("خط دستور بازی در جابجایی پیام‌ها ثبت شده",
          "def _is_active_game_answer(" in dispatch)
    ingest = (ROOT / "modules" / "light_spam_ingest.py").read_text(encoding="utf-8")
    check("بررسی «پاسخ بازی» پیش از قفل هرزنگاری است",
          ingest.index("game_probe")
          < ingest.index("if callable(lock_probe) and lock_probe(key):"))


def main():
    print("=" * 56)
    print("⚖️ تست «شرکت» برای همهٔ اعضا (خط پیام + مسیر واقعی)")
    print("=" * 56)
    test_join_word_is_a_game_command_lane()
    test_player_message_is_not_treated_as_spam()
    test_only_players_take_the_game_lane()
    test_all_normal_users_can_join_through_handler()
    test_handler_core_is_left_alone()
    print("\n" + "=" * 56)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 56)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
