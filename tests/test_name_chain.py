# -*- coding: utf-8 -*-
"""🧪 تستِ بازی «اسم‌چین».

اجرا:

    PYTHONPATH=. python3 tests/test_name_chain.py

هر چیزی که کارفرما مشخص کرده اینجا بررسی می‌شود: متنِ دقیقِ شروع، حرفِ
تصادفی، فقط‌ریپلای‌بودنِ جواب‌ها، اعتبارسنجیِ نامِ ایرانی/حرفِ درست/تکراری
نبودن، زنجیرهٔ حرفِ آخر، «بازنده شدی ⛄️»، «نمی‌دونم»ِ ربات و بردِ کاربر،
استقلالِ وضعیتِ هر چت و اتصالِ بازی به لیستِ بازی‌ها (با Bold).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import name_chain as NC  # noqa: E402["E402"]

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


def section(title):
    print("\n=== %s ===" % title)


# ---------------------------------------------------------------------------
def test_normalization():
    section("۱. نرمال‌سازیِ متن")
    check("ي و ك عربی → ی و ک فارسی",
          NC.normalize("علي اكبر") == "علی اکبر", NC.normalize("علي اكبر"))
    check("حذفِ اعراب", NC.normalize("حَسَن") == "حسن", NC.normalize("حَسَن"))
    check("حذفِ نیم‌فاصله", NC.normalize("نمی‌دونم") == "نمیدونم",
          NC.normalize("نمی‌دونم"))
    check("حذفِ فاصله‌های اضافه", NC.normalize("  لاله   خانم ") == "لاله خانم",
          repr(NC.normalize("  لاله   خانم ")))
    check("آ و ا هم‌حرف حساب می‌شوند", NC.first_letter("آرش") == "ا",
          NC.first_letter("آرش"))
    check("نشانه‌ها به فاصله تبدیل می‌شوند", NC.normalize("«لاله!»") == "لاله",
          NC.normalize("«لاله!»"))

    spellings = (
        "نمیدونم", "نمی‌دونم", "نمی دونم", "نمیدانم", "نمی دانم",
        "نمدونم", "نمیدونم!", "  نمی دونم  ",
    )
    for text in spellings:
        check("«نمی‌دونم» با املای %r شناخته شد" % text,
              NC.is_unknown_phrase(text))
    check("«نمی دونم» با فاصله", NC.is_unknown_phrase("نمی دونم"))
    check("«نمیدانم»", NC.is_unknown_phrase("نميدانم"))
    check("یک اسمِ عادی، «نمی‌دونم» نیست", not NC.is_unknown_phrase("لاله"))


def test_names_file():
    section("۲. فایلِ names.txt و بارگذاری")
    check("فایلِ names.txt وجود دارد", NC.NAMES_FILE.is_file(), NC.NAMES_FILE)
    check("چند صد نام بارگذاری شد (≥ ۴۰۰)", len(NC.NAMES) >= 400, len(NC.NAMES))
    check("همهٔ نام‌ها در یک set هستند", isinstance(NC.NAMES, frozenset))
    check("دیکشنری بر اساسِ حرفِ اول ساخته شد",
          isinstance(NC.NAMES_BY_LETTER, dict) and len(NC.NAMES_BY_LETTER) >= 30,
          len(NC.NAMES_BY_LETTER))
    check("حروفِ پرکاربرد پوشش دارند",
          all(NC.NAMES_BY_LETTER.get(letter) for letter in "ابپتمنسش"),
          [l for l in "ابپتمنسش" if not NC.NAMES_BY_LETTER.get(l)])

    sample = [NC.NAME_DISPLAY[key] for key in list(NC.NAMES)[:40]]
    unknown = [name for name in sample if not NC.is_real_name(name)]
    check("نام‌های استخر، نامِ واقعیِ ایرانی‌اند", not unknown, unknown)
    check("نامِ الکی رد می‌شود", not NC.is_real_name("هززززززز"))
    check("نامِ واقعیِ بیرونِ استخر هم قبول است (دیتابیسِ بزرگ)",
          NC.is_real_name("آزیتا") or NC.is_real_name("آتنا"))

    by_letter_ok = all(
        NC.normalize(NC.NAME_DISPLAY[key])[0] == letter
        for letter, keys in NC.NAMES_BY_LETTER.items() for key in keys
    )
    check("کلیدِ هر نام در دیکشنری با حرفِ اولش می‌خواند", by_letter_ok)


def test_start():
    section("۳. شروعِ بازی و متنِ دقیق")
    NC.reset()
    round_ = NC.start("chat-start")
    check("بازی شروع شد", round_ is not None)
    letter = round_["letter"]
    check("حرفِ شروع از الفبای فارسی است", letter in NC.ALPHABET, letter)

    expected = (
        "⛄️بازی اسم چین\n"
        "\n"
        "دقت کن دوست، هرجا که بلد نبودی اسمی بگی بگو «نمیدونم» تا بازنده بشی، "
        "یا برای تموم کردن بازی هم بگو «نمیدونم».\n"
        "\n"
        "برای جواب گرفتن باید هر پیام رو روی ربات ریپلای کنی.\n"
        "\n"
        "حالا برای شروع یک اسم با «%s» بگو" % letter
    )
    check("متنِ شروع عیناً مطابقِ متنِ کارفرماست", round_["text"] == expected,
          round_["text"])

    # حرفِ شروع نباید همیشه یکی باشد.
    letters = {NC.start("chat-rand")["letter"] for _ in range(40)}
    check("حرفِ شروع تصادفی است (چند حرفِ مختلف)", len(letters) >= 3, letters)
    check("حرفِ شروع همیشه بازی‌پذیر است",
          all(NC.NAMES_BY_LETTER.get(l) for l in letters), letters)
    check("پس از شروع، بازی فعال است", NC.is_active("chat-start"))


def test_only_replies():
    section("۴. فقط پیام‌های ریپلای‌شده پردازش می‌شوند")
    NC.reset()
    NC.start("chat-reply", letter="ل")
    NC.record_prompt("chat-reply", 500)

    check("پیامِ بدونِ ریپلای نادیده گرفته می‌شود",
          NC.answer("chat-reply", "لاله") is None)
    check("ریپلای روی پیامِ دیگر نادیده گرفته می‌شود",
          NC.answer("chat-reply", "لاله", reply_id=499) is None)
    check("هنوز هیچ اسمی مصرف نشده است",
          NC.state("chat-reply")["used"] == set())
    accepted = NC.answer("chat-reply", "لاله", reply_id=500)
    check("ریپلای روی پیامِ ربات پردازش شد",
          accepted is not None and accepted["kind"] == "accepted", accepted)


def test_validation():
    section("۵. اعتبارسنجی: حرف، تکراری، نامِ الکی")
    NC.reset()
    NC.start("chat-valid", letter="ل")
    NC.record_prompt("chat-valid", 700)

    wrong = NC.answer("chat-valid", "سارا", reply_id=700)
    check("حرفِ اشتباه رد شد", wrong["kind"] == "invalid" and wrong["reason"] == "wrong-letter")
    check("دلیلِ ردشدن، حرفِ درست را می‌گوید", "«ل»" in wrong["message"],
          wrong["message"])

    fake = NC.answer("chat-valid", "لززززززز", reply_id=700)
    check("اسمِ الکی رد شد", fake["kind"] == "invalid" and fake["reason"] == "not-a-name")
    check("دلیلِ ردشدن، اسمِ ناشناس را می‌گوید", "نمی‌شناسم" in fake["message"],
          fake["message"])

    many = NC.answer("chat-valid", "لاله یک دو سه", reply_id=700)
    check("جملهٔ چندکلمه‌ای به‌عنوان اسم رد شد",
          many["kind"] == "invalid" and many["reason"] == "not-a-word")

    ok = NC.answer("chat-valid", "لاله", reply_id=700)
    check("اسمِ درست قبول شد", ok["kind"] == "accepted", ok)

    NC.record_prompt("chat-valid", 701)
    duplicate = NC.answer("chat-valid", "لاله", reply_id=701)
    check("همان اسم دوباره رد شد",
          duplicate is not None and duplicate["kind"] == "invalid",
          duplicate)


def test_chain():
    section("۶. زنجیره: ربات با حرفِ آخر جواب می‌دهد")
    NC.reset()
    NC.start("chat-chain", letter="ل")
    NC.record_prompt("chat-chain", 900)

    result = NC.answer("chat-chain", "لاله", reply_id=900)
    check("ربات جواب داد", result["kind"] == "accepted")
    check("ربات با حرفِ آخرِ اسمِ کاربر جواب داد",
          NC.first_letter(result["bot_name"]) == "ه",
          (result["user_name"], result["bot_name"]))
    check("حرفِ بعدی برای کاربر = حرفِ آخرِ اسمِ ربات",
          result["next_letter"] == NC.last_letter(result["bot_name"]),
          (result["bot_name"], result["next_letter"]))
    check("متنِ ربات نام و حرفِ بعدی را دارد",
          result["bot_name"] in result["message"]
          and result["next_letter"] in result["message"], result["message"])

    # زنجیره ادامه پیدا می‌کند: کاربر با حرفِ بعدی اسم می‌گوید.
    next_letter = result["next_letter"]
    candidate = next(
        (NC.NAME_DISPLAY[key] for key in NC.NAMES_BY_LETTER.get(next_letter, ())
         if key not in NC.state("chat-chain")["used"]),
        None,
    )
    if candidate is None:
        check("برای حرفِ بعدی نامِ آزاد وجود دارد", False, next_letter)
        return
    NC.record_prompt("chat-chain", 901)
    second = NC.answer("chat-chain", candidate, reply_id=901)
    check("نوبتِ دوم هم قبول شد", second is not None and second["kind"] == "accepted",
          second)
    used = NC.state("chat-chain")["used"]
    check("هیچ اسمی دو بار استفاده نشده است", len(used) == len(set(used)))

    # چند نوبتِ پشتِ‌سرهم: تکرارِ اسم ممنوع (هم کاربر، هم ربات).
    NC.reset()
    NC.start("chat-many", letter="س")
    prompt = 1000
    NC.record_prompt("chat-many", prompt)
    seen = set()
    rounds = 0
    name = "سارا"
    while rounds < 6:
        outcome = NC.answer("chat-many", name, reply_id=prompt)
        if outcome is None or outcome["kind"] != "accepted":
            break
        key = NC.normalize(name)
        check("اسمِ %r تکراری نبود" % name, key not in seen, name)
        bot_key = NC.normalize(outcome["bot_name"])
        check("اسمِ ربات %r تکراری نبود" % outcome["bot_name"], bot_key not in seen,
              outcome["bot_name"])
        seen.update({key, bot_key})
        prompt += 1
        NC.record_prompt("chat-many", prompt)
        next_letter = outcome["next_letter"]
        name = next(
            (NC.NAME_DISPLAY[k] for k in NC.NAMES_BY_LETTER.get(next_letter, ())
             if k not in NC.state("chat-many")["used"]),
            None,
        )
        if name is None:
            break
        rounds += 1
    check("چند نوبت پشتِ سرهم بازی شد", rounds >= 3, rounds)


def test_player_loses():
    section("۷. «نمی‌دونم» = باختِ کاربر")
    for spelling in ("نمیدونم", "نمی‌دونم", "نمی دونم", "نمیدانم"):
        NC.reset()
        NC.start("chat-lose", letter="ل")
        NC.record_prompt("chat-lose", 1200)
        result = NC.answer("chat-lose", spelling, reply_id=1200)
        check("با املای %r کاربر باخت" % spelling,
              result is not None and result["kind"] == "user_lost", result)
        check("متنِ باخت عیناً «بازنده شدی ⛄️» است",
              result["message"] == "بازنده شدی ⛄️", result["message"])
        check("بازی بسته شد", not NC.is_active("chat-lose"))
        check("پاسخ بعدی پردازش نمی‌شود",
              NC.answer("chat-lose", "لاله", reply_id=1200) is None)


def test_bot_loses():
    section("۸. ربات اسمی ندارد ⇒ «نمی‌دونم» و بردِ کاربر")
    NC.reset()
    NC.start("chat-win", letter="س")
    NC.record_prompt("chat-win", 1300)
    # همهٔ نام‌های «ل» را از قبل مصرف می‌کنیم تا ربات گیر کند.
    entry = NC.state("chat-win")
    entry["used"].update(NC.NAMES_BY_LETTER["ل"])
    result = NC.answer("chat-win", "سهیل", reply_id=1300)
    check("ربات گیر کرد", result is not None and result["kind"] == "bot_lost", result)
    check("ربات «نمی‌دونم» گفت", "نمی‌دونم" in result["message"], result["message"])
    check("بردِ کاربر اعلام شد", "🏆" in result["message"], result["message"])
    check("بازی بسته شد", not NC.is_active("chat-win"))


def test_chat_isolation():
    section("۹. وضعیتِ هر چت جداست")
    NC.reset()
    NC.start("group-a", letter="ل")
    NC.record_prompt("group-a", 1400)
    NC.start("group-b", letter="م")
    NC.record_prompt("group-b", 2400)

    a = NC.answer("group-a", "لاله", reply_id=1400)
    check("بازیِ گروه A پیش رفت", a["kind"] == "accepted")
    b_state = NC.state("group-b")
    check("وضعیتِ گروه B دست‌نخورده ماند",
          b_state["letter"] == "م" and b_state["used"] == set(),
          b_state)
    check("اسم‌های گروه A در گروه B مصرف‌شده نیست",
          NC.normalize("لاله") not in b_state["used"])
    check("ریپلایِ گروه B روی شناسهٔ گروه A پذیرفته نمی‌شود",
          NC.answer("group-b", "مینا", reply_id=1400) is None)

    # هندلر پس از هر نوبت، شناسهٔ پیامِ تازهٔ ربات را ثبت می‌کند؛ همان کار را
    # اینجا هم می‌کنیم تا «ریپلایِ کهنه» واقعاً کهنه باشد.
    NC.record_prompt("group-a", 1401)
    check("بازیِ گروه A همچنان فعال است", NC.is_active("group-a"))
    check("ریپلایِ کهنه (پیامِ قبلیِ ربات) رد می‌شود",
          NC.answer("group-a", "هادی", reply_id=1400) is None)
    letter_a = NC.state("group-a")["letter"]
    fresh_name = next(
        (NC.NAME_DISPLAY[key] for key in NC.NAMES_BY_LETTER.get(letter_a, ())
         if key not in NC.state("group-a")["used"]),
        None,
    )
    check("ریپلای روی پیامِ تازهٔ ربات پذیرفته می‌شود",
          (NC.answer("group-a", fresh_name, reply_id=1401) or {}).get("kind")
          == "accepted", (letter_a, fresh_name))


def test_handler_wiring():
    section("۱۰. اتصال به هندلر و لیستِ بازی‌ها")
    source = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    check("دستورِ «اسم چین» در هندلر تعریف شده",
          'NAME_CHAIN_COMMANDS = frozenset({' in source)
    check("لیستِ بازی ردیفِ اسم چین را دارد",
          '"🫆اسم چین\\n"' in source, "missing list row")
    check("توضیحِ لیست همان متنِ کارفرماست",
          '"رقابت با ربات با آخر اسم یک اسم بگید\\n\\n"' in source)
    check("«اسم چین» در فهرستِ Boldِ لیستِ بازی هست",
          '"🫆اسم چین",' in source)
    check("اسم چین در بازی‌های چت‌محور (Busy) حساب می‌شود",
          source.count("or name_chain_active(chat_id)") >= 3,
          source.count("or name_chain_active(chat_id)"))

    from modules.entertainment_control import GAME_COMMANDS

    check("گاردِ سرگرمی «اسم چین» را می‌شناسد", "اسم چین" in GAME_COMMANDS)

    from modules.group_dispatch import _COMMAND_EXACT

    check("group_dispatch «اسم چین» را می‌شناسد", "اسم چین" in _COMMAND_EXACT)



# ---------------------------------------------------------------------------
# ۱۱) از مسیرِ واقعیِ هندلر
# ---------------------------------------------------------------------------
CHAT = -88001122


def _handler_e2e():
    section("۱۱. از مسیرِ واقعیِ هندلر")
    from types import SimpleNamespace

    import asyncio

    import handlers.message_handler as handler

    # شناسهٔ پیام‌های ربات باید یکتا باشد (مثلِ کلاینتِ واقعی).
    counter = {"next": 9000}

    def next_id():
        counter["next"] += 1
        return counter["next"]

    class Logger:
        def __init__(self):
            self.infos, self.errors = [], []

        def log_info(self, message):
            self.infos.append(message)

        def log_error(self, message):
            self.errors.append(message)

    class Config:
        def get(self, key, default=None):
            return default

    class Tracker:
        def reset_count(self, *a, **k):
            return None

        def get_count(self, *a, **k):
            return 0

    class ReplyTo:
        def __init__(self, msg_id, user_id):
            self.reply_to_msg_id = msg_id
            self.from_id = SimpleNamespace(user_id=user_id)

    class Msg:
        def __init__(self, text, mid, reply_to=None):
            self.message = text
            self.id = mid
            self.file = None
            self.caption = None
            self.reply_to = reply_to
            self.reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)

    class Event:
        def __init__(self, text, mid, reply_to=None, uid=770):
            self.message = Msg(text, mid, reply_to)
            self.chat_id = CHAT
            self.sender_id = uid
            self.sender = SimpleNamespace(
                id=uid, first_name="نیکا", last_name=None, username="nika_1"
            )
            self.chat = SimpleNamespace(id=CHAT, title="گروه تست")
            self.is_private = False
            self.replies = []
            self.reply_to = reply_to

        async def get_chat(self):
            return self.chat

        async def get_sender(self):
            return self.sender

        async def reply(self, text, **_kw):
            self.replies.append(text)
            return SimpleNamespace(id=next_id())

        async def respond(self, text, **_kw):
            return await self.reply(text)

    class Client:
        async def get_messages(self, chat_id, ids=None, limit=None):
            return None

        async def get_permissions(self, chat_id, user):
            return SimpleNamespace(is_admin=True)

        async def get_me(self):
            return SimpleNamespace(id=555, username="fox")

        async def get_entity(self, value):
            return SimpleNamespace(id=value)

        async def get_input_entity(self, value):
            return SimpleNamespace(id=value)

        async def send_message(self, target, text, **_kw):
            return SimpleNamespace(id=1)

        async def delete_messages(self, *a, **k):
            return True

        async def __call__(self, *a, **k):
            return SimpleNamespace(users=[], participants=[], full_chat=None)

    bot = SimpleNamespace(
        logger=Logger(),
        config_manager=Config(),
        tracker=Tracker(),
        client=Client(),
        group_timer_tasks={},
        bot_account_id=555,
        punished_users=set(),
        reply_input_peer_cache={},
        native_group_admin_cache={},
        spam_burst_users=set(),
        spam_burst_reset_at=0,
        last_message_at={},
        is_spam_locked=lambda key: False,
        has_public_username=lambda text: False,
        detector=SimpleNamespace(
            check_banned_words=lambda *a, **k: (False, None),
            has_public_username=lambda text: False,
        ),
    )

    NC.reset()
    start_event = Event("اسم چین", 7101)
    asyncio.run(handler.handle_new_message(bot, start_event))
    check("دستورِ «اسم چین» بازی را شروع کرد", len(start_event.replies) == 1,
          start_event.replies)
    check("متنِ شروع از مسیرِ هندلر هم همان متنِ کارفرماست",
          start_event.replies and start_event.replies[0].startswith("⛄️بازی اسم چین"),
          start_event.replies[:1])
    letter = NC.state(CHAT)["letter"]
    prompt_id = NC.state(CHAT)["prompt_id"]
    check("شناسهٔ پیامِ ربات ثبت شد", prompt_id == 9001, prompt_id)
    check("حرفِ شروع در متن آمده", "«%s»" % letter in start_event.replies[0])

    # پیامِ آزادِ گروه (بدونِ ریپلای) نباید جواب بگیرد.
    free_event = Event("سلام بچه‌ها", 7102)
    asyncio.run(handler.handle_new_message(bot, free_event))
    check("پیامِ بدونِ ریپلای هیچ پاسخی از بازی نگرفت", free_event.replies == [],
          free_event.replies)
    check("و وضعیتِ بازی دست‌نخورده ماند",
          NC.state(CHAT)["used"] == set() and NC.state(CHAT)["letter"] == letter)

    # ریپلای روی پیامِ ربات = نوبتِ بازی.
    name = next(
        (NC.NAME_DISPLAY[key] for key in NC.NAMES_BY_LETTER.get(letter, ())), None
    )
    turn_event = Event(name, 7103, reply_to=ReplyTo(prompt_id, 555))
    asyncio.run(handler.handle_new_message(bot, turn_event))
    check("ریپلای روی پیامِ ربات پردازش شد", len(turn_event.replies) == 1,
          turn_event.replies)
    check("ربات با نامِ تازه جواب داد",
          turn_event.replies and "من می‌گم" in turn_event.replies[0],
          turn_event.replies[:1])
    refreshed_prompt = NC.state(CHAT)["prompt_id"]
    check("شناسهٔ پیامِ تازهٔ ربات ثبت شد (نوبتِ بعد روی همین ریپلای شود)",
          refreshed_prompt != prompt_id and refreshed_prompt > prompt_id,
          (prompt_id, refreshed_prompt))

    # ریپلای روی پیامِ قدیمی (نه آخرین پیام) نادیده گرفته می‌شود.
    stale_event = Event("لاله", 7104, reply_to=ReplyTo(prompt_id, 555))  # پیامِ اولِ ربات
    asyncio.run(handler.handle_new_message(bot, stale_event))
    check("ریپلای روی پیامِ قبلیِ ربات نادیده گرفته شد",
          stale_event.replies == [], stale_event.replies)

    # «نمی‌دونم» با ریپلای ⇒ باختِ کاربر
    lose_event = Event(
        "نمی دونم", 7105, reply_to=ReplyTo(NC.state(CHAT)["prompt_id"], 555)
    )
    asyncio.run(handler.handle_new_message(bot, lose_event))
    check("«نمی‌دونم» از مسیرِ هندلر هم باخت داد",
          lose_event.replies == ["بازنده شدی ⛄️"], lose_event.replies)
    check("بازی بسته شد", not NC.is_active(CHAT))
    NC.reset()


def main():
    print("=" * 62)
    print("⛄️ تستِ بازی اسم‌چین")
    print("=" * 62)
    test_normalization()
    test_names_file()
    test_start()
    test_only_replies()
    test_validation()
    test_chain()
    test_player_loses()
    test_bot_loses()
    test_chat_isolation()
    test_handler_wiring()
    _handler_e2e()
    print("\n" + "=" * 62)
    print("نتیجه: %s پاس / %s شکست" % (PASS, FAIL))
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
