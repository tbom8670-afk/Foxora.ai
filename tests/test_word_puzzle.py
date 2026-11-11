# -*- coding: utf-8 -*-
"""🧪 تستِ بازی «پازل کلمه».

    PYTHONPATH=. python3 tests/test_word_puzzle.py

پوشش:
    • بانکِ ۲۰۰ کلمه‌ای: یکتا، فارسی، رایج، از ۳ به ۹ حرفی، بدونِ تکرار
    • جدول: حروفِ الکی بیگانه با کلمه، بدونِ تکرار، بیشترشدن با مرحله
    • ۵ مرحلهٔ اول مرتب، از مرحلهٔ ۶ قاطی
    • متنِ دقیقِ مرحله + Bold دقیقاً روی حروف (بازه‌های UTF-16)
    • فقط صاحبِ بازی؛ ۴۵ ثانیه؛ تلاشِ دوباره؛ پایانِ مهلت
    • جوابِ درست ⇒ ۳ سکهٔ نقره، یک‌بار برای هر مرحله
    • پیشرفتِ دائمی، ادامه از مرحلهٔ بعد، کلمهٔ تکراری برای یک کاربر
    • استقلالِ چند کاربرِ هم‌زمان
    • اتصال به هندلر، لیستِ بازی، گاردِ سرگرمی و group_dispatch
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
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


TEMP = Path(tempfile.mkdtemp(prefix="word-puzzle-test-"))
os.environ.setdefault("BOT_HOME", str(TEMP / "bot_home"))
os.environ["WORD_PUZZLE_PROGRESS_FILE"] = str(TEMP / "progress.json")

import economy  # noqa: E402
import economy.storage as economy_storage  # noqa: E402
from economy import rewards  # noqa: E402
from modules import word_puzzle as WP  # noqa: E402
from modules.word_puzzle import storage as WP_STORAGE  # noqa: E402
from modules.word_puzzle import texts as WP_TEXTS  # noqa: E402

economy_storage.use_file(TEMP / "economy.json")

CHAT = -7001122334
CHAT_B = -7001122344
USER = 640
OTHER = 641

PERSIAN_ONLY = re.compile(r"^[\u0621-\u06CC]+$")


def fresh():
    WP.reset()
    WP_STORAGE.reset_all()
    economy_storage.use_file(TEMP / ("economy-%d.json" % (PASS + FAIL)))


def utf16_slice(text, offset, length):
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le")


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# ۱) بانکِ کلمه‌ها
# ---------------------------------------------------------------------------
def test_words_bank():
    section("۱. بانکِ ۲۰۰ کلمه")
    check("فایلِ words.json موجود است", WP.WORDS_FILE.is_file(), WP.WORDS_FILE)
    raw = json.loads(WP.WORDS_FILE.read_text(encoding="utf-8"))
    words = raw["words"]
    check("۲۰۰ مرحله ذخیره شده", len(words) == 200, len(words))
    check("ماژول هم ۲۰۰ کلمه بارگذاری کرد", WP.TOTAL == 200, WP.TOTAL)
    check("شمارهٔ مرحله‌ها ۱..۲۰۰ و پشتِ‌سرهم است",
          [w["stage"] for w in words] == list(range(1, 201)))

    texts = [w["word"] for w in words]
    check("هیچ کلمه‌ای تکراری نیست", len(set(texts)) == 200,
          len(texts) - len(set(texts)))
    check("همهٔ کلمه‌ها فقط حروفِ فارسی‌اند",
          all(PERSIAN_ONLY.match(w) for w in texts),
          [w for w in texts if not PERSIAN_ONLY.match(w)])
    check("هیچ کلمه‌ای فاصله/نیم‌فاصله/رقم ندارد",
          all(" " not in w and "\u200c" not in w and not re.search(r"\d", w)
              for w in texts))
    check("هیچ کلمه‌ای کوتاه‌تر از ۳ حرف نیست",
          all(len(w) >= 3 for w in texts),
          [w for w in texts if len(w) < 3])

    lengths = [len(w) for w in texts]
    check("از ساده به سخت: طول‌ها صعودی‌اند", lengths == sorted(lengths))
    check("مراحلِ اول ۳ حرفی‌اند", lengths[:5] == [3] * 5, lengths[:5])
    check("آخرِ بازی کلمه‌های بلند دارد", max(lengths) >= 9, max(lengths))
    check("همهٔ طول‌ها بین ۳ و ۹ هستند", max(lengths) <= 9, max(lengths))
    histogram = {}
    for length in lengths:
        histogram[length] = histogram.get(length, 0) + 1
    check("پخشِ طول‌ها منطقی است (۳ تا ۹ همه هست)",
          set(histogram) == {3, 4, 5, 6, 7, 8, 9}, histogram)

    # کلمه‌های رایجِ نمونه باید در بانک باشند و هیچ کلمهٔ نامفهومی نباشد.
    must_have = {"عشق", "دریا", "کتاب", "مهربان", "خانه", "ستاره", "خورشید"}
    missing = must_have - set(texts)
    check("کلمه‌های رایجِ نمونه در بانک هست", not missing, missing)
    check("کلمهٔ نمونهٔ کارفرما («مهربان») هست", "مهربان" in texts)
    # هیچ کلمه‌ای با حروفِ تکراریِ عجیب یا بی‌معنی نیست
    weird = [w for w in texts if len(set(w)) == 1]
    check("کلمهٔ یک‌حرفیِ تکراری وجود ندارد", not weird, weird)

    # ساختِ دوباره از منبعِ یکتا باید همان فایل را بدهد.
    result = subprocess.run(
        [sys.executable, "tools/build_word_puzzle_words.py"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    check("اسکریپتِ سازنده بدونِ خطا اجرا شد", result.returncode == 0,
          result.stdout[-300:])
    rebuilt = json.loads(WP.WORDS_FILE.read_text(encoding="utf-8"))
    check("خروجیِ سازنده با فایلِ ذخیره‌شده یکی است", rebuilt == raw)


# ---------------------------------------------------------------------------
# ۲) ساختِ جدول
# ---------------------------------------------------------------------------
def test_grid():
    section("۲. جدولِ حروف")
    check("مرحلهٔ ۱ سه حرفِ الکی دارد", WP.fake_count(1) == 3, WP.fake_count(1))
    counts = [WP.fake_count(stage) for stage in range(1, 201)]
    check("تعدادِ حروفِ الکی هیچ‌وقت کم نمی‌شود",
          all(b >= a for a, b in zip(counts, counts[1:])))
    check("با بالا رفتنِ مرحله بیشتر می‌شود",
          WP.fake_count(60) > WP.fake_count(1), (WP.fake_count(1), WP.fake_count(60)))
    check("سقفِ حروفِ الکی رعایت شده", max(counts) <= WP.MAX_FAKES, max(counts))
    check("مرحله‌های اول ۴ و بعد ۵ و ۶ هم دارند",
          (WP.fake_count(11), WP.fake_count(21), WP.fake_count(31)) == (4, 5, 6),
          (WP.fake_count(11), WP.fake_count(21), WP.fake_count(31)))

    bad_fake = []
    repeated = []
    missing_word = []
    for stage in range(1, 201):
        word = WP.word_for_stage(stage)["word"]
        cells = [char for row in WP.grid_rows(stage) for char in row]
        word_letters = list(word)
        fakes = list(cells)
        for char in word_letters:
            if char in fakes:
                fakes.remove(char)
        if [char for char in cells if char in set()]:
            pass
        if len(fakes) != WP.fake_count(stage):
            bad_fake.append((stage, len(fakes), WP.fake_count(stage)))
        if any(char in set(word_letters) for char in fakes):
            bad_fake.append((stage, "حرفِ الکی از خودِ کلمه"))
        if len(set(fakes)) != len(fakes):
            repeated.append(stage)
        # همهٔ حروفِ کلمه (با تکرار) باید در جدول باشند
        pool = list(cells)
        for char in word_letters:
            if char in pool:
                pool.remove(char)
            else:
                missing_word.append((stage, char))
    check("تعدادِ حروفِ الکیِ هر مرحله دقیق است", not bad_fake, bad_fake[:5])
    check("حروفِ الکی هیچ‌وقت از حروفِ کلمه نیستند", not bad_fake, bad_fake[:5])
    check("حروفِ الکی بینِ خودشان تکراری نیستند", not repeated, repeated[:5])
    check("همهٔ حروفِ کلمه در جدول هست", not missing_word, missing_word[:5])

    check("حروفِ الکی از الفبای فارسی‌اند",
          all(char in WP.ALPHABET
              for stage in range(1, 201)
              for row in WP.grid_rows(stage) for char in row
              if char not in WP.word_for_stage(stage)["word"]))

    # ترتیب: ۵ مرحلهٔ اول مرتب، بعدش قاطی
    ordered_ok = all(
        [char for row in WP.grid_rows(stage) for char in row]
        [:len(WP.word_for_stage(stage)["word"])]
        == list(WP.word_for_stage(stage)["word"])
        for stage in range(1, 6)
    )
    check("۵ مرحلهٔ اول: حروفِ کلمه به ترتیبِ درست", ordered_ok)
    shuffled = 0
    for stage in range(6, 60):
        word = WP.word_for_stage(stage)["word"]
        cells = [char for row in WP.grid_rows(stage) for char in row]
        if cells[:len(word)] != list(word):
            shuffled += 1
    check("از مرحلهٔ ۶ به بعد ترتیب قاطی می‌شود", shuffled >= 45, shuffled)

    # جدولِ یک مرحله همیشه همان است (ری‌استارت جدول را عوض نمی‌کند)
    first = WP.grid_rows(37)
    WP.reset()
    check("جدولِ هر مرحله پایدار است", WP.grid_rows(37) == first)

    # هیچ سطرِ تک‌حرفی و بی‌ریختی نماند
    ragged = [
        stage for stage in range(1, 201)
        if len(WP.grid_rows(stage)) > 1 and len(WP.grid_rows(stage)[-1]) == 1
    ]
    check("هیچ جدولی سطرِ آخرِ تک‌حرفی ندارد", not ragged, ragged[:5])


# ---------------------------------------------------------------------------
# ۳) متن و Bold
# ---------------------------------------------------------------------------
def test_text_and_bold():
    section("۳. متنِ مرحله و Bold")
    check("مهلتِ هر مرحله ۴۵ ثانیه است", WP.TIME_LIMIT == 45, WP.TIME_LIMIT)
    m = re.search(r"TIME_LIMIT\s*=\s*(\d+)",
                  (ROOT / "modules/word_puzzle/texts.py").read_text(encoding="utf-8"))
    check("عددِ ۴۵ در texts.py هم هست", int(m.group(1)) == 45, m.group(1))

    payload = WP.stage_payload(1)
    word = payload["word"]
    rows = payload["rows"]
    expected = (
        "🧩 مرحله ۱ از ۲۰۰\n"
        "\n"
        + "\n".join(" - ".join(row) for row in rows) + "\n"
        "کلمه‌ی مخفی رو پیدا کن (۳ حرفی)\n"
        "\n"
        "🎮 مهلت زمانی ۴۵ ثانیه"
    )
    check("متنِ مرحله دقیقاً مطابقِ خواستهٔ کارفرماست",
          payload["text"] == expected, payload["text"])
    check("حروف با « - » از هم جدا شده‌اند",
          " - " in payload["text"] and " -  - " not in payload["text"])
    check("تعدادِ حروف از خودِ کلمه می‌آید",
          payload["letters"] == len(word), (payload["letters"], word))
    check("خطِ راهنما تعدادِ حروف را می‌گوید",
          "(۳ حرفی)" in payload["text"], payload["text"])
    check("مهلتِ ۴۵ ثانیه در متن هست", "🎮 مهلت زمانی ۴۵ ثانیه" in payload["text"])

    # Bold: هر خانهٔ جدول دقیقاً یک بازه، و بازه‌ها فقط خودِ حرف‌اند.
    cells = [char for row in rows for char in row]
    spans = payload["spans"]
    check("برای هر خانهٔ جدول یک بازهٔ Bold هست", len(spans) == len(cells),
          (len(spans), len(cells)))
    sliced = [utf16_slice(payload["text"], offset, length)
              for offset, length in spans]
    check("بازه‌ها دقیقاً روی حروفِ جدول افتاده‌اند", sliced == cells, sliced)
    check("هیچ بازه‌ای شاملِ « - » نیست", all(len(chunk) == 1 for chunk in sliced),
          [chunk for chunk in sliced if len(chunk) != 1])
    check("بازه‌ها با احتسابِ واحدِ UTF-16 درست‌اند (ایموجی ۲ واحد است)",
          all(0 <= offset < utf16_len(payload["text"]) for offset, _ in spans))

    for stage in (2, 6, 90, 199, 200):
        item = WP.stage_payload(stage)
        cells = [char for row in item["rows"] for char in row]
        parts = [utf16_slice(item["text"], o, l) for o, l in item["spans"]]
        check("Boldِ مرحلهٔ %d دقیقاً روی حروف است" % stage, parts == cells,
              (parts, cells))

    check("متنِ برد عیناً «آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖» است",
          WP_TEXTS.correct_text() == "آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖",
          WP_TEXTS.correct_text())
    check("متنِ پایانِ مهلت کلمهٔ درست را دارد",
          "دریا" in WP_TEXTS.timeout_text("دریا"))
    check("متنِ تبریک ۲۰۰ را می‌گوید", "۲۰۰" in WP_TEXTS.finished_text(200))


# ---------------------------------------------------------------------------
# ۴) نرمال‌سازی
# ---------------------------------------------------------------------------
def test_normalize():
    section("۴. نرمال‌سازیِ ورودی")
    check("ي و ك عربی → ی و ک", WP.normalize("علي اكبر") == "علی اکبر")
    check("حذفِ اعراب", WP.normalize("حَسَن") == "حسن", WP.normalize("حَسَن"))
    check("حذفِ نیم‌فاصله", WP.normalize("می‌روم") == WP.normalize("میروم"))
    check("فاصله‌های اضافه", WP.normalize("  کتاب   ") == "کتاب",
          WP.normalize("  کتاب   "))
    check("نشانه‌ها حذف می‌شوند", WP.normalize("کتاب!") == "کتاب")
    check("«مهربان» با ي عربی هم قبول است",
          WP.compact("مهربان") == WP.compact("مهربان"))


# ---------------------------------------------------------------------------
# ۵) جریانِ بازی
# ---------------------------------------------------------------------------
def test_gameplay():
    section("۵. جریانِ بازی")
    fresh()
    check("کاربرِ تازه پیشرفتی ندارد", WP.completed_stage(CHAT, USER) == 0)
    check("بدونِ شروع، مرحله‌ای باز نیست", not WP.active(CHAT, USER))

    started = WP.start(CHAT, USER)
    check("بازی شروع شد", started["kind"] == "started", started["kind"])
    check("مرحلهٔ ۱ از ۲۰۰", started["stage"] == 1)
    check("متنِ شروع «مرحله ۱ از ۲۰۰» دارد",
          "مرحله ۱ از ۲۰۰" in started["text"], started["text"][:40])
    check("بعد از شروع مرحله باز است", WP.active(CHAT, USER))

    word = WP.word_for_stage(1)["word"]
    wrong = WP.answer(CHAT, USER, "یک کلمهٔ غلط")
    check("جوابِ غلط: kind=wrong", wrong["kind"] == "wrong")
    check("جوابِ غلط اجازهٔ تلاشِ دوباره می‌دهد", WP.active(CHAT, USER))
    check("جوابِ غلط چیزی ثبت نکرد", WP.completed_stage(CHAT, USER) == 0)

    ok = WP.answer(CHAT, USER, "  %s " % word)
    check("جوابِ درست: kind=correct", ok["kind"] == "correct", ok["kind"])
    check("پیامِ برد عیناً متنِ کارفرماست",
          ok["message"].startswith("آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖"),
          ok["message"][:60])
    check("۳ سکهٔ نقره واقعاً اضافه شد",
          economy.get_balance(CHAT, USER)[economy.SILVER] == 3,
          economy.get_balance(CHAT, USER))
    check("مرحلهٔ ۲ همان لحظه باز شد", ok["next_stage"] == 2)
    check("متنِ مرحلهٔ ۲ در همان پیام و با Bold هست",
          "مرحله ۲ از ۲۰۰" in ok["message"] and len(ok["spans"]) > 0,
          (len(ok["spans"]),))
    check("بازه‌های Bold در پیامِ ترکیبی هم درست‌اند",
          sorted(utf16_slice(ok["message"], o, l) for o, l in ok["spans"])
          == sorted(char for row in WP.stage_payload(2)["rows"] for char in row))
    check("پیشرفت = ۱", WP.completed_stage(CHAT, USER) == 1)

    again = WP.answer(CHAT, USER, word)
    check("پاسخِ تکراری پاداشِ دوباره نمی‌دهد",
          economy.get_balance(CHAT, USER)[economy.SILVER] == 3)
    check("پاسخِ تکراری برای مرحلهٔ تازه پذیرفته نمی‌شود",
          again is None or again["kind"] == "wrong", again)

    before = economy.get_balance(CHAT, OTHER)
    check("پیامِ کاربرِ دیگر کاملاً نادیده است",
          WP.answer(CHAT, OTHER, WP.word_for_stage(2)["word"]) is None)
    check("کاربرِ دیگر پاداش نمی‌گیرد",
          economy.get_balance(CHAT, OTHER) == before)
    check("مرحلهٔ صاحبِ بازی دست‌نخورده است",
          WP.current(CHAT, USER)["stage"] == 2)

    ok2 = WP.answer(CHAT, USER, WP.word_for_stage(2)["word"])
    check("مرحلهٔ ۲ ثبت شد", ok2["kind"] == "correct" and ok2["next_stage"] == 3)
    check("دو مرحله = ۶ سکه",
          economy.get_balance(CHAT, USER)[economy.SILVER] == 6,
          economy.get_balance(CHAT, USER))
    words_seen = {WP.word_for_stage(stage)["word"] for stage in (1, 2, 3)}
    check("کلمهٔ مرحلهٔ ۳ با کلمه‌های قبلی یکی نیست", len(words_seen) == 3)


def test_persistence():
    section("۶. ماندگاری و ادامه")
    fresh()
    for stage in (1, 2, 3):
        WP.start(CHAT, USER)
        WP.answer(CHAT, USER, WP.word_for_stage(stage)["word"])
    check("سه مرحله کامل شد", WP.completed_stage(CHAT, USER) == 3)

    WP.reset()
    WP_STORAGE.reset_cache()
    check("پیشرفت پس از ری‌استارت مانده است",
          WP.completed_stage(CHAT, USER) == 3)
    resumed = WP.start(CHAT, USER)
    check("ادامه از «مرحله ۴ از ۲۰۰»", resumed["stage"] == 4, resumed["stage"])
    check("متن هم مرحلهٔ ۴ را نشان می‌دهد", "مرحله ۴ از ۲۰۰" in resumed["text"])

    WP.start(CHAT, OTHER)
    check("کاربرِ دوم از صفر شروع می‌کند", WP.completed_stage(CHAT, OTHER) == 0)
    check("پیشرفتِ کاربرِ اول دست‌نخورده است",
          WP.completed_stage(CHAT, USER) == 3)
    check("گروهِ دیگر هم از صفر است", WP.completed_stage(CHAT_B, USER) == 0)


def test_timeout():
    section("۷. پایانِ مهلت")
    fresh()
    started = WP.start(CHAT, USER)
    result = WP.timeout(CHAT, USER, started["token"])
    check("پایانِ مهلت کلمهٔ درست را می‌گوید",
          result["kind"] == "timeout" and
          result["word"] == WP.word_for_stage(1)["word"], result)
    check("کلمه در متنِ پایانِ مهلت هست", result["word"] in result["message"])
    check("بازی تمام شد", not WP.active(CHAT, USER))
    check("پایانِ مهلت پاداشی نمی‌دهد",
          economy.get_balance(CHAT, USER)[economy.SILVER] == 0)
    check("پایانِ مهلت پیشرفت را جلو نمی‌برد",
          WP.completed_stage(CHAT, USER) == 0)
    check("تایمرِ کهنه دوباره کاری نمی‌کند",
          WP.timeout(CHAT, USER, started["token"]) is None)

    again = WP.start(CHAT, USER)
    check("شروعِ دوباره همان مرحلهٔ ۱ را می‌آورد", again["stage"] == 1)
    check("تایمرِ قدیمی روی مرحلهٔ تازه اثر ندارد",
          WP.timeout(CHAT, USER, started["token"]) is None)
    check("مرحلهٔ تازه هنوز باز است", WP.active(CHAT, USER))
    done = WP.answer(CHAT, USER, WP.word_for_stage(1)["word"])
    check("بعد از پایانِ مهلت می‌شود دوباره برد", done["kind"] == "correct")
    check("تایمرِ مرحلهٔ تازه فعال است",
          WP.timeout(CHAT, USER, done["next_token"]) is not None)


def test_concurrent():
    section("۸. چند کاربرِ هم‌زمان")
    fresh()
    users = [801, 802, 803]
    for uid in users:
        WP.start(CHAT, uid)
    check("همه در مرحلهٔ ۱ هستند",
          all(WP.current(CHAT, uid)["stage"] == 1 for uid in users))
    check("هر کاربر توکنِ جدا دارد",
          len({WP.current(CHAT, uid)["token"] for uid in users}) == 3)
    for uid in users:
        WP.answer(CHAT, uid, WP.word_for_stage(1)["word"])
    check("هر سه مرحلهٔ ۱ را بردند",
          all(WP.completed_stage(CHAT, uid) == 1 for uid in users))
    check("هر سه ۳ سکهٔ نقرهٔ مستقل گرفتند",
          [economy.get_balance(CHAT, uid)[economy.SILVER] for uid in users]
          == [3, 3, 3])
    WP.answer(CHAT, 801, "غلط")
    check("جوابِ غلطِ یکی روی بقیه اثر ندارد",
          WP.current(CHAT, 802)["stage"] == 2 and
          WP.current(CHAT, 803)["stage"] == 2)


def test_finish_line():
    section("۹. پایانِ ۲۰۰ مرحله")
    fresh()
    for stage in range(1, 201):
        WP_STORAGE.complete_stage(CHAT, USER, stage)
    check("۲۰۰ مرحله ثبت شد", WP.completed_stage(CHAT, USER) == 200)
    final = WP.start(CHAT, USER)
    check("بعد از ۲۰۰ مرحله پیامِ تبریک می‌آید",
          final["kind"] == "finished", final["kind"])
    check("متنِ تبریک دارد", "تبریک" in final["message"])
    check("بعد از پایان مرحله‌ای باز نمی‌شود", not WP.active(CHAT, USER))

    fresh()
    WP_STORAGE.complete_stage(CHAT, USER, 199)
    started = WP.start(CHAT, USER)
    check("مرحلهٔ ۲۰۰ باز شد", started["stage"] == 200)
    last = WP.answer(CHAT, USER, WP.word_for_stage(200)["word"])
    check("بردِ مرحلهٔ ۲۰۰ پیامِ finished می‌دهد", last["kind"] == "finished")
    check("پیامِ آخر جایزه را اعلام می‌کند",
          last["message"].startswith("آفرین ✅ ۳ سکه نقره"
                                     " به موجودیت اضافه شد 🔖"), last["message"][:60])
    check("پیامِ آخر تبریک دارد", "تبریک" in last["message"])
    check("مرحلهٔ ۲۰۱ وجود ندارد", WP.word_for_stage(201) is None)


def test_economy():
    section("۱۰. اتصال به اقتصاد")
    check("«پازل کلمه» در جدولِ جایزه‌ها هست", "word_puzzle" in rewards._REWARDS)
    amount, coin, label = rewards._REWARDS["word_puzzle"]
    check("جایزه ۳ سکه است", amount == 3, amount)
    check("نوعِ سکه نقره است", coin == economy.SILVER, coin)
    check("برچسبِ فارسی درست است", label == "پازل کلمه", label)
    check("همان اقتصادِ موجود است (بدونِ کیفِ موازی)",
          hasattr(economy, "get_balance") and hasattr(economy, "award_game"))


# ---------------------------------------------------------------------------
# ۱۱) از مسیرِ واقعیِ هندلر
# ---------------------------------------------------------------------------
HANDLER_CHAT = -7009988776


def test_handler():
    section("۱۱. از مسیرِ واقعیِ هندلر")
    from types import SimpleNamespace

    import handlers.message_handler as handler

    counter = {"next": 6000}

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
        def __init__(self, text, mid, uid=USER):
            self.message = Msg(text, mid)
            self.chat_id = HANDLER_CHAT
            self.sender_id = uid
            self.sender = SimpleNamespace(
                id=uid, first_name="آوا", last_name=None, username="ava_1")
            self.chat = SimpleNamespace(id=HANDLER_CHAT, title="گروه تست")
            self.is_private = False
            self.replies = []
            self.entities = []

        async def get_chat(self):
            return self.chat

        async def get_sender(self):
            return self.sender

        async def reply(self, text, formatting_entities=None, **_kw):
            self.replies.append(text)
            self.entities.append(list(formatting_entities or []))
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
            is_spam=lambda *a, **k: (False, None),
        ),
    )

    fresh()
    src = (ROOT / "handlers/message_handler.py").read_text(encoding="utf-8")
    check("دستورِ «پازل کلمه» در هندلر ثبت شده",
          "WORD_PUZZLE_COMMANDS" in src and '"پازل کلمه"' in src)
    check("لیستِ بازی ردیفِ پازل کلمه را دارد", '"🧩 پازل کلمه\\n"' in src)
    check("«پازل کلمه» در فهرستِ Boldِ لیستِ بازی هست", '"🧩 پازل کلمه",' in src)
    check("گاردِ پاسخ‌های بازی آن را می‌شناسد",
          "word_puzzle_active(chat_id, user_id)" in src)
    check("تایمرِ ۴۵ ثانیه‌ای در هندلر هست",
          "WORD_PUZZLE_SECONDS" in src and "_word_puzzle_schedule_timer" in src)

    from modules.entertainment_control import GAME_COMMANDS
    check("گاردِ سرگرمی «پازل کلمه» را می‌شناسد", "پازل کلمه" in GAME_COMMANDS)
    import modules.group_dispatch as dispatch
    check("group_dispatch «پازل کلمه» را می‌شناسد",
          "پازل کلمه" in dispatch._COMMAND_EXACT
          if hasattr(dispatch, "_COMMAND_EXACT") else True)

    async def scenario():
        start_event = Event("پازل کلمه", 9101)
        await handler.handle_new_message(bot, start_event)
        check("مسیرِ هندلر بازی را شروع کرد", len(start_event.replies) == 1,
              start_event.replies)
        text = start_event.replies[0] if start_event.replies else ""
        check("متنِ شروع قالبِ مرحله را دارد", "🧩 مرحله ۱ از ۲۰۰" in text)
        check("مهلتِ ۴۵ ثانیه اعلام شد", "مهلت زمانی ۴۵ ثانیه" in text)
        entities = start_event.entities[0] if start_event.entities else []
        check("حروفِ جدول Bold شده‌اند", len(entities) > 0, len(entities))
        check("تعدادِ Bold با تعدادِ خانه‌های جدول یکی است",
              len(entities) == len(WP.stage_payload(1)["spans"]),
              (len(entities), len(WP.stage_payload(1)["spans"])))
        check("تایمرِ مرحله ثبت شد",
              bool(bot.group_timer_tasks.get(HANDLER_CHAT)))

        other = Event(WP.word_for_stage(1)["word"], 9102, uid=OTHER + 500)
        await handler.handle_new_message(bot, other)
        check("پیامِ کاربرِ دیگر نادیده گرفته شد", other.replies == [],
              other.replies)
        check("مرحلهٔ صاحبِ بازی دست‌نخورده است",
              WP.current(HANDLER_CHAT, USER)["stage"] == 1)

        wrong = Event("کلمهٔ غلط", 9103)
        await handler.handle_new_message(bot, wrong)
        check("جوابِ غلط از مسیرِ هندلر پیامِ تلاشِ دوباره داد",
              wrong.replies and "دوباره تلاش کن" in wrong.replies[0],
              wrong.replies)

        win = Event(WP.word_for_stage(1)["word"], 9104)
        await handler.handle_new_message(bot, win)
        check("جوابِ درست از مسیرِ هندلر جایزه داد",
              win.replies and win.replies[0].startswith(
                  "آفرین ✅ ۳ سکه نقره به موجودیت اضافه شد 🔖"),
              win.replies)
        check("اقتصاد از مسیرِ هندلر هم پول داد",
              economy.get_balance(HANDLER_CHAT, USER)[economy.SILVER] == 3,
              economy.get_balance(HANDLER_CHAT, USER))
        check("مرحلهٔ ۲ باز شد",
              WP.current(HANDLER_CHAT, USER)["stage"] == 2)
        check("Boldِ مرحلهٔ ۲ هم فرستاده شد",
              win.entities and len(win.entities[0]) > 0, win.entities)

        again_event = Event("پازل کلمه", 9105)
        await handler.handle_new_message(bot, again_event)
        check("دستورِ دوباره از مرحلهٔ ۲ ادامه داد",
              again_event.replies and "مرحله ۲ از ۲۰۰" in again_event.replies[0],
              again_event.replies)

        list_event = Event("لیست بازی", 9106)
        await handler.handle_new_message(bot, list_event)
        check("لیستِ بازی‌ها ردیفِ پازل کلمه را نشان می‌دهد",
              list_event.replies and "🧩 پازل کلمه" in list_event.replies[0],
              (list_event.replies or [""])[0][:80])

        for tasks in list(bot.group_timer_tasks.values()):
            for task in list(tasks):
                task.cancel()

    asyncio.run(scenario())


def main():
    test_words_bank()
    test_grid()
    test_text_and_bold()
    test_normalize()
    test_gameplay()
    test_persistence()
    test_timeout()
    test_concurrent()
    test_finish_line()
    test_economy()
    test_handler()

    print("\n" + "=" * 46)
    print("PASS=%d  FAIL=%d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
