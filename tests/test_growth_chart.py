# -*- coding: utf-8 -*-
"""📊 تست دستور «نمودار» — رشد/ریزش اعضا، قالبِ دقیق، و قانونِ دادهٔ ناقص.

قراردادها:

1. اعداد فقط از دفترِ رخدادِ خودِ ربات می‌آیند؛ هیچ عددی حدس زده نمی‌شود.
2. اگر بازه‌ای کامل پوشش داده نشده باشد، داخل همان براکت «تکمیل نشده»
   نوشته می‌شود (نه صفر، نه تخمین).
3. قالبِ خروجی دقیقاً همان چیزی است که خواسته شده، با ارقامِ بولد و با
   entity واقعیِ Bold/Blockquote سروش پلاس.
4. «این هفته» و «این ماه» هرگز عددِ کوچک‌تر از «امروز» نشان نمی‌دهند.
5. دستور فقط برای مالک سراسری، مالک ثبت‌شده و ادمین‌های ثبت‌شده مجاز است
   و این فهرست از دیتابیس خوانده می‌شود، نه از متنِ پیام.
6. متنِ راهنمای «لیست ادمینی» کامل Bold است و «نمودار» داخل نقل‌قول.
"""
import asyncio
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# دیتابیسِ تست، جدا از دیتابیسِ واقعیِ ربات
_TMP = Path(tempfile.mkdtemp(prefix="growth-test-"))
os.environ.setdefault("BOT_PRIVATE_DATA_DIR", str(_TMP))

from modules import group_growth as gg  # noqa: E402
from modules import admin_help_builder as help_builder  # noqa: E402

CHAT = -100777333111
OWNER_ID = 1
GROUP_OWNER_ID = 2
ADMIN_ID = 3
STRANGER_ID = 4

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


def u16(text):
    return len(text.encode("utf-16-le")) // 2


def slice_entity(text, entity):
    raw = text.encode("utf-16-le")
    return raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


def reset_store():
    """پاک‌کردنِ کاملِ دفترِ رخداد بین تست‌ها."""
    from modules import runtime_db
    gg.ensure_storage()
    runtime_db.execute("DELETE FROM membership_events")
    runtime_db.execute("DELETE FROM membership_baseline")
    runtime_db.execute("DELETE FROM membership_state")


def days_ago(days, extra_hours=0):
    """زمانِ مشخص در گذشته به وقتِ تهران."""
    from datetime import timedelta
    from modules.time_utils import now_local
    return (now_local() - timedelta(days=days, hours=extra_hours)).timestamp()


def seed_history(chat_id, *, baseline_ids, joins_by_window, leaves_by_window):
    """کاشتِ دادهٔ آزمایشیِ واقعی در دفترِ رخداد.

    ``joins_by_window`` مثل ``{"day": 3, "week": 20, "month": 90}`` یعنی
    تعدادِ کلِ ورودها در آن بازه (بازهٔ بزرگ‌تر شاملِ کوچک‌تر است).
    اعضایی که بعداً خارج می‌شوند از همان اسنپ‌شاتِ اولیه‌اند تا شمارشِ
    «عضو شده» دقیقاً همان چیزی بماند که انتظار داریم.
    """
    base_ts = days_ago(45)
    leave_month = leaves_by_window.get("month", 0)
    leave_week = leaves_by_window.get("week", 0)
    leave_day = leaves_by_window.get("day", 0)
    leavers = [800000 + index for index in range(leave_month)]
    gg.save_baseline(chat_id, list(baseline_ids) + leavers, base_ts)

    plan = []
    older = max(joins_by_window.get("month", 0) - joins_by_window.get("week", 0), 0)
    mid = max(joins_by_window.get("week", 0) - joins_by_window.get("day", 0), 0)
    day = joins_by_window.get("day", 0)
    counter = 900000
    for _ in range(older):
        counter += 1
        plan.append((days_ago(20), "join", counter))
    for _ in range(mid):
        counter += 1
        plan.append((days_ago(3), "join", counter))
    for _ in range(day):
        counter += 1
        plan.append((days_ago(0, 1), "join", counter))
    older_left = max(leave_month - leave_week, 0)
    mid_left = max(leave_week - leave_day, 0)
    for index in range(older_left):
        plan.append((days_ago(22), "leave", leavers[index]))
    for index in range(older_left, older_left + mid_left):
        plan.append((days_ago(2), "leave", leavers[index]))
    for index in range(older_left + mid_left, leave_month):
        plan.append((days_ago(0, 2), "leave", leavers[index]))
    plan.sort(key=lambda row: row[0])
    for stamp, kind, user_id in plan:
        if kind == "join":
            gg.record_join(chat_id, user_id, stamp, source="test")
        else:
            gg.record_leave(chat_id, user_id, stamp, source="test")
    gg.mark_synced(chat_id, time.time())
    return counter


def find_cells(text):
    """همهٔ اعدادِ داخل براکت‌ها را به ترتیب برمی‌گرداند."""
    cells = []
    for opener, closer in (("『", "』"), ("「", "」")):
        start = 0
        while True:
            left = text.find(opener, start)
            if left < 0:
                break
            right = text.find(closer, left)
            if right < 0:
                break
            cells.append(text[left + 1:right].strip())
            start = right + 1
    return cells


# ---------------------------------------------------------------------------
# ۱) قالب
# ---------------------------------------------------------------------------

def test_format_is_exact():
    print("\n### ۱ قالبِ خروجی دقیقاً همان ساختار خواسته‌شده است")
    reset_store()
    seed_history(CHAT,
                 baseline_ids=list(range(1000, 1500)),
                 joins_by_window={"day": 32, "week": 100, "month": 200},
                 leaves_by_window={"day": 10, "week": 30, "month": 80})
    text, spans = gg.build_report(CHAT)
    cells = find_cells(text)
    check("۵ سلول عددی در قالب هست (۱ فعلی + ۳ رشد + ۳ ریزش = ۷)",
          len(cells) == 7, f"-> {cells}")
    if len(cells) != 7:
        return
    current, join_day, join_week, join_month, leave_day, leave_week, leave_month = cells
    check("«تعداد اعضای فعلی» از دفترِ رخداد خوانده شد",
          current == gg.bold_digits(gg.current_members(CHAT)),
          f"-> {current} / {gg.current_members(CHAT)}")
    check("امروز (عضو شده) = ۳۲", join_day == gg.bold_digits(32), f"-> {join_day}")
    check("این هفته (عضو شده) = ۱۰۰", join_week == gg.bold_digits(100), f"-> {join_week}")
    check("این ماه (عضو شده) = ۲۰۰", join_month == gg.bold_digits(200), f"-> {join_month}")
    check("امروز (ریزش) = ۱۰", leave_day == gg.bold_digits(10), f"-> {leave_day}")
    check("این هفته (ریزش) = ۳۰", leave_week == gg.bold_digits(30), f"-> {leave_week}")
    check("این ماه (ریزش) = ۸۰", leave_month == gg.bold_digits(80), f"-> {leave_month}")

    for line in ("⟳ تعداد اعضای فعلی : 『",
                 "〄 اعضای عضو شده",
                 "〄  ریزش اعضا",
                 " │امروز : 「",
                 " │این هفته :  「",
                 " │این ماه : 「",
                 "│ امروز : 「",
                 "│این هفته :  「",
                 "│این ماه : 「",
                 "« نتیجه گیری : "):
        check(f"خطِ «{line.strip()}» در قالب هست", line in text)

    check("ارقام با استایلِ بولدِ ریاضی نوشته شده‌اند (𝟬..𝟵)",
          all(any("𝟬" <= char <= "𝟵" for char in cell) for cell in cells
              if cell != gg.INCOMPLETE),
          f"-> {cells}")
    check("هیچ رقمِ لاتینِ ساده داخل براکت‌ها نیست",
          not any(any("0" <= char <= "9" for char in cell) for cell in cells))
    check("نتیجه‌گیری «پیشرفت» است چون رشد > ریزش",
          gg.VERDICT_GROWTH in text, f"-> {text[-80:]!r}")
    check("دو ستون ریزش و رشد از هم جدا شده‌اند", text.count("〄") == 2)


def test_entities_are_real():
    print("\n### ۲ Bold و نقل‌قول با entity واقعی سروش ساخته می‌شود")
    text, spans = gg.build_report(CHAT)
    bold = [(o, l) for o, l, kind in spans if kind == gg.BOLD]
    quotes = [(o, l) for o, l, kind in spans if kind == gg.QUOTE]

    def covered(needle, ranges):
        index = text.index(needle)
        start = u16(text[:index])
        end = start + u16(needle)
        return any(o <= start and o + l >= end for o, l in ranges)

    for needle in ("⟳ تعداد اعضای فعلی", "〄 اعضای عضو شده",
                   "〄  ریزش اعضا", "« نتیجه گیری"):
        check(f"«{needle}» بولد است", covered(needle, bold))
    verdict_line = text[text.index("« نتیجه گیری"):].rstrip("\n")
    check("کلِ خطِ نتیجه‌گیری بولد است",
          covered(verdict_line, bold), f"-> {verdict_line!r}")
    check("همهٔ سلول‌های عددی داخل نقل‌قول‌اند",
          len(quotes) >= 7, f"-> {quotes}")
    check("هیچ بازه‌ای از متن بیرون نمی‌زند",
          all(0 <= o and o + l <= u16(text) for o, l in bold + quotes))


def test_incomplete_windows():
    print("\n### ۳ بازهٔ ناقص «تکمیل نشده» می‌شود، نه عددِ جعلی")
    reset_store()
    # نصبِ ربات از هشت روز پیش: امروز و این هفته کامل، این ماه نه.
    gg.save_baseline(CHAT, list(range(2000, 2600)), days_ago(8))
    gg.record_join(CHAT, 5001, days_ago(0, 1))
    gg.record_join(CHAT, 5002, days_ago(2))
    gg.record_join(CHAT, 5003, days_ago(2, 3))
    gg.record_leave(CHAT, 2500, days_ago(0, 2))
    gg.record_leave(CHAT, 2501, days_ago(1))
    text, _ = gg.build_report(CHAT)
    cells = find_cells(text)
    check("۷ سلول عددی/متنی داریم", len(cells) == 7, f"-> {cells}")
    current, join_day, join_week, join_month, leave_day, leave_week, leave_month = cells
    check("تعداد فعلی = ۶۰۰ + ۳ ورود − ۲ خروج",
          current == gg.bold_digits(601), f"-> {current}")
    check("«امروز» واقعی است (۱ عضو تازه)",
          join_day == gg.bold_digits(1), f"-> {join_day}")
    check("«این هفته» = ۳", join_week == gg.bold_digits(3), f"-> {join_week}")
    check("«این ماه» تکمیل نشده است", join_month == gg.INCOMPLETE, f"-> {join_month}")
    check("ریزشِ امروز = ۱", leave_day == gg.bold_digits(1), f"-> {leave_day}")
    check("ریزشِ هفته = ۲", leave_week == gg.bold_digits(2), f"-> {leave_week}")
    check("ریزشِ ماه تکمیل نشده است", leave_month == gg.INCOMPLETE, f"-> {leave_month}")
    check("«تکمیل نشده» داخل همان براکت‌ها نشسته است",
          "「 تکمیل نشده 」" in text)
    check("هیچ صفرِ جعلی برای بازه‌های ناقص نوشته نشده",
          gg.bold_digits(0) not in cells)
    check("نتیجه‌گیری با بازه‌های موجود محاسبه شد (رشد ۳ > ریزش ۲)",
          gg.VERDICT_GROWTH in text, f"-> {text[-70:]!r}")

    # نصبِ همین حالا: حتی «امروز» هم بازهٔ کاملی نیست
    reset_store()
    gg.save_baseline(CHAT, list(range(3000, 3100)), time.time())
    fresh, _ = gg.build_report(CHAT)
    fresh_cells = find_cells(fresh)
    check("در روزِ نصب، «امروز» هم تکمیل نشده است",
          fresh_cells[1] == gg.INCOMPLETE, f"-> {fresh_cells[1]}")
    check("تعداد فعلی حتی در روزِ نصب واقعی است",
          fresh_cells[0] == gg.bold_digits(100), f"-> {fresh_cells[0]}")
    check("وقتی هیچ بازه‌ای کامل نیست، نتیجه‌گیری نامشخص است",
          gg.VERDICT_UNKNOWN in fresh)


def test_equal_or_missing_gives_unknown():
    print("\n### ۴ نتیجه‌گیریِ نامشخص وقتی داده کافی نیست یا برابر است")
    check("بدونِ هیچ داده‌ای → «داده کافی برای نتیجه‌گیری وجود ندارد»",
          gg.evaluate({}, {}, {}, {}) == gg.VERDICT_UNKNOWN)
    check("برابر بودن رشد و ریزش → نامشخص",
          gg.evaluate({"day": 5, "week": None, "month": None},
                      {"day": 5, "week": None, "month": None},
                      {}, {}) == gg.VERDICT_UNKNOWN)
    check("رشد بیشتر → پیشرفت",
          gg.evaluate({"day": 6}, {"day": 5}, {}, {}) == gg.VERDICT_GROWTH)
    check("ریزش بیشتر → افت",
          gg.evaluate({"day": 4}, {"day": 9}, {}, {}) == gg.VERDICT_DECLINE)
    check("سه بازه با هم جمع زده می‌شود (۱۰۰ رشد در برابر ۸۲ ریزش)",
          gg.evaluate({"day": 10, "week": 30, "month": 60},
                      {"day": 11, "week": 31, "month": 40},
                      {}, {}) == gg.VERDICT_GROWTH)
    check("مجموعِ ریزشِ بیشتر → افت",
          gg.evaluate({"day": 10, "week": 30, "month": 60},
                      {"day": 11, "week": 31, "month": 90},
                      {}, {}) == gg.VERDICT_DECLINE)
    reset_store()
    text, _ = gg.build_report(CHAT)
    check("گزارشِ گروهِ بدونِ داده، عدد جعلی ندارد",
          gg.bold_digits(0) not in text, f"-> {find_cells(text)}")
    check("گزارشِ بدونِ داده، نتیجه‌گیریِ نامشخص دارد",
          gg.VERDICT_UNKNOWN in text)
    check("«تعداد اعضای فعلی» هم در نبودِ داده «تکمیل نشده» است",
          find_cells(text)[0] == gg.INCOMPLETE, f"-> {find_cells(text)}")


def test_monotonic_windows():
    print("\n### ۵ «این هفته»/«این ماه» هرگز کوچک‌تر از «امروز» نیست")
    reset_store()
    # امروز ۵ ورود، ولی در بقیهٔ هفته هیچ ورودی نیست (دادهٔ ناسازگارِ ساختگی)
    gg.save_baseline(CHAT, list(range(3000, 3100)), days_ago(10))
    for user in range(7001, 7006):
        gg.record_join(CHAT, user, days_ago(0, 1))
    text, _ = gg.build_report(CHAT)
    cells = find_cells(text)
    day, week, month = cells[1], cells[2], cells[3]
    check("امروز = ۵", day == gg.bold_digits(5), f"-> {day}")
    check("این هفته حداقل برابرِ امروز است",
          week == gg.bold_digits(5), f"-> {week}")
    check("«این ماه» چون از نصب قدیمی‌تر است، تکمیل نشده می‌ماند",
          month == gg.INCOMPLETE, f"-> {month}")


# ---------------------------------------------------------------------------
# ۶) دفترِ رخداد
# ---------------------------------------------------------------------------

def test_never_blank_current_members():
    print("\n### ۳.۵ «تعداد اعضای فعلی» هرگز خالی نمی‌ماند")
    reset_store()
    # فهرست اعضا در دسترس نبوده؛ فقط عددِ اعلامیِ سروش را داریم.
    gg.set_server_count(CHAT, 532)
    text, _ = gg.build_report(CHAT, title="گروه نمونه")
    cells = find_cells(text)
    check("تعداد اعضای فعلی از عددِ سروش خوانده شد",
          cells[0] == gg.bold_digits(532), f"-> {cells[0]}")
    check("بازه‌های بدونِ داده صادقانه تکمیل‌نشده‌اند",
          cells[1:] == [gg.INCOMPLETE] * 6, f"-> {cells[1:]}")
    check("نتیجه‌گیری با دادهٔ ناکافی، نامشخص است", gg.VERDICT_UNKNOWN in text)
    check("نامِ گروه در گزارش می‌آید", "« گروه نمونه »" in text)
    check("سرتیترِ گزارش هست", "📊 نمودار رشد و ریزش گروه" in text)
    check("عددِ سروش هیچ بازه‌ای را «کامل» نمی‌کند",
          all(gg.joins(CHAT, key) is None for key in ("day", "week", "month")))
    check("ریزش هم هیچ عدد جعلی ندارد",
          all(gg.leaves(CHAT, key) is None for key in ("day", "week", "month")))

    # با دادهٔ واقعی، دفترِ رخداد اولویت دارد
    gg.save_baseline(CHAT, list(range(4000, 4500)), days_ago(10))
    gg.record_join(CHAT, 7777, days_ago(0, 1))
    check("با وجودِ دفترِ رخداد، عدد از دفتر خوانده می‌شود",
          gg.current_members(CHAT) == 501, f"-> {gg.current_members(CHAT)}")

    # هیچ داده‌ای نباشد → تکمیل نشده (نه صفر)
    reset_store()
    text, _ = gg.build_report(CHAT)
    check("بدونِ هیچ داده‌ای، عددِ فعلی «تکمیل نشده» است",
          find_cells(text)[0] == gg.INCOMPLETE, f"-> {find_cells(text)[0]}")
    check("خطِ تحلیلی در نبودِ داده ساخته نمی‌شود",
          gg.build_insight(CHAT) is None)


def test_midday_start_is_honest():
    print("\n### ۳.۶ شروعِ پایش در میانهٔ روز «امروز» را کامل حساب نمی‌کند")
    reset_store()
    # پایش همین حالا شروع شده (بعد از نیمه‌شب) و یک ورود ثبت شده است.
    gg.save_baseline(CHAT, list(range(6000, 6100)), time.time())
    gg.record_join(CHAT, 6101, time.time())
    text, _ = gg.build_report(CHAT)
    cells = find_cells(text)
    check("«تعداد اعضای فعلی» واقعی است",
          cells[0] == gg.bold_digits(101), f"-> {cells[0]}")
    check("«امروز» تکمیل نشده می‌ماند (ورودهای صبح ثبت نشده‌اند)",
          cells[1] == gg.INCOMPLETE, f"-> {cells[1]}")
    check("«این هفته» هم تکمیل نشده می‌ماند",
          cells[2] == gg.INCOMPLETE, f"-> {cells[2]}")
    check("هیچ عددی از بازهٔ ناقص ساخته نشد",
          gg.joins(CHAT, "day") is None and gg.leaves(CHAT, "day") is None)


def test_event_ledger():
    print("\n### ۶ دفترِ رخداد ورود و خروج را درست ثبت می‌کند")
    reset_store()
    check("ورودِ تازه ثبت شد", gg.record_join(CHAT, 101))
    check("ورودِ تکراری ثبت نمی‌شود", not gg.record_join(CHAT, 101))
    check("کاربر عضو است", gg.is_member(CHAT, 101))
    check("ترکِ عضو ثبت شد", gg.record_leave(CHAT, 101))
    check("ترکِ دوباره ثبت نمی‌شود", not gg.record_leave(CHAT, 101))
    check("کاربر دیگر عضو نیست", not gg.is_member(CHAT, 101))
    check("ورودِ مجدد بعد از ترک ثبت می‌شود", gg.record_join(CHAT, 101))
    check("شمارشِ اعضای فعلی = ۱", gg.current_members(CHAT) == 1)
    check("شناسه‌های خالی نادیده گرفته می‌شوند",
          not gg.record_join(CHAT, 0) and not gg.record_join(CHAT, None))
    # فهرست زنده = همگام‌سازی
    result = gg.sync_members(CHAT, [101, 102, 103])
    check("اعضای تازه در همگام‌سازی ثبت شدند",
          result["joined"] in (0, 2), f"-> {result}")
    check("اعضای فعلی همگام شدند",
          set(gg.member_ids(CHAT)) == {"101", "102", "103"},
          f"-> {gg.member_ids(CHAT)}")
    result = gg.sync_members(CHAT, [101, 103])
    check("عضوِ حذف‌شده ریزش ثبت کرد", result["left"] == 1, f"-> {result}")
    check("اعضای فعلی به‌روز شد",
          set(gg.member_ids(CHAT)) == {"101", "103"}, f"-> {gg.member_ids(CHAT)}")


def test_first_snapshot_is_not_a_join():
    print("\n### ۷ اعضای قدیمی «ورودِ امروز» حساب نمی‌شوند")
    reset_store()
    result = gg.sync_members(CHAT, list(range(4000, 4100)))
    check("بارِ اول اسنپ‌شات ساخته شد", result["baseline"] is True, f"-> {result}")
    check("هیچ ورودی به‌عنوانِ عضویتِ امروز ثبت نشد",
          result["joined"] == 0, f"-> {result}")
    check("۹۹ نفر عضو حساب می‌شوند",
          gg.current_members(CHAT) == 100, f"-> {gg.current_members(CHAT)}")
    text, _ = gg.build_report(CHAT)
    cells = find_cells(text)
    check("«امروز» صفرِ واقعی نیست بلکه داده ندارد",
          cells[1] == gg.INCOMPLETE, f"-> {cells[1]}")
    check("«این ماه» تکمیل نشده", cells[3] == gg.INCOMPLETE, f"-> {cells[3]}")
    check("تعداد فعلی درست گزارش شد",
          cells[0] == gg.bold_digits(100), f"-> {cells[0]}")


# ---------------------------------------------------------------------------
# ۸) دسترسی و راهنما (از طریق هندلر واقعی)
# ---------------------------------------------------------------------------

class Msg:
    def __init__(self, text, message_id=1):
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
    def __init__(self, text, user_id, chat_id=CHAT, message_id=1):
        self.message = Msg(text, message_id)
        self.chat_id = chat_id
        self.is_private = False
        self.sender_id = user_id
        self.sender = types.SimpleNamespace(
            id=user_id, first_name=f"u{user_id}", last_name=None,
            username=None)
        self.replies = []
        self.entities = []

    async def get_chat(self):
        return types.SimpleNamespace(id=self.chat_id, title="گروه تست")

    async def get_sender(self):
        return self.sender

    async def reply(self, text, formatting_entities=None, **kwargs):
        self.replies.append(text)
        self.entities.append(list(formatting_entities or []))

    async def respond(self, text, **kwargs):
        self.replies.append(text)
        self.entities.append([])


def patch_client(bot, members):
    """کلاینتِ تست: فهرست اعضا را بدونِ شبکه برمی‌گرداند."""
    async def get_input_entity(chat_id):
        return types.SimpleNamespace(id=chat_id)

    async def get_participants(entity, *args, **kwargs):
        return [types.SimpleNamespace(id=user) for user in members]

    bot.client.get_input_entity = get_input_entity
    bot.client.get_participants = get_participants
    return bot


def test_permissions_and_help():
    print("\n### ۸ فقط مالک/ادمینِ ثبت‌شده و راهنمای «لیست ادمینی»")
    from test_court_join_all_users import build_handler_bot
    import handlers.message_handler as message_handler
    from modules import admin_storage, group_storage, owner_check

    reset_store()

    async def scenario():
        bot = patch_client(build_handler_bot(set(), game_answer=False),
                           list(range(5000, 5040)))
        owner = owner_check.get_owner()["user_id"]

        # ۱) مالک سراسری مجاز است
        event = Ev("نمودار", owner, message_id=101)
        await message_handler.handle_new_message(bot, event)
        check("مالک سراسری پاسخ گرفت", bool(event.replies), f"-> {event.replies}")
        check("گزارشِ نمودار برای مالک ارسال شد",
              event.replies and "نمودار" not in event.replies[0] == False
              or any("نتیجه گیری" in r for r in event.replies),
              f"-> {event.replies[:1]}")
        check("قالبِ گزارش برای مالک درست است",
              any("⟳ تعداد اعضای فعلی" in r for r in event.replies),
              f"-> {event.replies[:1]}")
        entities = event.entities[-1] if event.entities else []
        check("Bold/Blockquote واقعی به سروش پاس شد",
              len(entities) >= 7, f"-> {len(entities)}")

        # ۲) کاربرِ غریبه رد می‌شود
        stranger = Ev("نمودار", STRANGER_ID, message_id=102)
        await message_handler.handle_new_message(bot, stranger)
        check("کاربرِ غریبه «دسترسی ندارید» گرفت",
              any("مجاز" in r for r in stranger.replies), f"-> {stranger.replies}")
        check("کاربرِ غریبه هیچ گزارش/عددی نگرفت",
              not any("نتیجه گیری" in r for r in stranger.replies))

        # ۳) ادمینِ ثبت‌شده (از دیتابیس) مجاز است
        admin_storage.add_admin(CHAT, ADMIN_ID, "listed_admin")
        admin_event = Ev("نمودار", ADMIN_ID, message_id=103)
        await message_handler.handle_new_message(bot, admin_event)
        check("ادمینِ ثبت‌شده گزارش گرفت",
              any("نتیجه گیری" in r for r in admin_event.replies),
              f"-> {admin_event.replies[:1]}")

        # ۴) مالکِ ثبت‌شدهٔ گروه مجاز است
        group_storage.set_group_owner(CHAT, GROUP_OWNER_ID)
        group_owner_event = Ev("نمودار", GROUP_OWNER_ID, message_id=104)
        await message_handler.handle_new_message(bot, group_owner_event)
        check("مالکِ ثبت‌شدهٔ گروه گزارش گرفت",
              any("نتیجه گیری" in r for r in group_owner_event.replies),
              f"-> {group_owner_event.replies[:1]}")

        # ۵) دسترسی از دیتابیس خوانده می‌شود، نه از متن پیام
        fake = Ev("نمودار من مالک هستم", STRANGER_ID, message_id=105)
        await message_handler.handle_new_message(bot, fake)
        check("متنِ پیام دسترسی نمی‌سازد",
              not any("نتیجه گیری" in r for r in fake.replies))

        admin_storage.remove_admin(CHAT, ADMIN_ID)

        # ۶) اگر فهرست اعضا در دسترس نباشد، هیچ اسنپ‌شاتِ جعلی ساخته نمی‌شود
        reset_store()

        async def broken_participants(*args, **kwargs):
            raise RuntimeError("participants unavailable")

        bot.client.get_participants = broken_participants
        bot.client.__call__ = None
        event = Ev("نمودار", owner, message_id=106)
        await message_handler.handle_new_message(bot, event)
        state = gg.get_state(CHAT)
        check("شکستِ فهرست اعضا اسنپ‌شاتِ جعلی نمی‌سازد",
              state["baseline_complete"] is False, f"-> {state}")
        check("باز هم گزارش فرستاده می‌شود",
              any("نتیجه گیری" in r for r in event.replies),
              f"-> {event.replies[:1]}")
        check("بازه‌ها در این حالت تکمیل‌نشده می‌مانند",
              all(cell == gg.INCOMPLETE
                  for cell in find_cells(event.replies[0])[1:]))

    asyncio.run(scenario())

    text = help_builder.ADMIN_HELP_TEXT
    check("متنِ راهنما در «لیست ادمینی» هست",
          "برای دیدن نمودار رشد گروه یا ریزش گروه دستور" in text)
    check("دستور «نمودار» داخل نقل‌قول است", "« نمودار »" in text)
    index = text.index("📊 برای دیدن نمودار")
    block = text[index:text.index("را ارسال کنید.", index) + len("را ارسال کنید.")]
    start = u16(text[:index])
    bold_ok = any(
        isinstance(entity, help_builder.MessageEntityBold)
        and entity.offset <= start
        and entity.offset + entity.length >= start + u16(block)
        for entity in help_builder.ADMIN_HELP_ENTITIES)
    quote_ok = any(
        isinstance(entity, help_builder.MessageEntityBlockquote)
        and entity.offset >= start
        and entity.offset + entity.length <= start + u16(block)
        for entity in help_builder.ADMIN_HELP_ENTITIES)
    check("کلِ متنِ راهنما بولد است", bold_ok)
    check("«نمودار» داخل نقل‌قول است", quote_ok)


def test_dispatch_lane():
    print("\n### ۹ «نمودار» روی خطِ دستورهای مدیریتی می‌ماند")
    from modules import group_dispatch
    priority, kind = group_dispatch.classify_priority("نمودار")
    check("«نمودار» خطِ ادمین است",
          kind == group_dispatch.LANE_ADMIN
          and priority == group_dispatch.PRIORITY_ADMIN,
          f"-> {(priority, kind)}")
    priority, kind = group_dispatch.classify_priority("نمودار گروه")
    check("«نمودار» با پارامتر اضافه، دستور نیست",
          kind == group_dispatch.LANE_NORMAL, f"-> {(priority, kind)}")


def test_recorder_wiring():
    print("\n### ۱۰ شنوندهٔ رویداد و ثبتِ خودکار")
    from modules import group_growth_events

    async def scenario():
        class FakeClient:
            def __init__(self):
                self.handlers = []

            def on(self, event_filter):
                def decorator(func):
                    self.handlers.append(func)
                    return func
                return decorator

        bot = types.SimpleNamespace(client=FakeClient(), logger=None)
        first = group_growth_events.register(bot)
        second = group_growth_events.register(bot)
        check("ثبتِ شنونده موفق بود", first is True)
        check("ثبتِ دوباره تکرار نمی‌شود", second is False)
        check("یک شنونده نصب شد", len(bot.client.handlers) == 1)

        reset_store()
        event = types.SimpleNamespace(
            chat_id=CHAT, user_ids=[88001], user_joined=True,
            user_added=False, user_left=False, user_kicked=False)
        await group_growth_events.handle_chat_action(bot, event)
        check("ورود از رویداد ثبت شد", gg.is_member(CHAT, 88001))
        leave = types.SimpleNamespace(
            chat_id=CHAT, user_ids=[88001], user_joined=False,
            user_added=False, user_left=True, user_kicked=False)
        await group_growth_events.handle_chat_action(bot, leave)
        check("خروج از رویداد ثبت شد", not gg.is_member(CHAT, 88001))
        chatter = types.SimpleNamespace(
            chat_id=CHAT, user_ids=[88002], user_joined=False,
            user_added=False, user_left=False, user_kicked=False)
        await group_growth_events.handle_chat_action(bot, chatter)
        check("رویدادهای بی‌ربط نادیده گرفته می‌شوند",
              gg.current_members(CHAT) == 0)

    asyncio.run(scenario())


def test_no_fake_numbers_in_source():
    print("\n### ۱۱ هیچ عددِ جعلی/تخمینی در مسیر گزارش نیست")
    source = (ROOT / "modules" / "group_growth.py").read_text(encoding="utf-8")
    check("ماژول هیچ تماسِ شبکه‌ای ندارد",
          "urllib" not in source and "requests" not in source
          and "http://" not in source and "https://" not in source)
    check("هیچ ضریب/تخمینی در محاسبه نیست",
          not any(token in source for token in ("estimate", "approx", "random.")))
    check("متنِ «تکمیل نشده» فقط یک منبع دارد",
          source.count('INCOMPLETE = "تکمیل نشده"') == 1)


def main():
    print("📊 تست دستور «نمودار» (رشد/ریزش اعضا)")
    tests = [
        test_format_is_exact,
        test_entities_are_real,
        test_incomplete_windows,
        test_equal_or_missing_gives_unknown,
        test_monotonic_windows,
        test_never_blank_current_members,
        test_midday_start_is_honest,
        test_event_ledger,
        test_first_snapshot_is_not_a_join,
        test_permissions_and_help,
        test_dispatch_lane,
        test_recorder_wiring,
        test_no_fake_numbers_in_source,
    ]
    for test in tests:
        try:
            test()
        except Exception as error:
            import traceback
            traceback.print_exc()
            check(f"{test.__name__} بدونِ خطا اجرا شد", False, f"-> {error!r}")
    print("\n" + "=" * 58)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 58)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
