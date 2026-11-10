# -*- coding: utf-8 -*-
"""🎯 تستِ رگرسیون «مرکز فرماندهی ↔ بقا» و پیامِ «ظرفیت پر است».

سه ایراد گزارش‌شدهٔ کارفرما، هر کدام با بازتولیدِ همان مسیر واقعی:

۱. **«ظرفیت شرکت در این بازی پر شده»** — مسابقهٔ کهنه‌ای که مهلت ثبت‌نامش
   گذشته (یا ربات ری‌استارت شده) و هیچ فرمانده‌ای نداشت، در حافظه می‌ماند و
   هر کسی — حتی خودِ فرمانده — دوباره دستور را می‌زد، پیام
   «ظرفیت این مسابقه پر است ⛔» می‌گرفت. حالا: مسابقهٔ بدون بازیکنِ منقضی
   کنار می‌رود، بازی تازه ساخته می‌شود، و «پر است» فقط با دو فرماندهٔ واقعی.

۲. **«تداخل با بقا»** — «شرکت» واژهٔ مشترکِ چند بازی است. اگر بقا و
   مرکز فرماندهی هم‌زمان در یک گروه ثبت‌نام بگیرند، فقط بازیِ صاحبِ «شرکت»
   آن را می‌بلعد؛ دیگری نه «قبلاً ثبت‌نام» غلط می‌فرستد نه بازیکن می‌دزدد.

۳. **بقیهٔ بازی‌ها** — برای بازی‌های دیگر (زامبی، خون‌آشام، دادگاه، نبرد،
   معما، مین‌یاب، حدس جمله، کارگاه، بهترین جواب) رفتار دقیقاً مثل قبل است:
   تک‌بازی در گروه هیچ پیام اضافه‌ای نمی‌گیرد و «شرکت» بلافاصله جواب می‌دهد.

اجرا:  PYTHONPATH=. python3 tests/test_game_join_isolation.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="join-isolation-")
os.environ["FOX_COMMAND_DATA_DIR"] = _TMP
os.environ.setdefault("BOT_PRIVATE_DATA_DIR", _TMP)
os.environ.setdefault("BOT_INSTANCE", "test")

PASSED = 0
FAILED = 0
FAILURES = []


def check(title, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {title}")
    else:
        FAILED += 1
        FAILURES.append(f"{title} :: {detail}")
        print(f"  ❌ {title}  {detail}")


from modules import entertainment_control, game_ownership  # noqa: E402
from modules.court_game import game as court_game  # noqa: E402
from modules.fox_command import engine, storage  # noqa: E402
from modules.fox_command import game as fgame  # noqa: E402
from modules.fox_command import texts as ftexts  # noqa: E402
from modules.fox_games import (  # noqa: E402
    battle,
    best_answer,
    karagah,
    laugh_or_lose,
    maemma,
    minesweeper,
    sentence_guess,
    survival,
    vampire,
    zombie,
)
from handlers import fox_games_router as gr  # noqa: E402

storage.set_base_dir(_TMP)


# ---------------------------------------------------------------------------
# ابزار تست
# ---------------------------------------------------------------------------
class Event:
    is_private = False

    def __init__(self, chat_id, user, text=""):
        self.chat_id = chat_id
        self.user = user
        self.sender = user
        self.sender_id = user.id
        self._text = text
        self.raw_text = text
        self.replies = []
        self.message = SimpleNamespace(id=1, text=text)

    async def reply(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(id=900 + len(self.replies))

    async def respond(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(id=900 + len(self.replies))

    async def get_sender(self):
        return self.user

    async def get_chat(self):
        return SimpleNamespace(id=self.chat_id)

    def said(self, needle):
        return any(needle in row for row in self.replies)

    def text(self):
        return "\n".join(self.replies)


class Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(id=1000 + len(self.sent))


class Bot:
    def __init__(self):
        self.client = Client()
        self.logger = SimpleNamespace(log_info=lambda *a, **k: None,
                                      log_error=lambda *a, **k: None)
        self.spam_lock = set()
        self.punished_users = set()
        self.is_spam_locked = lambda key: False
        self.clear_spam_lock = lambda key: None


class Voice:
    def __init__(self, ident, name, chat_id):
        self.id = ident
        self.first_name = name
        self.username = name
        self.chat_id = chat_id

    def event(self, text):
        return Event(self.chat_id, self, text)


def run(coro):
    return asyncio.run(coro)


async def say_together(*calls):
    """چند «شرکت» هم‌زمان — مثل گروه واقعی که چند نفر پشت سر هم می‌نویسند.

    هر بازی تایمر ثبت‌نامش را با ``asyncio.sleep`` می‌شمارد؛ اگر همهٔ پیام‌ها
    داخل یک حلقهٔ واحد (همان حلقهٔ ربات) پردازش شوند، تایمرها در همین بین
    پیش می‌روند و مثل دنیای واقعی مهلت ثبت‌نام تمام نمی‌شود.
    """
    return await asyncio.gather(*[say(*call) for call in calls])


async def settle():
    """به تایمرهای در جریان فرصت بسته‌شدن بده تا بین تست‌ها نشت نکنند."""
    await asyncio.sleep(0)


def reset_games():
    fgame.reset_all()
    game_ownership.reset_all()
    for module in (survival, vampire, karagah, zombie, battle, best_answer,
                   maemma, sentence_guess, minesweeper, laugh_or_lose):
        try:
            module.reset_all()
        except Exception:
            pass
    court_game.reset_all()
    for path in Path(_TMP).glob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)
    try:
        storage.ensure_dirs()
    except Exception:
        pass


async def say(bot, user, text, chat_id=None):
    """مسیر واقعی: روتر بازی‌ها (همان چیزی که هندلر اصلی صدا می‌زند)."""
    event = Event(chat_id or user.chat_id, user, text)
    consumed = await gr.handle(bot, event, event.chat_id, user.id, user, text,
                               bot.logger)
    return consumed, event


CHAT = -1008001


# ---------------------------------------------------------------------------
# ۱) ریشهٔ «ظرفیت پر است»: مسابقهٔ کهنه
# ---------------------------------------------------------------------------
async def scenario_01_stale_match_does_not_block():
    print("\n### ۱) مسابقهٔ کهنه (مهلت گذشته، صفر فرمانده) مسدود نمی‌کند")
    reset_games()
    bot = Bot()
    a = Voice(7001, "ali", CHAT)
    b = Voice(7002, "reza", CHAT)

    _consumed, first = await say(bot, a, "مرکز فرماندهی")
    check("دستور شروع، بازی را ساخت", fgame.match_of(CHAT) is not None,
          first.text())
    check("پیام شروع برای اولین بار «ظرفیت پر است» نیست",
          not first.said("پر است"), first.text())

    # همان اتفاقی که بعد از ری‌استارت/گذشت مهلت رخ می‌دهد
    match = fgame.match_of(CHAT)
    match["join_deadline"] = time.time() - 60
    check("مسابقهٔ بدون بازیکنِ منقضی، «فعال» شمرده نمی‌شود",
          fgame.is_active(CHAT) is False, fgame.phase(CHAT))

    owner_again, second = await say(bot, a, "مرکز فرماندهی")
    check("دستور دوباره مصرف شد", owner_again is True, owner_again)
    check("پیام «ظرفیت این مسابقه پر است» حذف شد",
          not second.said("ظرفیت این مسابقه پر است"), second.text())
    check("بازی تازه ساخته شد", "مرکز فرماندهی روباه" in second.text(),
          second.text())
    check("گروه به سقف «دو بازی هم‌زمان» گیر نمی‌کند",
          gr.active_game_count(CHAT) == 1, gr.active_game_count(CHAT))

    await say_together((bot, a, "شرکت"), (bot, b, "شرکت"))
    check("دو فرمانده ثبت‌نام شدند",
          len(fgame.match_of(CHAT)["players"]) == 2,
          fgame.match_of(CHAT)["players"])

    _consumed, full = await say(bot, Voice(7003, "sara", CHAT), "مرکز فرماندهی")
    check("«پر است» فقط با دو فرماندهٔ واقعی می‌آید",
          full.said("ظرفیت این مسابقه پر است"), full.text())


async def scenario_02_owner_never_gets_full_message():
    print("\n### ۲) خودِ فرمانده پیام «ظرفیت پر است» نمی‌گیرد")
    reset_games()
    bot = Bot()
    a = Voice(7101, "ali", CHAT)
    b = Voice(7102, "reza", CHAT)
    await say(bot, a, "مرکز فرماندهی")
    await say(bot, a, "شرکت")
    _consumed, one = await say(bot, a, "مرکز فرماندهی")
    check("پس از یک ثبت‌نام: پیام «این مسابقه باز است»", one.said("این مسابقه باز است"),
          one.text())
    check("پیام «پر است» نیامد", not one.said("ظرفیت این مسابقه پر است"), one.text())

    await say_together((bot, b, "شرکت"),)
    _consumed, two = await say(bot, a, "مرکز فرماندهی")
    check("فرماندهٔ اول بعد از پرشدن: «داخل مسابقه هستید»",
          two.said("داخل مسابقه هستید"), two.text())
    check("فرماندهٔ اول «ظرفیت پر است» نگرفت",
          not two.said("ظرفیت این مسابقه پر است"), two.text())

    _consumed, third = await say(bot, Voice(7103, "sara", CHAT), "مرکز فرماندهی")
    check("نفر سوم همان پیام «پر است» را می‌گیرد",
          third.said("ظرفیت این مسابقه پر است"), third.text())


# ---------------------------------------------------------------------------
# ۳) تداخل «شرکت» بین بازی‌ها
# ---------------------------------------------------------------------------
async def scenario_03_survival_and_fox_command_do_not_fight():
    print("\n### ۳) «شرکت» با بقا و مرکز فرماندهی هم‌زمان (سناریوی گزارش‌شده)")
    reset_games()
    bot = Bot()
    chat = -1008002
    a = Voice(7201, "ali", chat)
    b = Voice(7202, "reza", chat)

    await say(bot, a, "بقا")
    check("بقا در حال ثبت‌نام است",
          survival.is_active(chat) and survival.phase(chat) == "joining")
    _consumed, start = await say(bot, a, "مرکز فرماندهی")
    check("مرکز فرماندهی هم ساخته شد", fgame.match_of(chat) is not None,
          start.text())
    check("راهنمای «چند بازی هم‌زمان» یک بار آمد",
          start.said("چند بازی هم‌زمان"), start.text())
    check("راهنما هر دو بازی را نام می‌برد",
          "بقا" in start.text() and "مرکز فرماندهی" in start.text(),
          start.text())
    check("مالک «شرکت» تازه‌ترین بازی است",
          game_ownership.join_owner(chat, a.id) == "fox_command",
          game_ownership.evaluate(chat, a.id))

    _consumed, join1 = await say(bot, a, "شرکت")
    check("بقا «قبلاً ثبت‌نام» غلط نفرستاد",
          not join1.said("قبلاً ثبت‌نام"), join1.text())
    check("بقا بازیکن ندید", survival.player_count(chat) == 0,
          survival.player_count(chat))
    check("فرماندهی بازیکن اول را گرفت",
          len(fgame.match_of(chat)["players"]) == 1,
          fgame.match_of(chat)["players"])

    _consumed, join2 = await say(bot, b, "شرکت")
    check("فرماندهی دو فرمانده شد و منوی کشور آمد",
          len(fgame.match_of(chat)["players"]) == 2
          and join2.said("انتخاب کشور"),
          join2.text())
    check("بقا هنوز خالی و سالم است",
          survival.is_active(chat) and survival.player_count(chat) == 0,
          survival.player_count(chat))
    check("«شرکت» بعد از پرشدن فرماندهی به بقا برمی‌گردد",
          game_ownership.join_owner(chat, Voice(7203, "sara", chat).id) == "survival",
          game_ownership.join_owner(chat, 7203))
    _consumed, join3 = await say(bot, Voice(7203, "sara", chat), "شرکت")
    check("بازیکن سوم در بقا ثبت شد", survival.player_count(chat) == 1,
          join3.text())


async def scenario_04_survival_started_first_keeps_its_players():
    print("\n### ۴) بقا که زودتر باز شده، بازیکنش را از دست نمی‌دهد")
    reset_games()
    bot = Bot()
    chat = -1008003
    a = Voice(7301, "ali", chat)
    await say(bot, a, "مرکز فرماندهی")
    time.sleep(0.01)
    await say(bot, a, "بقا")     # تازه‌تر
    _consumed, join = await say(bot, a, "شرکت")
    check("«شرکت» به تازه‌ترین بازیِ باز (بقا) رفت",
          survival.player_count(chat) == 1, join.text())
    check("فرماندهی دست‌نخورده ماند",
          len(fgame.match_of(chat)["players"]) == 0,
          fgame.match_of(chat)["players"])
    _consumed, cmd = await say(bot, a, "مرکز فرماندهی")
    check("دستور فرماندهی به کاربر می‌گوید چه کند",
          cmd.said("این مسابقه باز است"), cmd.text())


async def scenario_05_member_wins_over_others():
    print("\n### ۵) عضویت قبلی بر تازگی اولویت دارد (تکرار «شرکت»)")
    reset_games()
    bot = Bot()
    chat = -1008004
    a = Voice(7401, "ali", chat)
    b = Voice(7402, "reza", chat)
    await say(bot, b, "بقا")
    await say(bot, b, "شرکت")          # b عضو بقا
    time.sleep(0.01)
    await say(bot, a, "مرکز فرماندهی")  # تازه‌تر
    statuses = {s.key: s for s in game_ownership.evaluate(chat, b.id)}
    check("بقا: عضو است", statuses["survival"].member)
    check("«شرکت» دوبارهٔ عضو به بقا می‌رسد",
          game_ownership.join_owner(chat, b.id) == "survival",
          game_ownership.join_owner(chat, b.id))
    _consumed, dup = await say(bot, b, "شرکت")
    check("پاسخ «قبلاً ثبت‌نام» از خودِ بقا آمد",
          dup.said("قبلاً ثبت‌نام"), dup.text())
    check("دوباره ثبت‌نام نشد", survival.player_count(chat) == 1,
          survival.player_count(chat))


# ---------------------------------------------------------------------------
# ۶) بقیهٔ بازی‌ها دست‌نخورده
# ---------------------------------------------------------------------------
async def scenario_06_other_games_unchanged():
    print("\n### ۶) بقیهٔ بازی‌ها: بدون تغییر، بدون پیام اضافه")
    reset_games()
    bot = Bot()

    # زامبی
    chat = -1008005
    a = Voice(7501, "z1", chat)
    b = Voice(7502, "z2", chat)
    _c, start = await say(bot, a, "زامبی")
    check("زامبی شروع شد", zombie.is_active(chat), start.text())
    check("زامبی پیام اضافهٔ هم‌زمانی نگرفت",
          not start.said("چند بازی هم‌زمان"), start.text())
    _c, joined = await say(bot, a, "شرکت")
    check("زامبی «شرکت» را گرفت", "ثبت شد" in joined.text(), joined.text())
    check("زامبی بازیکن دارد", zombie.phase(chat) == "joining")

    # نبرد (بازیکن اول با دستور شروع)
    reset_games()
    chat = -1008006
    a = Voice(7503, "b1", chat)
    b = Voice(7504, "b2", chat)
    await say(bot, a, "نبرد")
    _c, joined = await say(bot, b, "شرکت")
    check("نبرد بازیکن دوم را گرفت",
          battle.is_active(chat) and not any("قبلاً ثبت‌نام" in r
                                             for r in joined.replies),
          joined.text())

    # خون‌آشام
    reset_games()
    chat = -1008007
    a = Voice(7505, "v1", chat)
    _c, start = await say(bot, a, "خون آشام")
    check("خون‌آشام شروع شد", vampire.is_active(chat), start.text())
    _c, joined = await say(bot, a, "شرکت")
    check("خون‌آشام ثبت‌نام کرد", "ثبت شد" in joined.text(), joined.text())
    check("خون‌آشام پیام اضافه نگرفت",
          not any("چند بازی هم‌زمان" in r for r in start.replies), start.text())

    # کارگاه
    reset_games()
    chat = -1008008
    a = Voice(7506, "k1", chat)
    await say(bot, a, "کارگاه")
    _c, joined = await say(bot, a, "شرکت")
    check("کارگاه ثبت‌نام کرد", "ثبت شد" in joined.text(), joined.text())

    # دادگاه
    reset_games()
    chat = -1008009
    a = Voice(7507, "c1", chat)
    _c, start = await say(bot, a, "دادگاه")
    check("دادگاه شروع شد", court_game.is_active(chat), start.text())
    _c, joined = await say(bot, a, "شرکت")
    check("دادگاه ثبت‌نام کرد", "ثبت" in joined.text(), joined.text())


async def scenario_07_no_claim_on_innocent_messages():
    print("\n### ۷) پیام‌های عادی هیچ بازی را بلعیده نمی‌شوند")
    reset_games()
    bot = Bot()
    chat = -1008010
    a = Voice(7601, "n1", chat)
    await say(bot, a, "بقا")
    for text in ("سلام دوستان", "امروز چه خبر", "قیمت دلار"):
        consumed, event = await say(bot, a, text)
        check(f"پیام عادی «{text}» مصرف نشد", consumed is False, consumed)
        check(f"پیام عادی «{text}» پاسخی نگرفت", not event.replies, event.replies)


async def scenario_08_join_word_without_any_game():
    print("\n### ۸) «شرکت» بدون هیچ بازی باز — مثل قبل بی‌اثر")
    reset_games()
    bot = Bot()
    chat = -1008011
    a = Voice(7701, "sara", chat)
    consumed, event = await say(bot, a, "شرکت")
    check("«شرکت» توسط روتر بازی‌ها مصرف نشد", consumed is False, consumed)
    check("هیچ پیامی فرستاده نشد", not event.replies, event.replies)
    check("مالک «شرکت» وجود ندارد", game_ownership.join_owner(chat, a.id) is None,
          game_ownership.evaluate(chat, a.id))


async def scenario_09_entertainment_off_still_guards():
    print("\n### ۹) گارد «سرگرمی خاموش» دست‌نخورده")
    reset_games()
    bot = Bot()
    chat = -1008012
    a = Voice(7801, "sara", chat)
    await say(bot, a, "بقا")
    entertainment_control.disable(chat) if hasattr(
        entertainment_control, "disable") else None
    guard = await entertainment_control.guard(
        Event(chat, a, "مرکز فرماندهی"), chat, "مرکز فرماندهی",
    )
    check("گارد سرگرمی برای بازی‌های جدید هم فعال است", guard is True, guard)
    if hasattr(entertainment_control, "enable"):
        entertainment_control.enable(chat)


def scenario_10_texts_present():
    print("\n### ۱۰) متن‌های تازه موجود و بدون تکرارِ اشتباه")
    title_in, _ = ftexts.already_in_match()
    check("پیام «داخل مسابقه هستید» ساخته شد",
          "داخل مسابقه هستید" in title_in, title_in)
    full_in, _ = ftexts.join_full()
    check("پیام «پر است» دست‌نخورده", "پر است" in full_in, full_in)
    hint_in, _ = ftexts.join_word_hint()
    check("پیام راهنمای «شرکت» دست‌نخورده", "شرکت" in hint_in, hint_in)


async def scenario_11_recovery_marks_stale_closed():
    print("\n### ۱۱) بازیابی پس از ری‌استارت: مسابقهٔ خالیِ منقضی بسته می‌شود")
    reset_games()
    bot = Bot()
    chat = -1008013
    a = Voice(7901, "ali", chat)
    await say(bot, a, "مرکز فرماندهی")
    match = fgame.match_of(chat)
    match["join_deadline"] = time.time() - 120
    storage.save_match(match)
    fgame.reset_all()
    check("پس از ری‌ست حافظه، مسابقه از دیسک برنمی‌گردد به‌عنوان باز",
          fgame.match_of(chat) is None)
    restored = await fgame.recover(bot, bot.logger)
    check("recover اجرا شد", isinstance(restored, int), restored)
    check("مسابقهٔ کهنه پس از بازیابی «فعال» نیست",
          fgame.is_active(chat) is False, fgame.phase(chat))
    _c, start = await say(bot, a, "مرکز فرماندهی")
    check("کاربر می‌تواند بازی تازه بسازد",
          "مرکز فرماندهی روباه" in start.text(), start.text())


def main():
    # نام‌ها عمداً ``scenario_`` هستند تا pytest آن‌ها را به‌عنوان تست
    # async جمع نکند؛ این فایل یک اسکریپت خودبسنده است.
    tests = [(name, value) for name, value in sorted(globals().items())
             if name.startswith("scenario_") and callable(value)]
    print("=" * 62)
    print("🎯 تست جداسازی «شرکت» بین بازی‌ها (بقا ↔ مرکز فرماندهی)")
    print("=" * 62)
    async def runner():
        for _name, test in tests:
            result = test()
            if asyncio.iscoroutine(result):
                await result
            await settle()

    run(runner())
    print("\n" + "=" * 62)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\n".join(f"  - {row}" for row in FAILURES))
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
