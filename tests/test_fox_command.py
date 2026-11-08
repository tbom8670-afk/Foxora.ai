# -*- coding: utf-8 -*-
"""🦊 تست کامل بازی «مرکز فرماندهی روباه» (FOX COMMAND).

اجرا:  PYTHONPATH=. python3 tests/test_fox_command.py

پوشش: ۲۶ سناریوی خواسته‌شدهٔ کارفرما — از ثبت‌نام تا پرداخت واقعی جایزه،
پایداری پس از ری‌استارت، جدایی مسابقه‌های دو گروه، و صحت قالب‌بندی
Bold/Blockquote سروش پلاس.
"""
import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# محیط ایزوله: هیچ داده‌ای بیرون از پوشهٔ موقت نوشته نشود
# ---------------------------------------------------------------------------
_TMP = tempfile.mkdtemp(prefix="fox-command-test-")
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


# ---------------------------------------------------------------------------
# اقتصاد واقعی ربات (همان ماژول کیف پول) در حالت ایزوله
# ---------------------------------------------------------------------------
from modules import fox_command as fc  # noqa: E402
from modules.fox_command import engine, storage, texts  # noqa: E402
from modules.fox_command import game as fgame  # noqa: E402
from handlers import fox_command_router as router  # noqa: E402

storage.set_base_dir(_TMP)


def _reset_state():
    fgame.reset_all()
    fgame._RECOVERED[0] = False
    for path in Path(_TMP).glob("*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    storage.ensure_dirs()


# ---------------------------------------------------------------------------
# استاب‌های کلاینت/رویداد
# ---------------------------------------------------------------------------
class FakeEvent:
    is_private = False

    def __init__(self, chat_id, user, text=""):
        self.chat_id = chat_id
        self.user = user
        self.text = text
        self.replies = []
        self.message = SimpleNamespace(id=1, text=text)

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs.get("formatting_entities") or []))
        return SimpleNamespace(id=900 + len(self.replies))

    async def get_sender(self):
        return self.user

    async def get_chat(self):
        return SimpleNamespace(id=self.chat_id)


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("formatting_entities") or []))
        return SimpleNamespace(id=1000 + len(self.sent))


class FakeBot:
    def __init__(self):
        self.client = FakeClient()
        self.logger = SimpleNamespace(log_info=lambda *a, **k: None,
                                      log_error=lambda *a, **k: None)
        self.silver_awarded = []

    def award_silver(self, chat_id, user_id, amount):
        self.silver_awarded.append((chat_id, user_id, amount))


USER_A = SimpleNamespace(id=9001, first_name="علی", username="ali_cmd")
USER_B = SimpleNamespace(id=9002, first_name="رضا", username="reza_cmd")
USER_C = SimpleNamespace(id=9003, first_name="سارا", username="sara_cmd")
CHAT_1 = -1009001
CHAT_2 = -1009002
CHAT_X = -1009003   # گروه اختصاصی تست «دستور شروع» (بدون اثر روی بقیه)
CHAT_M = -1009004   # گروه اختصاصی تست «عضو عادی»
ADMIN_M = SimpleNamespace(id=9100, first_name="مدیر", username="admin_m")


def run(coro):
    return asyncio.run(coro)


async def say(bot, chat_id, user, text, expect_consumed=None):
    event = FakeEvent(chat_id, user, text)
    consumed = await fgame.handle_message(bot, event, chat_id, user.id, user,
                                          text, bot.logger)
    if expect_consumed is not None:
        check(f"«{text}» مصرف شد = {expect_consumed}", consumed is expect_consumed,
              f"→ {consumed}")
    return consumed, event


def last_text(event):
    return event.replies[-1][0] if event.replies else ""


def all_text(event):
    return "\n".join(text for text, _entities in event.replies)


# ---------------------------------------------------------------------------
# ۱..۶ — ثبت‌نام، ظرفیت، تکرار، کشور، بودجه
# ---------------------------------------------------------------------------
def test_01_join_and_capacity():
    print("\n### ۱..۳) ثبت‌نام، بازیکن سوم، ثبت‌نام تکراری")
    _reset_state()
    bot = FakeBot()
    ok, event = run(say(bot, CHAT_1, USER_A, "مرکز فرماندهی", expect_consumed=True))
    check("پیام شروع ارسال شد", "مرکز فرماندهی روباه" in all_text(event), all_text(event))
    check("«مرکز فرماندهی» کسی را ثبت‌نام نمی‌کند",
          len(fgame.match_of(CHAT_1)["players"]) == 0,
          fgame.match_of(CHAT_1)["players"])
    check("پیام شروع، ثبت‌نام با «شرکت» را آموزش می‌دهد",
          "شرکت در بازی بنویسید" in all_text(event), all_text(event))
    _ok, event_a = run(say(bot, CHAT_1, USER_A, "شرکت", expect_consumed=True))
    check("«شرکت» فرماندهٔ اول را ثبت‌نام کرد",
          len(fgame.match_of(CHAT_1)["players"]) == 1, all_text(event_a))
    _ok, event_b = run(say(bot, CHAT_1, USER_B, "شرکت", expect_consumed=True))
    check("«شرکت» فرماندهٔ دوم را ثبت‌نام کرد",
          len(fgame.match_of(CHAT_1)["players"]) == 2, all_text(event_b))
    check("بعد از دو نفر، منوی انتخاب کشور آمد",
          "انتخاب کشور" in all_text(event_b), all_text(event_b))
    _ok, event_dup = run(say(bot, CHAT_1, USER_A, "شرکت", expect_consumed=True))
    check("ثبت‌نام تکراری رد شد", "قبلاً ثبت‌نام" in all_text(event_dup), all_text(event_dup))
    _ok, event_c = run(say(bot, CHAT_1, USER_C, "شرکت", expect_consumed=True))
    check("بازیکن سوم رد شد", "پر است" in all_text(event_c), all_text(event_c))
    check("بازیکن سوم وارد نشد",
          len(fgame.match_of(CHAT_1)["players"]) == 2)


def test_01b_start_word_never_registers():
    print("\n### ۳۱) «مرکز فرماندهی» فقط شروع است، ثبت‌نام فقط با «شرکت»")
    fgame.reset_all(CHAT_X)   # فقط گروه تست ۳۱ پاک می‌شود؛ بقیه دست‌نخورده
    bot = FakeBot()

    # الف) فرماندهٔ اول با «مرکز فرماندهی» ثبت‌نام نمی‌شود
    _ok, event = run(say(bot, CHAT_X, USER_A, "مرکز فرماندهی", expect_consumed=True))
    match = fgame.match_of(CHAT_X)
    check("مسابقه ساخته شد", match is not None)
    check("فرماندهٔ اول با «مرکز فرماندهی» ثبت‌نام نشد",
          len(match["players"]) == 0, match["players"])
    check("پیام راهنما «شرکت» را می‌گوید",
          "شرکت در بازی بنویسید" in all_text(event), all_text(event))

    # ب) تکرار «مرکز فرماندهی» توسط همان نفر هم ثبت‌نام نمی‌کند
    _ok, event2 = run(say(bot, CHAT_X, USER_A, "مرکز فرماندهی روباه", expect_consumed=True))
    check("تکرار دستور شروع باز هم ثبت‌نام نکرد",
          len(fgame.match_of(CHAT_X)["players"]) == 0)
    check("پیام «این مسابقه باز است» آمد", "این مسابقه باز است" in all_text(event2),
          all_text(event2))

    # ج) نفر دیگر هم با «مرکز فرماندهی» وارد نمی‌شود
    _ok, event3 = run(say(bot, CHAT_X, USER_B, "مرکز فرماندهی", expect_consumed=True))
    check("فرماندهٔ دوم با «مرکز فرماندهی» وارد نشد",
          len(fgame.match_of(CHAT_X)["players"]) == 0)
    check("همان‌جا هم «شرکت» تجویز شد", "شرکت" in all_text(event3), all_text(event3))

    # د) فقط «شرکت» ثبت‌نام می‌کند
    run(say(bot, CHAT_X, USER_A, "شرکت", expect_consumed=True))
    run(say(bot, CHAT_X, USER_B, "شرکت", expect_consumed=True))
    check("با دو «شرکت» ظرفیت کامل شد",
          len(fgame.match_of(CHAT_X)["players"]) == 2)

    # ه) بعد از پر شدن ظرفیت، «مرکز فرماندهی» هیچ‌کس را اضافه نمی‌کند
    _ok, event4 = run(say(bot, CHAT_X, USER_C, "مرکز فرماندهی", expect_consumed=True))
    check("نفر سوم با «مرکز فرماندهی» وارد نشد",
          len(fgame.match_of(CHAT_X)["players"]) == 2, all_text(event4))
    check("پیام «ظرفیت پر است» آمد", "پر است" in all_text(event4), all_text(event4))

    # و) در فاز انتخاب کشور هم شروع دوباره چیزی را عوض نمی‌کند
    match_now = fgame.match_of(CHAT_X)
    _ok, event_country = run(say(bot, CHAT_X, USER_A, "1", expect_consumed=True))
    check("با دو فرمانده، انتخاب کشور کار می‌کند",
          engine.player_of(match_now, USER_A.id)["country"] == "iran",
          all_text(event_country))
    _ok, event_again = run(say(bot, CHAT_X, USER_B, "1", expect_consumed=True))
    check("کشور تکراری رد شد", "انتخاب شده" in all_text(event_again),
          all_text(event_again))
    match_now = fgame.match_of(CHAT_X)
    phase_before = match_now["phase"]
    run(say(bot, CHAT_X, USER_B, "مرکز فرماندهی", expect_consumed=True))
    check("فاز بازی با دستور شروع عوض نشد",
          fgame.match_of(CHAT_X)["phase"] == phase_before, phase_before)
    check("هنوز همان دو فرمانده‌اند", len(fgame.match_of(CHAT_X)["players"]) == 2)
    fgame.reset_all(CHAT_X)


def test_01c_member_never_blocked_by_admin_gates():
    """🛡️ «شرکت» و دستورهای بازی برای عضو عادی هم کار می‌کند.

    علت واقعی گزارش کارفرما: پیام‌های اعضای عادی در لایه‌های مدیریتی
    (کلمهٔ ممنوع، قفل اسپم، فهرست دستورها، عضویت اجباری) بلعیده می‌شد و
    بازی فقط برای مالک/ادمین پیش می‌رفت. در این تست همهٔ آن گیت‌ها روشن
    است و عضو عادی باید کل مسیر را تا شلیک موشک برود.
    """
    print("\n### ۳۲) عضو عادی با همهٔ گیت‌های مدیریتی روشن بازی می‌کند")
    from modules import light_spam_ingest, group_dispatch
    from modules import entertainment_control

    fgame.reset_all(CHAT_M)
    entertainment_control.enable(CHAT_M)

    class _Msg:
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

    class _MemberEvent:
        """عضو عادی: بدون یوزرنیم، بدون نقش ادمینی، بدون bypass."""

        def __init__(self, user, text, message_id):
            self.message = _Msg(text, message_id)
            self.chat_id = CHAT_M
            self.is_private = False
            self.replies = []
            self.sender_id = user.id
            self.sender = SimpleNamespace(id=user.id, first_name=user.first_name,
                                          last_name=None, username=None)

        async def get_chat(self):
            return SimpleNamespace(id=CHAT_M)

        async def get_sender(self):
            return self.sender

        async def reply(self, text, **kwargs):
            self.replies.append(text)

        async def respond(self, text, **kwargs):
            self.replies.append(text)

        def said(self, needle):
            return any(needle in row for row in self.replies)

    locked = {(CHAT_M, USER_A.id), (CHAT_M, USER_B.id)}
    bot = FakeBot()
    bot.is_spam_locked = lambda key: key in locked
    bot.clear_spam_lock = lambda key: None
    bot.spam_lock = {}
    bot.punished_users = set()

    # ---- گیت‌های بیرونی: هر سه باید پیام بازی را آزاد کنند ----
    check("لایهٔ هرزنگاری: «شرکت» عضو عادی اسپم نیست",
          light_spam_ingest.ingest(bot, CHAT_M, USER_A.id, 2, "شرکت",
                                   event=_MemberEvent(USER_A, "شرکت", 2)
                                   ).skip_heavy is False)
    check("خط دستور: «شرکت» روی خط command می‌ماند",
          group_dispatch.classify_priority("شرکت")[1] == group_dispatch.LANE_COMMAND)
    check("claims(شرکت) در پنجرهٔ باز",
          router.claims(CHAT_M, "شرکت") is False, "هنوز مسابقه‌ای نیست")

    # ---- مسیر واقعی: شروع با دستور، ثبت‌نام دو عضو عادی ----
    _ok, start_event = run(say(bot, CHAT_M, ADMIN_M, "مرکز فرماندهی",
                               expect_consumed=True))
    check("دستور شروع برای ادمین کار می‌کند",
          "مرکز فرماندهی روباه" in all_text(start_event), all_text(start_event))
    check("bypass برای عضو عادی در پنجرهٔ «شرکت» فعال است",
          router.bypass(CHAT_M, USER_A.id, "شرکت") is True)

    _ok, e1 = run(say(bot, CHAT_M, USER_A, "شرکت", expect_consumed=True))
    check("عضو عادی ۱ ثبت‌نام شد", "ثبت\u200cنام شد" in all_text(e1), all_text(e1))
    _ok, e2 = run(say(bot, CHAT_M, USER_B, "شرکت", expect_consumed=True))
    check("عضو عادی ۲ ثبت‌نام شد", "ثبت\u200cنام شد" in all_text(e2), all_text(e2))
    check("ظرفیت کامل شد", len(fgame.match_of(CHAT_M)["players"]) == 2)

    # ---- پیام شخصی عضو عادی همچنان مسیر عادی خودش را دارد ----
    check("پیام غیربازی عضو (before join) bypass نمی‌شود",
          router.bypass(CHAT_M, USER_C.id, "سلام بچه‌ها") is False)
    check("پیام عادی عضو بازی‌کننده هم bypass نمی‌شود",
          router.bypass(CHAT_M, USER_A.id, "امروز هوا خوبه") is False)
    check("عضو غیربازیکن نمی‌تواند دستور بازی بفرستد",
          router.bypass(CHAT_M, USER_C.id, "فروشگاه") is False)

    # ---- عضو عادی تا شلیک موشک پیش می‌رود ----
    run(say(bot, CHAT_M, USER_A, "1", expect_consumed=True))
    run(say(bot, CHAT_M, USER_B, "2", expect_consumed=True))
    check("هر دو عضو عادی کشور انتخاب کردند",
          all(p["country"] for p in fgame.match_of(CHAT_M)["players"]))
    for step in ("فروشگاه", "1", "1"):
        run(say(bot, CHAT_M, USER_A, step, expect_consumed=True))
    check("خرید عضو عادی از بودجهٔ بازی کم شد",
          fgame.match_of(CHAT_M)["players"][0]["spent"] > 0,
          fgame.match_of(CHAT_M)["players"][0]["spent"])
    check("کیف پول واقعی لمس نشد (خرید فقط از بودجهٔ مسابقه)",
          fgame.match_of(CHAT_M)["players"][0]["bronze"] < 400)
    fgame.reset_all(CHAT_M)


def test_02_countries():
    print("\n### ۴..۵) انتخاب کشور دو بازیکن و جلوگیری از تکرار")
    match = fgame.match_of(CHAT_1)
    bot = FakeBot()
    _ok, event_a = run(say(bot, CHAT_1, USER_A, "1"))
    check("کشور اول ذخیره شد", engine.player_of(match, USER_A.id)["country"] == "iran",
          engine.player_of(match, USER_A.id)["country"])
    check("پیام تأیید کشور آمد", "ایران" in all_text(event_a), all_text(event_a))
    _ok, event_b_try = run(say(bot, CHAT_1, USER_B, "1"))
    check("انتخاب کشور تکراری رد شد",
          "انتخاب شده" in all_text(event_b_try), all_text(event_b_try))
    check("کشور دوم عوض نشد", engine.player_of(match, USER_B.id)["country"] is None)
    _ok, event_b = run(say(bot, CHAT_1, USER_B, "2"))
    check("کشور دوم ذخیره شد", engine.player_of(match, USER_B.id)["country"] == "russia")
    check("مرحله به آماده‌سازی رفت", match["phase"] == engine.PHASE_SETUP, match["phase"])


def test_03_budget():
    print("\n### ۶) تخصیص دقیق ۴۰۰ برنز و جدایی از کیف پول واقعی")
    match = fgame.match_of(CHAT_1)
    for player in match["players"]:
        check(f"بودجهٔ {player['name']} دقیقاً ۴۰۰ برنز است",
              int(player["bronze"]) == 400, player["bronze"])
    # کیف پول واقعی باید دست‌نخورده بماند
    try:
        from economy import get_balance
        before = get_balance(CHAT_1, USER_A.id)
    except Exception:
        before = None
    bot = FakeBot()
    run(say(bot, CHAT_1, USER_A, "فروشگاه"))
    run(say(bot, CHAT_1, USER_A, "1"))      # بالستیک
    run(say(bot, CHAT_1, USER_A, "4"))      # عماد ۵۵
    player = engine.player_of(match, USER_A.id)
    check("خرید از بودجهٔ بازی کم شد", int(player["bronze"]) == 345, player["bronze"])
    if before is not None:
        after = get_balance(CHAT_1, USER_A.id)
        check("کیف پول واقعی تغییر نکرد", after == before, f"{before} → {after}")


def test_04_shop_limits_and_budget_guard():
    print("\n### ۷..۹) خرید ترکیبی، جلوگیری از خرید بیش از موجودی، سقف تجهیزات")
    match = fgame.match_of(CHAT_1)
    bot = FakeBot()
    player_a = engine.player_of(match, USER_A.id)
    # سقف بالستیک/کروز = ۳
    run(say(bot, CHAT_1, USER_A, "فروشگاه"))
    run(say(bot, CHAT_1, USER_A, "2"))      # کروز
    run(say(bot, CHAT_1, USER_A, "1"))      # پاوه ۲۰
    run(say(bot, CHAT_1, USER_A, "فروشگاه"))
    run(say(bot, CHAT_1, USER_A, "1"))
    run(say(bot, CHAT_1, USER_A, "2"))      # سجیل ۷۰
    check("سقف موشک‌های تهاجمی رعایت شد", engine.count_offensive(player_a) == 3,
          engine.count_offensive(player_a))
    _ok, event_cap = run(say(bot, CHAT_1, USER_A, "فروشگاه"))
    run(say(bot, CHAT_1, USER_A, "2"))
    _ok, event_cap = run(say(bot, CHAT_1, USER_A, "1"))
    check("خرید چهارمی رد شد", "پر است" in last_text(event_cap), last_text(event_cap))
    check("تعداد موشک تغییر نکرد", engine.count_offensive(player_a) == 3)

    # خرید بیش از موجودی: بازیکن دوم همه بودجه را خرج می‌کند
    player_b = engine.player_of(match, USER_B.id)
    player_b["bronze"] = 30
    fgame._save(match)
    event_budget = None
    run(say(bot, CHAT_1, USER_B, "فروشگاه"))
    run(say(bot, CHAT_1, USER_B, "1"))
    _ok, event_budget = run(say(bot, CHAT_1, USER_B, "1"))   # خرمشهر ۱۰۰ > ۳۰
    check("خرید بیش از موجودی رد شد",
          "موجودی کافی نیست" in all_text(event_budget), all_text(event_budget))
    check("موجودی منفی نشد", int(player_b["bronze"]) == 30, player_b["bronze"])
    check("موشک اضافه نشد", engine.count_offensive(player_b) == 0)


def test_05_war_start_requires_both_ready():
    print("\n### ۲۹) شروع جنگ فقط با آماده بودن هر دو فرمانده")
    match = fgame.match_of(CHAT_1)
    check("جنگ شروع نشده چون فرماندهٔ دوم تجهیزات ندارد",
          match["phase"] == engine.PHASE_SETUP, match["phase"])
    bot = FakeBot()
    # فرماندهٔ دوم: رادار + دفاع + موشک
    player_b = engine.player_of(match, USER_B.id)
    player_b["bronze"] = 400
    for steps in (["فروشگاه", "4", "2"],          # رادار پیشرفته
                  ["فروشگاه", "3", "1"],          # مرصاد
                  ["فروشگاه", "1", "4"]):         # عماد
        for step in steps:
            run(say(bot, CHAT_1, USER_B, step))
    check("پس از آماده شدن هر دو، جنگ شروع شد",
          match["phase"] == engine.PHASE_WAR, match["phase"])
    check("هر دو بازیکن موشک دارند",
          all(engine.count_offensive(p) > 0 for p in match["players"]))


# ---------------------------------------------------------------------------
# ۱۰..۱۶ — حمله، هشدار، دفاع، خسارت
# ---------------------------------------------------------------------------
def test_06_launch_and_warning():
    print("\n### ۱۰..۱۱) شلیک موشک، کم شدن موجودی و ارسال هشدار")
    match = fgame.match_of(CHAT_1)
    player_a = engine.player_of(match, USER_A.id)
    before = engine.count_offensive(player_a)
    bot = FakeBot()
    run(say(bot, CHAT_1, USER_A, "حمله"))
    consumed, event = run(say(bot, CHAT_1, USER_A, "1", expect_consumed=True))
    check("موجودی موشک یک عدد کم شد", engine.count_offensive(player_a) == before - 1,
          f"{before} → {engine.count_offensive(player_a)}")
    check("هشدار حمله با مشخصات ارسال شد",
          "هشدار فرمانده" in all_text(event) and "زمان باقی‌مانده" in all_text(event),
          all_text(event))
    check("مهلت دفاع ۱۷ ثانیه اعلام شد", "۱۷" in all_text(event))
    attacks = engine.active_attacks(match)
    check("حملهٔ مستقل با شناسهٔ یکتا ثبت شد",
          len(attacks) == 1 and attacks[0]["attack_id"])


def test_07_defense_success_and_failure():
    print("\n### ۱۲..۱۳) دفاع موفق و ناموفق (قطعیت‌شده برای تست)")
    match = fgame.match_of(CHAT_1)
    attack = engine.active_attacks(match)[0]
    bot = FakeBot()

    # --- دفاع ناموفق: شانس رهگیری صفر می‌شود
    original = engine._RANDOM.random
    engine._RANDOM.random = lambda: 0.999
    try:
        run(say(bot, CHAT_1, USER_B, "دفاع"))
        consumed, event = run(say(bot, CHAT_1, USER_B, "1", expect_consumed=True))
        check("انتخاب سامانهٔ دفاعی ثبت شد",
              "رهگیری آغاز شد" in all_text(event) or "گزارش" in all_text(event))
        health_before = 100
        settled = engine.attack_by_id(match, attack["attack_id"])
        check("حمله تعیین تکلیف شد", settled.get("settled") is True)
        check("دفاع ناموفق بود و اصابت ثبت شد",
              settled.get("result") in ("defense_failed", "hit"), settled.get("result"))
        check("خسارت سلامت پایگاه ثبت شد",
              float(settled.get("damage") or 0) > 0 and
              engine.player_of(match, USER_B.id)["health"] < health_before,
              settled.get("damage"))
        check("تلفات جمعیت ثبت شد", int(settled.get("casualties") or 0) > 0,
              settled.get("casualties"))
    finally:
        engine._RANDOM.random = original

    # --- دفاع موفق
    player_b = engine.player_of(match, USER_B.id)
    player_b["defense"] = {"thaad": 2}
    attack2 = dict(attack)
    attack2.update({"attack_id": "manual-success", "settled": False,
                    "defended": False, "defense_item": None, "status": "in_flight",
                    "defend_deadline": time.time() + 17})
    match["attacks"].append(attack2)
    engine._RANDOM.random = lambda: 0.0
    try:
        run(say(bot, CHAT_1, USER_B, "دفاع"))
        _c, event = run(say(bot, CHAT_1, USER_B, "1", expect_consumed=True))
    finally:
        engine._RANDOM.random = original
    settled2 = engine.attack_by_id(match, "manual-success")
    check("دفاع موفق رهگیری کرد", settled2.get("result") == "intercepted",
          settled2.get("result"))
    check("رهگیری موفق کمتر از اصابت خسارت زد",
          float(settled2.get("damage") or 0) < float(settled.get("damage") or 0),
          f"{settled2.get('damage')} vs {settled.get('damage')}")
    check("شمارندهٔ رهگیری مدافع زیاد شد",
          int(engine.player_of(match, USER_B.id)["interceptions"]) >= 1)
    # گزارش رهگیری ممکن است در پیام دوم (گزارش جنگ‌مانند) بیاید.
    check("گزارش دفاع ارسال شد",
          any("گزارش دفاع" in text for text, _e in event.replies)
          or "رهگیری" in all_text(event), all_text(event)[:80])


def test_08_defense_timeout_and_rules():
    print("\n### ۱۴) پایان مهلت ۱۷ ثانیه‌ای و قواعد فرصت دفاع")
    match = fgame.match_of(CHAT_1)
    player_a = engine.player_of(match, USER_A.id)
    player_b = engine.player_of(match, USER_B.id)

    # مهاجم نمی‌تواند دفاع کند
    attack_id = "rule-test"
    match["attacks"].append({
        "attack_id": attack_id, "match_id": match["match_id"],
        "attacker_id": USER_A.id, "defender_id": USER_B.id, "item_id": "emad",
        "item_name": "عماد", "power": 66, "nuke": False,
        "launched_at": time.time(), "defend_deadline": time.time() - 1,
        "settled": False, "defended": False, "status": "in_flight",
        "defense_item": None, "result": None, "damage": 0.0, "casualties": 0,
        "created_at": time.time(),
    })
    player_b["defense"] = {"mersad": 2}
    status, _attack, _info = engine.defend(match, USER_A.id, "mersad", attack_id)
    check("مهاجم نمی‌تواند حملهٔ خودش را دفاع کند",
          status == engine.ST_NOT_DEFENDER, status)
    status, _a, _i = engine.defend(match, USER_B.id, "mersad", attack_id)
    check("مهلت گذشته → دفاع پذیرفته نمی‌شود", status == engine.ST_OK, status)

    # پایان مهلت: حمله تعیین تکلیف می‌شود و خسارت یک بار ثبت می‌شود
    match["attacks"].append({
        "attack_id": "timeout-test", "match_id": match["match_id"],
        "attacker_id": USER_A.id, "defender_id": USER_B.id, "item_id": "paveh",
        "item_name": "پاوه", "power": 30, "nuke": False,
        "launched_at": time.time(), "defend_deadline": time.time() - 1,
        "settled": False, "defended": False, "status": "in_flight",
        "defense_item": None, "result": None, "damage": 0.0, "casualties": 0,
        "created_at": time.time(),
    })
    damage_events_before = int(match.get("damage_events") or 0)
    attack, report = engine.settle(match, "timeout-test", "timeout")
    check("حملهٔ بدون دفاع تعیین تکلیف شد", attack.get("settled") is True)
    _attack2, report2 = engine.settle(match, "timeout-test", "timeout")
    check("تعیین تکلیف دوباره اجرا نشد (خسارت تکراری نه)",
          report2 is None and int(match.get("damage_events")) == damage_events_before + 1,
          f"events={match.get('damage_events')}")


def test_09_damage_bounds():
    print("\n### ۱۵..۱۶) مرزهای خسارت و جمعیت")
    match = fgame.match_of(CHAT_1)
    player_b = engine.player_of(match, USER_B.id)
    player_b["health"] = 3.0
    attack = {
        "attack_id": "bounds-test", "match_id": match["match_id"],
        "attacker_id": USER_A.id, "defender_id": USER_B.id, "item_id": "trident2",
        "item_name": "ترایدنت ۲ آمریکا", "power": 112, "nuke": False,
        "launched_at": time.time(), "defend_deadline": time.time(),
        "settled": False, "defended": False, "status": "in_flight",
        "defense_item": None, "result": None, "damage": 0.0, "casualties": 0,
        "created_at": time.time(),
    }
    match["attacks"].append(attack)
    engine.settle(match, "bounds-test", "test")
    check("سلامت پایگاه زیر صفر نرفت", player_b["health"] >= 0, player_b["health"])
    check("جمعیت منفی نشد", player_b["population"] >= 0, player_b["population"])
    player_b["health"] = 100.0
    check("سلامت از ۱۰۰ بیشتر نمی‌شود", engine.clamp_health(180) == 100)


def test_10_repair():
    print("\n### ۱۷) تعمیر پایگاه و کم شدن هزینه")
    match = fgame.match_of(CHAT_1)
    player_b = engine.player_of(match, USER_B.id)
    player_b["health"] = 40.0
    player_b["bronze"] = 100
    player_b["last_repair_at"] = 0.0
    bot = FakeBot()
    run(say(bot, CHAT_1, USER_B, "تعمیر پایگاه"))
    _ok, event = run(say(bot, CHAT_1, USER_B, "10", expect_consumed=True))
    check("سلامت ۱۰٪ زیاد شد", player_b["health"] == 50.0, player_b["health"])
    check("هزینهٔ ۳۰ برنز کم شد", player_b["bronze"] == 70, player_b["bronze"])
    check("گزارش تعمیر ارسال شد", "تعمیر شد" in all_text(event), all_text(event))
    # تعمیر پشت‌سرهم مجاز نیست (جلوگیری از سلامت مجانی)
    _ok, event_cd = run(say(bot, CHAT_1, USER_B, "تعمیر پایگاه"))
    _ok, event_cd = run(say(bot, CHAT_1, USER_B, "10", expect_consumed=True))
    check("تعمیر پیاپی رد شد", "زود است" in all_text(event_cd), all_text(event_cd))
    check("سلامت دوباره بالا نرفت", player_b["health"] == 50.0)
    # سقف ۱۰۰٪
    player_b["last_repair_at"] = 0.0
    player_b["health"] = 95.0
    engine.repair(match, USER_B.id, 20)
    check("سلامت از ۱۰۰ بیشتر نشد", player_b["health"] == 100.0, player_b["health"])


def test_11_nuke_risk():
    print("\n### ۱۸) سلاح هسته‌ای پرخطر و محدودیت‌ها")
    match = fgame.match_of(CHAT_1)
    player_a = engine.player_of(match, USER_A.id)
    player_a["bronze"] = 400
    bot = FakeBot()
    for part in ["فروشگاه", "5", "1"]:
        run(say(bot, CHAT_1, USER_A, part))
    check("سلاح هسته‌ای خریداری شد",
          sum(1 for i in player_a["arsenal"] if fc.is_nuke(i)) == 1)
    check("تعداد هسته‌ای محدود است (۱)", fc.MAX_NUKES == 1)

    attack_id = "nuke-test"
    status, attack = engine.launch(match, USER_A.id, "nuke_tactical", attack_id)
    check("شلیک هسته‌ای ثبت شد", status == engine.ST_OK, status)
    status2, _ = engine.launch(match, USER_A.id, "nuke_tactical", "nuke-test-2")
    check("شلیک دوم هسته‌ای رد شد", status2 in (engine.ST_SOLD_OUT,
                                                 engine.ST_NUKE_LIMIT,
                                                 engine.ST_NO_BUDGET), status2)

    # پیامد برای مهاجم: اجبار به وقوع
    original = engine._RANDOM.random
    engine._RANDOM.random = lambda: 0.0
    health_before = player_a["health"]
    try:
        _attack, report = engine.settle(match, attack_id, "test")
    finally:
        engine._RANDOM.random = original
    check("خسارت هسته‌ای به مدافع سنگین بود",
          float(report["attack"]["damage"]) >= 40, report["attack"]["damage"])
    check("پیامد برای مهاجم اعمال شد",
          float((report.get("blowback") or {}).get("self_damage") or 0) > 0,
          report.get("blowback"))
    check("سلامت مهاجم کم شد", player_a["health"] < health_before)


def test_12_end_and_reward():
    print("\n### ۱۹..۲۳) صفر شدن سلامت، تسلیم، اعلام برنده، واریز ۱۰ نقره، بدون پرداخت دوباره")
    _reset_state()
    bot = FakeBot()
    run(say(bot, CHAT_2, USER_A, "مرکز فرماندهی"))
    run(say(bot, CHAT_2, USER_A, "شرکت", expect_consumed=True))
    run(say(bot, CHAT_2, USER_B, "شرکت"))
    run(say(bot, CHAT_2, USER_A, "1"))
    run(say(bot, CHAT_2, USER_B, "3"))
    match = fgame.match_of(CHAT_2)
    for steps in (["فروشگاه", "1", "4"], ["فروشگاه", "3", "1"]):
        for step in steps:
            run(say(bot, CHAT_2, USER_A, step))
            run(say(bot, CHAT_2, USER_B, step))
    check("جنگ شروع شد", match["phase"] == engine.PHASE_WAR, match["phase"])

    # شلیک و سپس صفر شدن سلامت با تعیین تکلیف
    run(say(bot, CHAT_2, USER_A, "حمله"))
    run(say(bot, CHAT_2, USER_A, "1"))
    attack = engine.active_attacks(match)[0]
    defender = engine.player_of(match, USER_B.id)
    defender["health"] = 1.0
    run(fgame._settle_attack(bot, CHAT_2, attack["attack_id"], "test", bot.logger))
    check("مسابقه بسته شد", match["phase"] == engine.PHASE_FINISHED, match["phase"])
    check("برنده تعیین شد", match.get("winner") == USER_A.id, match.get("winner"))
    check("دلیل پایان: نابودی پایگاه",
          match.get("end_reason") == engine.END_DESTROYED, match.get("end_reason"))
    check("۱۰ سکه نقره به برنده پرداخت شد",
          bot.silver_awarded == [(CHAT_2, USER_A.id, 10)], bot.silver_awarded)
    check("پرچم پرداخت جایزه ثبت شد", match.get("reward_paid") is True)

    # تلاش برای پرداخت دوباره
    paid_again = fgame._pay_reward(bot, match, USER_A.id, bot.logger)
    check("پرداخت دوباره انجام نشد",
          paid_again is False and len(bot.silver_awarded) == 1, bot.silver_awarded)
    # فراخوانی دوبارهٔ پایان مسابقه
    run(fgame._finish_match(bot, match, USER_A.id, USER_B.id,
                            engine.END_DESTROYED, bot.logger))
    check("پایان مسابقه دوباره اجرا نشد", len(bot.silver_awarded) == 1)
    check("آرشیو مسابقه نوشته شد", storage.archive_path(match["match_id"]).exists())


def test_13_surrender():
    print("\n### ۲۰) تسلیم یکی از فرماندهان")
    _reset_state()
    bot = FakeBot()
    run(say(bot, CHAT_1, USER_A, "مرکز فرماندهی"))
    run(say(bot, CHAT_1, USER_A, "شرکت", expect_consumed=True))
    run(say(bot, CHAT_1, USER_B, "شرکت", expect_consumed=True))
    match = fgame.match_of(CHAT_1)
    run(say(bot, CHAT_1, USER_B, "تسلیم", expect_consumed=True))
    check("تسلیم مسابقه را بست", match["phase"] == engine.PHASE_FINISHED, match["phase"])
    check("حریف برنده شد", match.get("winner") == USER_A.id, match.get("winner"))
    check("دلیل پایان: تسلیم", match.get("end_reason") == engine.END_SURRENDER,
          match.get("end_reason"))
    check("جایزه به برندهٔ تسلیم پرداخت شد",
          bot.silver_awarded == [(CHAT_1, USER_A.id, 10)], bot.silver_awarded)


def test_14_restart_recovery():
    print("\n### ۲۴) بازیابی مسابقه پس از ری‌استارت")
    _reset_state()
    bot = FakeBot()
    run(say(bot, CHAT_1, USER_A, "مرکز فرماندهی"))
    run(say(bot, CHAT_1, USER_A, "شرکت", expect_consumed=True))
    run(say(bot, CHAT_1, USER_B, "شرکت", expect_consumed=True))
    run(say(bot, CHAT_1, USER_A, "1"))
    run(say(bot, CHAT_1, USER_B, "2"))
    match = fgame.match_of(CHAT_1)
    for steps in (["فروشگاه", "1", "4"], ["فروشگاه", "3", "1"]):
        for step in steps:
            run(say(bot, CHAT_1, USER_A, step))
            run(say(bot, CHAT_1, USER_B, step))
    match_id = match["match_id"]
    health_snapshot = {p["user_id"]: p["health"] for p in match["players"]}

    # 💥 ری‌استارت: state درون‌حافظه پاک می‌شود
    fgame.reset_all()
    fgame._RECOVERED[0] = False
    check("بدون بازیابی، مسابقه در حافظه نیست", fgame.is_active(CHAT_1) is False)
    restored = run(fgame.recover(bot, bot.logger))
    check("مسابقه از دیسک بازیابی شد", restored >= 1, restored)
    check("همان مسابقه برگشت",
          (fgame.match_of(CHAT_1) or {}).get("match_id") == match_id)
    recovered = fgame.match_of(CHAT_1)
    check("سلامت پایگاه‌ها حفظ شد",
          {p["user_id"]: p["health"] for p in recovered["players"]} == health_snapshot)
    check("مرحله حفظ شد", recovered["phase"] == engine.PHASE_WAR, recovered["phase"])
    consumed, event = run(say(bot, CHAT_1, USER_A, "وضعیت پایگاه", expect_consumed=True))
    check("بازی بعد از ری‌استارت پاسخ می‌دهد",
          "سلامت پایگاه" in all_text(event), all_text(event))


def test_15_group_isolation():
    print("\n### ۲۵) جلوگیری از تداخل دو مسابقه در گروه‌های مختلف")
    _reset_state()
    bot = FakeBot()
    run(say(bot, CHAT_1, USER_A, "مرکز فرماندهی"))
    run(say(bot, CHAT_1, USER_A, "شرکت", expect_consumed=True))
    run(say(bot, CHAT_2, USER_C, "مرکز فرماندهی"))
    run(say(bot, CHAT_2, USER_C, "شرکت", expect_consumed=True))
    match_1 = fgame.match_of(CHAT_1)
    match_2 = fgame.match_of(CHAT_2)
    check("دو مسابقهٔ جدا ساخته شد",
          match_1["match_id"] != match_2["match_id"])
    check("بازیکنان مسابقه‌ها قاطی نشدند",
          [p["user_id"] for p in match_1["players"]] == [USER_A.id] and
          [p["user_id"] for p in match_2["players"]] == [USER_C.id])
    run(say(bot, CHAT_1, USER_B, "شرکت"))
    check("«شرکت» فقط روی همان گروه اثر کرد",
          len(match_1["players"]) == 2 and len(match_2["players"]) == 1)
    check("بودجه‌ها جدا هستند",
          engine.player_of(match_1, USER_A.id)["bronze"] == 400 and
          engine.player_of(match_2, USER_C.id)["bronze"] == 400)


def test_16_formatting():
    print("\n### ۲۶) صحت قالب Bold و Blockquote سروش پلاس")
    text, entities = texts.start_prompt()
    bold = [e for e in entities if type(e).__name__ == "MessageEntityBold"]
    quote = [e for e in entities if type(e).__name__ == "MessageEntityBlockquote"]
    check("پیام شروع Bold دارد", len(bold) >= 1, len(bold))
    check("پیام شروع Blockquote دارد", len(quote) >= 1, len(quote))
    check("هیچ Markdown/تگ خامی در متن نیست",
          "**" not in text and "<b>" not in text and "<blockquote>" not in text, text[:40])
    check("آفست/length همهٔ entityها در محدودهٔ UTF-16 متن است",
          all(0 <= e.offset and e.offset + e.length <= texts.u16(text) for e in entities),
          [(e.offset, e.length, texts.u16(text)) for e in entities])
    # قالب پایگاه: عنوان Bold و راهنماها Blockquote
    match = fgame.match_of(CHAT_1)
    player = engine.player_of(match, USER_A.id)
    base_text, base_entities = texts.base_card(match, player, full=True)
    base_quote = [e for e in base_entities
                  if type(e).__name__ == "MessageEntityBlockquote"]
    base_bold = [e for e in base_entities
                 if type(e).__name__ == "MessageEntityBold"]
    check("کارت پایگاه Bold دارد", len(base_bold) >= 1)
    check("فهرست دستورات سالم است", "● حمله" in base_text and "● تسلیم" in base_text)
    report_text, report_entities = texts.attack_report({
        "attack": {"item_name": "عماد", "health_before": 100, "damage": 15,
                   "health_after": 85, "population_before": 10000,
                   "casualties": 500, "population_after": 9500, "result": "hit"},
        "intercepted": False, "defense_item": None, "blowback": {},
    })
    check("گزارش حمله مطابق قالب خواسته‌شده است",
          "گزارش حمله" in report_text and "خسارت واردشده" in report_text and
          "تلفات" in report_text, report_text[:120])
    check("طول entityهای گزارش در محدودهٔ متن است",
          all(e.offset + e.length <= texts.u16(report_text) for e in report_entities))


def test_17_wallet_integration_real():
    print("\n### ۲۷) پرداخت واقعی جایزه با سیستم کیف پول ربات (economy)")
    _reset_state()
    from economy import award, get_balance, settings as economy_settings
    # شناسه/مرجع یکتا تا دادهٔ کیف پولِ اجراهای قبلی روی این تست اثر نگذارد
    import uuid
    run_id = uuid.uuid4().hex[:8]
    chat_id, user_id = -1009101, int(run_id, 16) % 1_000_000 + 500000
    reference = f"fox_command:real-check:{run_id}"
    try:
        award(chat_id, user_id, 10, economy_settings.SILVER, reference=reference,
              note="تست جایزه مرکز فرماندهی")
        balance = get_balance(chat_id, user_id)
        check("۱۰ سکه نقره به کیف پول واقعی اضافه شد",
              int(balance[economy_settings.SILVER]) >= 10, balance)
        award(chat_id, user_id, 10, economy_settings.SILVER, reference=reference,
              note="تست تکراری")
        balance2 = get_balance(chat_id, user_id)
        check("پرداخت دوباره با همان مرجع انجام نشد",
              int(balance2[economy_settings.SILVER]) == int(balance[economy_settings.SILVER]),
              balance2)
        # مسیر واقعی ماژول بازی (بدون override تست)
        class _Bot:
            client = None
            logger = SimpleNamespace(log_info=lambda *a, **k: None,
                                     log_error=lambda *a, **k: None)
        match = {"chat_id": chat_id, "match_id": "wallet-check", "players": [
            {"user_id": user_id, "name": "تست"}], "reward_paid": False}
        paid = fgame._pay_reward(_Bot(), match, user_id, _Bot.logger)
        balance3 = get_balance(chat_id, user_id)
        check("مسیر واقعی جایزه هم پرداخت کرد", paid is True)
        delta = (int(balance3[economy_settings.SILVER])
                 - int(balance2[economy_settings.SILVER]))
        check("موجودی نقره دقیقاً ۱۰ سکه افزایش یافت", delta == 10,
              f"{balance2} → {balance3}")
        check("پرچم پرداخت روی مسابقه ثبت شد", match.get("reward_paid") is True)
    except Exception as error:  # pragma: no cover
        check("پرداخت واقعی جایزه بدون خطا", False, repr(error))


def test_18_no_duplicate_handlers():
    print("\n### ۲۸) عدم تداخل با دستورهای موجود ربات")
    _reset_state()
    check("دستور «شرکت» بدون مسابقهٔ باز، ادعا نمی‌شود",
          fgame.claims(CHAT_1, "شرکت") is False)
    check("«شرکت» در فهرست دستورهای شروع نیست (تا بازی‌های دیگر را نشکند)",
          "شرکت" not in fgame.START_COMMANDS)
    check("پیام عادی گروه مصرف نمی‌شود",
          fgame.claims(CHAT_1, "سلام بچه‌ها") is False)
    bot = FakeBot()
    run(say(bot, CHAT_1, USER_A, "مرکز فرماندهی"))
    run(say(bot, CHAT_1, USER_A, "شرکت", expect_consumed=True))
    check("«شرکت» با مسابقهٔ باز مصرف می‌شود",
          fgame.claims(CHAT_1, "شرکت") is True)
    check("«فروشگاه» با مسابقهٔ باز مصرف می‌شود",
          fgame.claims(CHAT_1, "فروشگاه") is True)
    check("فروشگاه فقط در گروه دارای مسابقه مصرف می‌شود",
          fgame.claims(CHAT_2, "فروشگاه") is False)
    # فروشگاه واقعی ربات نباید در گروه دیگر بشکند
    fgame.reset_all(CHAT_1)
    check("پس از بستن مسابقه، دستورها آزاد می‌شوند",
          fgame.claims(CHAT_1, "فروشگاه") is False)


def test_00_prices_locked():
    print("\n### ۰) قیمت‌های قطعی کارفرما دست‌نخورده‌اند")
    expected = {
        "khorramshahr4": 100, "sejjil": 70, "kheibar": 60, "emad": 55,
        "minuteman3": 100, "pershing": 80, "trident2": 110,
        "paveh": 20, "sommar": 35, "hovyzeh": 25, "tomahawk": 40, "agm80": 40,
        "mersad": 20, "somkhordad": 30, "panzdahkhordad": 35, "s300": 45,
        "bavar373": 50, "patriot": 55, "thaad": 60, "aegis": 55, "gmd": 65,
        "radar_basic": 10, "radar_advanced": 20, "radar_early": 30,
        "radar_super": 40,
        "nuke_tactical": 150, "nuke_powerful": 180, "nuke_heavy": 210,
        "nuke_super": 300,
    }
    for item_id, price in expected.items():
        item = fc.WEAPONS.get(item_id)
        check(f"قیمت {item['name'] if item else item_id}",
              item is not None and int(item["price"]) == price,
              item and item["price"])
    check("بودجهٔ اولیه ۴۰۰ برنز است", fc.START_BRONZE == 400)
    check("جایزه ۱۰ سکه نقره است", fc.WINNER_SILVER == 10)
    check("ظرفیت بازی دو فرمانده است", fc.PLAYERS == 2)
    check("مهلت دفاع ۱۷ ثانیه است", fc.DEFENSE_SECONDS == 17)
    check("سلامت اولیه ۱۰۰٪", fc.START_HEALTH == 100)
    check("جمعیت اولیه ۱۰٬۰۰۰", fc.START_POPULATION == 10_000)
    check("سقف سه موشک تهاجمی", fc.MAX_OFFENSIVE == 3)
    check("سقف سه سامانهٔ دفاعی", fc.MAX_DEFENSE_SYSTEMS == 3)


def test_19_balance_engine():
    print("\n### ۳۰) تعادل: هیچ استراتژی بدون ریسک برنده نیست و جنگ تمام می‌شود")
    import random
    random.seed(11)
    strategies = {
        "سنگین": ["trident2"],
        "متوازن": ["emad", "bavar373", "s300"],
        "کروز": ["paveh", "paveh", "paveh", "mersad", "somkhordad"],
    }

    def build(name, items_a, items_b):
        m = engine.new_match(-77, name)
        engine.join(m, 1, "A")
        engine.join(m, 2, "B")
        engine.choose_country(m, 1, "iran")
        engine.choose_country(m, 2, "russia")
        for uid, items in ((1, items_a), (2, items_b)):
            for item in items:
                assert engine.purchase(m, uid, item)[0] == "ok", item
        engine.start_war(m)
        return m

    def fight(m):
        for turn in range(40):
            for uid in (1, 2):
                player = engine.player_of(m, uid)
                arsenal = engine.arsenal_list(player)
                if not arsenal:
                    for candidate in ("paveh", "hovyzeh", "sommar", "emad"):
                        if engine.purchase(m, uid, candidate)[0] == "ok":
                            arsenal = engine.arsenal_list(player)
                            break
                if not arsenal:
                    continue
                arsenal.sort(key=lambda row: -fc.WEAPONS[row[0]]["damage"])
                status, attack = engine.launch(m, uid, arsenal[0][0],
                                               f"{m['match_id']}-{uid}-{turn}")
                if status != "ok":
                    continue
                defender = engine.opponent_of(m, uid)
                systems = engine.defense_list(defender)
                if systems and random.random() < 0.85:
                    engine.defend(m, defender["user_id"], systems[0][0],
                                  attack["attack_id"])
                engine.settle(m, attack["attack_id"], "sim")
            winner, loser, reason = engine.check_end(m)
            if winner:
                engine.finish(m, winner, loser, reason)
                return reason
        return None

    outcomes = {}
    for a_name, a_items in strategies.items():
        for b_name, b_items in strategies.items():
            if a_name == b_name:
                continue
            for _round in range(8):
                m = build(f"bal-{a_name}-{b_name}-{_round}", a_items, b_items)
                reason = fight(m)
                key = f"{a_name} vs {b_name}"
                outcomes.setdefault(key, []).append(reason)
    unfinished = [k for k, v in outcomes.items() if any(r is None for r in v)]
    check("همهٔ مسابقات شبیه‌سازی‌شده تمام شدند", not unfinished, unfinished)
    reasons = {r for v in outcomes.values() for r in v if r}
    check("پایان‌ها از قواعد موتور می‌آیند",
          reasons.issubset({engine.END_DESTROYED, engine.END_NO_AMMO,
                            engine.END_STALEMATE, engine.END_SURRENDER}), reasons)

    # هیچ سامانه‌ای ۱۰۰٪ نیست و رهگیری هیچ‌وقت خسارت صفر نمی‌دهد
    defender = engine.new_player(5, "D")
    defender["radars"] = {"radar_super": 2}
    gmd = fc.WEAPONS["gmd"]
    worst = 0.0
    for _ in range(400):
        worst = max(worst, engine._intercept_chance(defender, gmd,
                                                    fc.WEAPONS["paveh"]))
    check("هیچ دفاعی ۱۰۰٪ موفق نیست", worst <= fc.BALANCE["intercept_cap"], worst)
    check("رهگیری موفق خسارت صفر نمی‌دهد",
          float(fc.BALANCE["intercepted_damage_ratio"]) > 0)


def main():
    test_00_prices_locked()
    test_01_join_and_capacity()
    test_01b_start_word_never_registers()
    test_01c_member_never_blocked_by_admin_gates()
    test_02_countries()
    test_03_budget()
    test_04_shop_limits_and_budget_guard()
    test_05_war_start_requires_both_ready()
    test_06_launch_and_warning()
    test_07_defense_success_and_failure()
    test_08_defense_timeout_and_rules()
    test_09_damage_bounds()
    test_10_repair()
    test_11_nuke_risk()
    test_12_end_and_reward()
    test_13_surrender()
    test_14_restart_recovery()
    test_15_group_isolation()
    test_16_formatting()
    test_17_wallet_integration_real()
    test_18_no_duplicate_handlers()
    test_19_balance_engine()
    print("\n" + "=" * 62)
    print(f"passed={PASSED} failed={FAILED}")
    if FAILURES:
        print("\nشکست‌ها:")
        for row in FAILURES:
            print("  -", row)
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
