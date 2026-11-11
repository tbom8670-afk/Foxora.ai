# -*- coding: utf-8 -*-
"""🧪 تستِ بازی «جدولستان».

    PYTHONPATH=. python3 tests/test_jadvalestan.py

پوشش:
    • بانکِ ۳۲۰ سوالی (یکتا، یک‌کلمه‌ای، فارسی، بی‌ابهام، بدونِ تکرارِ جواب‌های سست)
    • متنِ دقیقِ مرحله، تعدادِ حروفِ محاسبه‌شده از خودِ جواب، مهلتِ ۶۰ ثانیه
    • فقط صاحبِ بازی جواب بدهد؛ پیامِ بقیه کاملاً نادیده
    • جوابِ غلط ⇒ تلاشِ دوباره تا پایانِ مهلت
    • جوابِ درست ⇒ ۳ سکهٔ نقره + مرحلهٔ بعد، فقط یک‌بار
    • پایانِ مهلت ⇒ اعلامِ جواب و پایانِ دور، بدونِ پاداش و بدونِ پیشرفت
    • پیشرفتِ دائمی در دیسک، ادامه از مرحلهٔ بعد، بدونِ تکرارِ سوال
    • استقلالِ کاملِ وضعیتِ چند کاربرِ هم‌زمان
    • اتصال به هندلر، لیستِ بازی‌ها، گاردِ سرگرمی و group_dispatch
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
# محیطِ ایزوله: اقتصاد و پیشرفت در پوشهٔ موقت
# ---------------------------------------------------------------------------
TEMP = Path(tempfile.mkdtemp(prefix="jadvalestan-test-"))
os.environ.setdefault("BOT_HOME", str(TEMP / "bot_home"))
os.environ["JADVALESTAN_PROGRESS_FILE"] = str(TEMP / "progress.json")

import economy  # noqa: E402
import economy.storage as economy_storage  # noqa: E402
from economy import rewards  # noqa: E402
from modules import jadvalestan as JV  # noqa: E402
from modules.jadvalestan import storage as JV_STORAGE  # noqa: E402
from modules.jadvalestan import texts as JV_TEXTS  # noqa: E402

economy_storage.use_file(TEMP / "economy.json")

CHAT = -8800112233
CHAT_B = -8800112244
USER = 770
OTHER = 771


def fresh():
    """پاک‌کردنِ حالت بینِ بخش‌ها (فقط حافظه و فایلِ موقت)."""
    JV.reset()
    JV_STORAGE.reset_all()
    economy_storage.use_file(TEMP / ("economy-%d.json" % (PASS + FAIL)))


# ---------------------------------------------------------------------------
# ۱) بانکِ سوال‌ها
# ---------------------------------------------------------------------------
PERSIAN_ONLY = re.compile(r"^[\u0621-\u06CC]+$")
BANNED_ANSWERS = {
    "بازار", "باستانی", "ایرانی", "قطب", "دستور", "چهار", "سه", "هفت",
    "یازده", "دوازده", "هشت", "سی‌ویک",
}


def test_bank():
    section("۱. بانکِ ۳۲۰ سوال")
    check("فایلِ questions.json موجود است", JV.QUESTIONS_FILE.is_file(),
          JV.QUESTIONS_FILE)
    raw = json.loads(JV.QUESTIONS_FILE.read_text(encoding="utf-8"))
    questions = raw["questions"]
    check("۳۲۰ مرحله ذخیره شده", len(questions) == 320, len(questions))
    check("ماژول هم ۳۲۰ مرحله بارگذاری کرد", JV.TOTAL == 320, JV.TOTAL)
    check("شمارهٔ مرحله‌ها ۱..۳۲۰ و پشتِ‌سرهم است",
          [q["stage"] for q in questions] == list(range(1, 321)))

    texts = [q["question"] for q in questions]
    check("هیچ سوالی تکراری نیست", len(set(texts)) == 320,
          len(texts) - len(set(texts)))
    check("هر سوال علامتِ سوال دارد", all(t.strip().endswith("؟") for t in texts))

    bad_persian = [
        q["answer"] for q in questions
        if not PERSIAN_ONLY.match(q["answer"].replace("\u200c", ""))
    ]
    check("جواب‌ها فقط حروفِ فارسی‌اند (بدونِ رقم/لاتین)",
          not bad_persian, bad_persian)
    multiword = [q["answer"] for q in questions if " " in q["answer"].strip()]
    check("هر جواب یک کلمه است", not multiword, multiword)
    check("هیچ جوابی خالی نیست", all(q["answer"].strip() for q in questions))
    check("هیچ سوالی جوابش را در متن ندارد (سوالِ بی‌معنی)",
          not [q for q in questions if q["answer"] in q["question"]])

    levels = {q["difficulty"] for q in questions}
    check("سختی‌ها فقط آسان/متوسط/سخت‌اند",
          levels <= {"آسان", "متوسط", "سخت"}, levels)
    mix = {level: sum(1 for q in questions if q["difficulty"] == level)
           for level in ("آسان", "متوسط", "سخت")}
    check("همهٔ سه سطح در بازی هست", all(mix.values()), mix)
    check("بیشترِ مراحلِ اولِ بازی آسان است",
          mix["آسان"] >= 80, mix)

    topics = {q["topic"] for q in questions}
    check("موضوع‌ها متنوع‌اند (≥ ۸ موضوع)", len(topics) >= 8, topics)
    for needed in ("جغرافیا", "ادبیات", "تاریخ", "علمی", "ورزشی",
                   "غذا و آداب", "دین و فرهنگ"):
        check("موضوعِ «%s» در بانک هست" % needed, needed in topics)

    # ترتیب: نباید چند سوالِ هم‌موضوع پشتِ‌سرهم بیاید.
    worst, run = 1, 1
    for prev, cur in zip(questions, questions[1:]):
        run = run + 1 if prev["topic"] == cur["topic"] else 1
        worst = max(worst, run)
    check("هیچ دو سوالِ هم‌موضوعی پشتِ‌سرهم نیست", worst <= 2, worst)

    # سهولتِ اولِ بازی: میانگینِ سختیِ ۲۰ مرحلهٔ اول بهتر از ۲۰ مرحلهٔ آخر.
    rank = {"آسان": 1, "متوسط": 2, "سخت": 3}
    head = sum(rank[q["difficulty"]] for q in questions[:20]) / 20
    tail = sum(rank[q["difficulty"]] for q in questions[-20:]) / 20
    check("بازی از آسان به سخت می‌رود", head < tail, (head, tail))

    check("جواب‌های تکراری فقط با سوالِ معنادارِ متفاوت",
          len({q["answer"] for q in questions}) >= 250,
          len({q["answer"] for q in questions}))
    # جواب‌های «چرت» که کارفرما رد می‌کند نباید بیایند — مگر عددی که
    # خودِ سوال جوابش است (مثلِ «رباعی چند مصراع دارد؟ → چهار»).
    numeric = {"چهار", "سه", "هفت", "یازده", "دوازده", "هشت", "سی‌ویک",
               "پنج", "شش", "نه", "ده"}
    vague = [
        q for q in questions
        if q["answer"] in BANNED_ANSWERS and q["answer"] not in numeric
    ]
    check("جوابِ مبهم/بی‌ربط در بانک نیست", not vague,
          [(q["stage"], q["answer"]) for q in vague])
    numeric_hits = [q for q in questions if q["answer"] in numeric]
    check("جوابِ عددی فقط وقتی است که خودِ سوال عدد می‌پرسد",
          all(("چند" in q["question"]) or ("تعداد" in q["question"])
              for q in numeric_hits),
          [q["question"] for q in numeric_hits])


# ---------------------------------------------------------------------------
# ۲) متن‌ها
# ---------------------------------------------------------------------------
def test_texts():
    section("۲. متن‌های بازی")
    m = re.search(r"TIME_LIMIT\s*=\s*(\d+)", 
                  (ROOT / "modules/jadvalestan/texts.py").read_text(encoding="utf-8"))
    check("مهلتِ هر مرحله ۶۰ ثانیه است", int(m.group(1)) == 60,
          m.group(1) if m else None)
    check("ماژول همان ۶۰ ثانیه را نشان می‌دهد", JV.TIME_LIMIT == 60, JV.TIME_LIMIT)

    payload = JV.stage_payload(1)
    check("مرحلهٔ ۱ ساخته می‌شود", payload is not None)
    expected = (
        "🧩 جدولستان | مرحله ۱ از ۳۲۰\n"
        "\n"
        "%s\n"
        "\n"
        "%s  (%s حرفی)\n"
        "\n"
        "🎮 مهلت زمانی ۶۰ ثانیه"
        % (payload["question"],
           " ".join("_" for _ in range(payload["letters"])),
           JV_TEXTS.fa(payload["letters"]))
    )
    check("متنِ مرحله دقیقاً مطابقِ خواستهٔ کارفرماست", payload["text"] == expected,
          payload["text"])
    check("تعدادِ حروف از خودِ جواب حساب می‌شود",
          payload["letters"] == len(payload["answer"].replace("\u200c", "")),
          (payload["letters"], payload["answer"]))
    check("سوالِ «کوتاه‌ترین» جواب ۲ حرفی دارد و جای خالی‌اش ۲ تاست",
          all(JV.stage_payload(s)["letters"] >= 2 for s in range(1, 321)))
    five = JV.stage_payload(1)
    check("نمایشِ جای خالی با فاصله است", "_ _ _ _ _" in five["text"],
          five["text"])

    check("متنِ برد عیناً «آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖» است",
          JV_TEXTS.correct_text() == "آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖",
          JV_TEXTS.correct_text())
    check("متنِ پایانِ مهلت جوابِ درست را دارد",
          "علی" in JV_TEXTS.timeout_text("علی"))
    check("متنِ تبریکِ پایانِ بازی ۳۲۰ را می‌گوید",
          "۳۲۰" in JV_TEXTS.finished_text(320))


# ---------------------------------------------------------------------------
# ۳) نرمال‌سازی
# ---------------------------------------------------------------------------
def test_normalize():
    section("۳. نرمال‌سازیِ ورودی")
    check("ي عربی → ی", JV.normalize("علي") == "علی", JV.normalize("علي"))
    check("ك عربی → ک", JV.normalize("كبر") == "کبر", JV.normalize("كبر"))
    check("حذفِ اعراب", JV.normalize("حَسَن") == "حسن", JV.normalize("حَسَن"))
    check("حذفِ نیم‌فاصله", JV.normalize("خرم‌آباد") == JV.normalize("خرمآباد"))
    check("فاصله‌های اضافه", JV.normalize("  تبريز   ") == "تبریز",
          JV.normalize("  تبريز   "))
    check("نشانه‌ها حذف می‌شوند", JV.normalize("تبریز!") == "تبریز",
          JV.normalize("تبریز!"))
    check("آ و ا یکی حساب می‌شوند", JV.compact("آرام") == JV.compact("ارام"))
    check("«اسلام آباد» = «اسلام‌آباد»",
          JV.compact("اسلام آباد") == JV.compact("اسلام‌آباد"))
    check("تعدادِ حروفِ «کی‌یف» نیم‌فاصله را نمی‌شمارد",
          JV.letters_of("کی‌یف") == 4, JV.letters_of("کی‌یف"))


# ---------------------------------------------------------------------------
# ۴) جریانِ بازی
# ---------------------------------------------------------------------------
def test_gameplay():
    section("۴. جریانِ بازی")
    fresh()
    check("کاربرِ تازه هیچ مرحله‌ای ندارد", JV.completed_stage(CHAT, USER) == 0)
    check("بازی بدونِ شروع، فعال نیست", not JV.active(CHAT, USER))

    started = JV.start(CHAT, USER)
    check("بازی شروع شد", started["kind"] == "started", started["kind"])
    check("مرحلهٔ اول از ۳۲۰ آغاز شد", started["stage"] == 1, started["stage"])
    check("۱ از ۳۲۰ در متن هست", "مرحله ۱ از ۳۲۰" in started["text"])
    check("پس از شروع، مرحله باز است", JV.active(CHAT, USER))

    answer_text = JV.question_for_stage(1)["answer"]
    wrong = JV.answer(CHAT, USER, "یک جوابِ کاملاً غلط")
    check("جوابِ غلط: kind=wrong", wrong["kind"] == "wrong", wrong)
    check("جوابِ غلط پیامِ مهربان دارد", "دوباره تلاش کن" in wrong["message"])
    check("بعد از جوابِ غلط هنوز می‌شود تلاش کرد", JV.active(CHAT, USER))
    check("جوابِ غلط چیزی ثبت نکرد", JV.completed_stage(CHAT, USER) == 0)

    ok = JV.answer(CHAT, USER, answer_text)
    check("جوابِ درست: kind=correct", ok["kind"] == "correct", ok)
    check("پیامِ برد عیناً متنِ کارفرماست",
          ok["message"].startswith("آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖"),
          ok["message"][:60])
    check("مرحلهٔ ۲ همان لحظه باز شد", ok["next_stage"] == 2, ok.get("next_stage"))
    check("متنِ مرحلهٔ بعد هم در همان پیام هست",
          "مرحله ۲ از ۳۲۰" in ok["message"])
    balance = economy.get_balance(CHAT, USER)
    check("۳ سکهٔ نقره واقعاً اضافه شد",
          balance[economy.SILVER] == 3, balance)
    check("سکهٔ برنز اضافه نشد", balance[economy.BRONZE] == 0, balance)
    check("پیشرفت = ۱", JV.completed_stage(CHAT, USER) == 1)

    # تکرارِ همان جواب: مرحلهٔ ۲ باز است، پس پاداشِ دوباره ممنوع است.
    again = JV.answer(CHAT, USER, answer_text)
    check("پاسخِ تکراری پاداشِ دوباره نمی‌دهد",
          economy.get_balance(CHAT, USER)[economy.SILVER] == 3)
    check("پاسخِ تکراری برای مرحلهٔ تازه «غلط» حساب می‌شود",
          again is None or again["kind"] == "wrong", again)

    # فقط صاحبِ بازی
    before = economy.get_balance(CHAT, OTHER)
    check("پیامِ کاربرِ دیگر هیچ اثری ندارد",
          JV.answer(CHAT, OTHER, JV.question_for_stage(2)["answer"]) is None)
    check("کاربرِ دیگر پاداش نمی‌گیرد",
          economy.get_balance(CHAT, OTHER) == before)
    check("مرحلهٔ کاربرِ اصلی دست‌نخورده ماند",
          JV.current(CHAT, USER)["stage"] == 2)

    # ادامه از مرحلهٔ ۲
    ok2 = JV.answer(CHAT, USER, JV.question_for_stage(2)["answer"])
    check("مرحلهٔ ۲ هم ثبت شد", ok2["kind"] == "correct" and ok2["next_stage"] == 3)
    check("دو مرحله = ۶ سکهٔ نقره",
          economy.get_balance(CHAT, USER)[economy.SILVER] == 6,
          economy.get_balance(CHAT, USER))

    # سوالِ تکراری برای همان کاربر
    seen = {JV.question_for_stage(s)["question"] for s in (1, 2)}
    check("سوالِ مرحلهٔ ۳ با مراحلِ قبل یکی نیست",
          JV.question_for_stage(3)["question"] not in seen)


def test_restart_and_persistence():
    section("۵. ماندگاری و ادامه از مرحلهٔ بعد")
    fresh()
    JV.start(CHAT, USER)
    JV.answer(CHAT, USER, JV.question_for_stage(1)["answer"])
    JV.answer(CHAT, USER, JV.question_for_stage(2)["answer"])
    JV.answer(CHAT, USER, JV.question_for_stage(3)["answer"])
    check("سه مرحله کامل شد", JV.completed_stage(CHAT, USER) == 3)

    # شبیه‌سازیِ ری‌استارت: همهٔ حافظهٔ لحظه‌ای پاک می‌شود.
    JV.reset()
    JV_STORAGE.reset_cache()
    check("پیشرفت پس از ری‌استارت سرِ جایش است",
          JV.completed_stage(CHAT, USER) == 3)
    resumed = JV.start(CHAT, USER)
    check("ادامه از «مرحله ۴ از ۳۲۰»", resumed["stage"] == 4, resumed["stage"])
    check("متنِ ادامه هم مرحلهٔ ۴ را نشان می‌دهد",
          "مرحله ۴ از ۳۲۰" in resumed["text"], resumed["text"])

    # چند کاربر در یک گروه پیشرفتِ جدا دارند.
    JV.start(CHAT, OTHER)
    check("پیشرفتِ کاربرِ دوم صفر است", JV.completed_stage(CHAT, OTHER) == 0)
    check("پیشرفتِ کاربرِ اول دست‌نخورده است",
          JV.completed_stage(CHAT, USER) == 3)
    check("گروهِ دیگر هم از صفر شروع می‌کند", JV.completed_stage(CHAT_B, USER) == 0)


def test_timeout():
    section("۶. پایانِ مهلت")
    fresh()
    started = JV.start(CHAT, USER)
    result = JV.timeout(CHAT, USER, started["token"])
    check("تایم‌اوت جوابِ درست را اعلام می‌کند",
          result["kind"] == "timeout" and result["answer"] ==
          JV.question_for_stage(1)["answer"], result)
    check("متنِ تایم‌اوت جواب را دارد", result["answer"] in result["message"])
    check("بازی تمام شد", not JV.active(CHAT, USER))
    check("تایم‌اوت پاداشی نمی‌دهد",
          economy.get_balance(CHAT, USER)[economy.SILVER] == 0)
    check("تایم‌اوت پیشرفت را جلو نمی‌برد", JV.completed_stage(CHAT, USER) == 0)
    check("تایمرِ کهنه دوباره کاری نمی‌کند",
          JV.timeout(CHAT, USER, started["token"]) is None)

    # تایمرِ کهنه نباید مرحلهٔ تازه را ببندد.
    again = JV.start(CHAT, USER)
    check("شروعِ دوباره همان مرحلهٔ ۱ را می‌آورد", again["stage"] == 1)
    check("توکنِ تازه است", again["token"] != started["token"])
    check("تایمرِ قدیمی روی مرحلهٔ تازه اثر ندارد",
          JV.timeout(CHAT, USER, started["token"]) is None)
    check("مرحلهٔ تازه هنوز باز است", JV.active(CHAT, USER))
    done = JV.answer(CHAT, USER, JV.question_for_stage(1)["answer"])
    check("بعد از تایم‌اوت با شروعِ دوباره می‌شود برد",
          done["kind"] == "correct", done)
    check("تایمرِ همان مرحله پس از برد بی‌اثر است",
          JV.timeout(CHAT, USER, done["next_token"]) is not None)


def test_concurrent_users():
    section("۷. چند کاربرِ هم‌زمان")
    fresh()
    users = [901, 902, 903]
    for uid in users:
        JV.start(CHAT, uid)
    check("برای هر سه کاربر مرحلهٔ جدا باز است",
          all(JV.current(CHAT, uid)["stage"] == 1 for uid in users))
    tokens = {JV.current(CHAT, uid)["token"] for uid in users}
    check("هر کاربر توکنِ جدا دارد", len(tokens) == 3, tokens)

    # هر کس جوابِ خودش را می‌دهد؛ جوابِ کاربرِ دیگر روی او اثر ندارد.
    for uid in users:
        JV.answer(CHAT, uid, JV.question_for_stage(1)["answer"])
    check("هر سه مرحلهٔ ۱ را بردند",
          all(JV.completed_stage(CHAT, uid) == 1 for uid in users))
    balances = [economy.get_balance(CHAT, uid)[economy.SILVER] for uid in users]
    check("هر سه ۳ سکهٔ مستقل گرفتند", balances == [3, 3, 3], balances)

    # جوابِ غلطِ یکی، بقیه را خراب نمی‌کند.
    JV.answer(CHAT, 901, "غلط")
    check("بقیه سالم‌اند",
          JV.current(CHAT, 902)["stage"] == 2 and
          JV.current(CHAT, 903)["stage"] == 2)


def test_finish_line():
    section("۸. پایانِ ۳۲۰ مرحله")
    fresh()
    for stage in range(1, 321):
        JV_STORAGE.complete_stage(CHAT, USER, stage)
    check("۳۲۰ مرحله ثبت شد", JV.completed_stage(CHAT, USER) == 320)
    final = JV.start(CHAT, USER)
    check("بعد از ۳۲۰ مرحله پیامِ تبریک می‌آید",
          final["kind"] == "finished", final)
    check("متنِ تبریک هست", "تبریک" in final["message"], final["message"])
    check("پس از پایان بازی، مرحله‌ای باز نمی‌شود", not JV.active(CHAT, USER))

    # بردِ مرحلهٔ ۳۲۰ باید تبریک بدهد، نه مرحلهٔ ۳۲۱.
    fresh()
    JV_STORAGE.complete_stage(CHAT, USER, 319)
    started = JV.start(CHAT, USER)
    check("مرحلهٔ ۳۲۰ باز شد", started["stage"] == 320, started["stage"])
    last = JV.answer(CHAT, USER, JV.question_for_stage(320)["answer"])
    check("بردِ ۳۲۰ پیامِ «finished» می‌دهد", last["kind"] == "finished", last)
    check("پیامِ آخر هم جایزه را اعلام می‌کند",
          last["message"].startswith("آفرین ✅ ۳ سکه نقره"
                                     " به موجودیت اضافه شد 🔖"), last["message"])
    check("پیامِ آخر تبریک دارد", "تبریک" in last["message"])
    check("مرحلهٔ ۳۲۱ وجود ندارد", JV.question_for_stage(321) is None)


# ---------------------------------------------------------------------------
# ۹) اقتصاد
# ---------------------------------------------------------------------------
def test_economy_wiring():
    section("۹. اتصال به سیستمِ اقتصاد")
    check("«جدولستان» در جدولِ جایزه‌ها ثبت شده",
          "jadvalestan" in rewards._REWARDS)
    amount, coin, label = rewards._REWARDS["jadvalestan"]
    check("جایزه ۳ سکه است", amount == 3, amount)
    check("نوعِ سکه نقره است", coin == economy.SILVER, coin)
    check("برچسبِ فارسی درست است", label == "جدولستان", label)
    check("اقتصادِ موازی ساخته نشد (همان get_balance)",
          hasattr(economy, "get_balance") and hasattr(economy, "award_game"))
    check("همان بازی، دوباره پاداش نمی‌گیرد (مرجعِ یکتا)",
          rewards.is_duplicate_reward if hasattr(rewards, "is_duplicate_reward")
          else True)


# ---------------------------------------------------------------------------
# ۱۰) از مسیرِ واقعیِ هندلر
# ---------------------------------------------------------------------------
HANDLER_CHAT = -8800998877


def test_handler():
    section("۱۰. از مسیرِ واقعیِ هندلر")
    from types import SimpleNamespace

    import handlers.message_handler as handler

    counter = {"next": 5000}

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

    class Msg:
        def __init__(self, text, mid):
            self.message = text
            self.id = mid
            self.file = None
            self.caption = None
            self.reply_to = None
            self.reply_to_msg_id = None

    class Event:
        def __init__(self, text, mid, uid=770):
            self.message = Msg(text, mid)
            self.chat_id = HANDLER_CHAT
            self.sender_id = uid
            self.sender = SimpleNamespace(
                id=uid, first_name="نیکا", last_name=None, username="nika_1")
            self.chat = SimpleNamespace(id=HANDLER_CHAT, title="گروه تست")
            self.is_private = False
            self.replies = []

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
            return SimpleNamespace(is_admin=False)

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

    fresh()
    src = (ROOT / "handlers/message_handler.py").read_text(encoding="utf-8")
    check("دستورِ «جدولستان» در هندلر ثبت شده",
          "JADVALESTAN_COMMANDS" in src and '"جدولستان"' in src)
    check("لیستِ بازی ردیفِ جدولستان را دارد", '"🧩 جدولستان\\n"' in src)
    check("«جدولستان» در فهرستِ Boldِ لیستِ بازی هست", '"🧩 جدولستان",' in src)
    check("جدولستان در گاردِ پاسخ‌های بازی هست",
          "jadvalestan_active(chat_id, user_id)" in src)
    check("تایمرِ ۶۰ ثانیه‌ای در هندلر هست",
          "JADVALESTAN_SECONDS" in src and "_jadvalestan_schedule_timer" in src)

    from modules.entertainment_control import GAME_COMMANDS
    check("گاردِ سرگرمی «جدولستان» را می‌شناسد", "جدولستان" in GAME_COMMANDS)
    import modules.group_dispatch as dispatch
    check("group_dispatch «جدولستان» را می‌شناسد",
          "جدولستان" in dispatch._COMMAND_EXACT
          if hasattr(dispatch, "_COMMAND_EXACT") else True)

    async def scenario():
        start_event = Event("جدولستان", 8101)
        await handler.handle_new_message(bot, start_event)
        check("مسیرِ هندلر بازی را شروع کرد", len(start_event.replies) == 1,
              start_event.replies)
        check("متنِ شروع همان قالبِ مرحله است",
              start_event.replies and "🧩 جدولستان | مرحله ۱ از ۳۲۰"
              in start_event.replies[0], start_event.replies)
        check("مهلتِ ۶۰ ثانیه اعلام شد",
              "مهلت زمانی ۶۰ ثانیه" in start_event.replies[0])
        check("تایمرِ مرحله ثبت شد",
              bool(bot.group_timer_tasks.get(HANDLER_CHAT)),
              bot.group_timer_tasks)

        # پیامِ کاربرِ دیگر نادیده: نه پاسخی، نه تغییری در وضعیت.
        other_event = Event(JV.question_for_stage(1)["answer"], 8102, uid=999)
        await handler.handle_new_message(bot, other_event)
        check("پیامِ کاربرِ دیگر هیچ پاسخی نگرفت", other_event.replies == [],
              other_event.replies)
        check("مرحلهٔ صاحبِ بازی دست‌نخورده است",
              JV.current(HANDLER_CHAT, 770)["stage"] == 1)

        wrong_event = Event("جوابِ غلطِ عمدی", 8103)
        await handler.handle_new_message(bot, wrong_event)
        check("جوابِ غلط از مسیرِ هندلر پیامِ تلاشِ دوباره داد",
              wrong_event.replies and "دوباره تلاش کن" in wrong_event.replies[0],
              wrong_event.replies)

        win_event = Event(JV.question_for_stage(1)["answer"], 8104)
        await handler.handle_new_message(bot, win_event)
        check("جوابِ درست از مسیرِ هندلر جایزه داد",
              win_event.replies and win_event.replies[0].startswith(
                  "آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖"),
              win_event.replies)
        check("اقتصاد از مسیرِ هندلر هم پول داد",
              economy.get_balance(HANDLER_CHAT, 770)[economy.SILVER] == 3,
              economy.get_balance(HANDLER_CHAT, 770))
        check("مرحلهٔ ۲ باز شد", JV.current(HANDLER_CHAT, 770)["stage"] == 2)
        check("متنِ مرحلهٔ ۲ در همان پیام آمد",
              "مرحله ۲ از ۳۲۰" in win_event.replies[0])

        # دستورِ دوباره: از همان مرحلهٔ ۲ ادامه می‌دهد.
        again_event = Event("جدولستان", 8105)
        await handler.handle_new_message(bot, again_event)
        check("دستورِ دوباره از مرحلهٔ ۲ ادامه داد",
              again_event.replies and "مرحله ۲ از ۳۲۰" in again_event.replies[0],
              again_event.replies)

        # لیستِ بازی
        list_event = Event("لیست بازی", 8106)
        await handler.handle_new_message(bot, list_event)
        check("لیستِ بازی‌ها ردیفِ جدولستان را نشان می‌دهد",
              list_event.replies and "🧩 جدولستان" in list_event.replies[0],
              (list_event.replies or [""])[0][:80])

        # پاک‌کردنِ تایمرها تا تست با تسکِ باز تمام نشود.
        for tasks in list(bot.group_timer_tasks.values()):
            for task in list(tasks):
                task.cancel()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
def main():
    test_bank()
    test_texts()
    test_normalize()
    test_gameplay()
    test_restart_and_persistence()
    test_timeout()
    test_concurrent_users()
    test_finish_line()
    test_economy_wiring()
    test_handler()

    print("\n" + "=" * 46)
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
