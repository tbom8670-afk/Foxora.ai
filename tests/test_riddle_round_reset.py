# -*- coding: utf-8 -*-
"""🧩 پروبِ قطعیِ «ریستِ دورِ چیستان» در حالتِ پیش‌فرضِ SQLite.

قاعده‌ای که بررسی می‌شود: کاربر نباید در یک دور چیستانِ تکراری بگیرد و بعد از
دیدنِ همهٔ چیستان‌ها، دورِ بعد باید از نو شروع شود (پیشرفتِ ذخیره‌شده هم پاک
شود، نه فقط مجموعهٔ درونِ حافظه).

اجرا:
    PYTHONPATH=. python3 tests/test_riddle_round_reset.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# پیش‌فرضِ تولید: RUNTIME_STATE_BACKEND تعیین نشده ⇒ SQLite فعال می‌شود.
os.environ.pop("RUNTIME_STATE_BACKEND", None)
# دادهٔ تست در پوشهٔ موقت؛ دیتابیسِ واقعی دست‌نخورده می‌ماند.
os.environ["SOROUSH_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="riddle-reset-")

from modules import riddles, runtime_db  # noqa: E402

PASSED = 0
FAILED = 0


def check(title, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {title}")
    else:
        FAILED += 1
        print(f"  ❌ {title}  {detail}")


def _persisted(user_key):
    """اندازهٔ ردیفِ ذخیره‌شده — از دیتابیس، نه از حافظه."""
    values = runtime_db.kv_get("riddle_progress", str(user_key), [])
    return len(values or ())


def _reset_state():
    riddles.reset_all()


def main():
    print("\n### ۰) پیش‌فرضِ تولید برقرار است")
    check("SQLite فعال است", runtime_db.SQLITE_ENABLED is True,
          f"{runtime_db.SQLITE_ENABLED}")
    check("انبارِ پیشرفت روی SQLite است", riddles._PROGRESS_STORE.sqlite is True,
          f"{riddles._PROGRESS_STORE.sqlite}")

    total = len(riddles.RIDDLES)
    chat_id, user_id = -100777333111, 909090
    user_key = riddles._user_key(user_id)

    print("\n### ۱) پیمایشِ کاملِ همهٔ چیستان‌ها")
    _reset_state()
    seen_sizes = []
    questions = []
    for _ in range(total):
        questions.append(riddles.new_riddle(chat_id, user_id))
        seen_sizes.append(len(riddles._seen(user_id)))
    check("هیچ چیستانی در یک دور تکرار نشد", len(set(questions)) == total,
          f"unique={len(set(questions))} of {total}")
    check("اندازهٔ پیشرفت هر بار دقیقاً یکی زیاد می‌شود",
          seen_sizes == list(range(1, total + 1)),
          f"{seen_sizes[:3]}...{seen_sizes[-3:]}")
    check("در پایانِ دور، پیشرفت برابرِ تعدادِ کل است",
          seen_sizes[-1] == total, f"{seen_sizes[-1]}")

    # پیشرفت باید ذخیره شده باشد؛ flush فقط در چرخهٔ دوره‌ای رخ می‌دهد.
    riddles._PROGRESS_STORE.flush()
    check("پیشرفتِ پایانِ دور در دیتابیس ذخیره شده",
          _persisted(user_key) == total, f"{_persisted(user_key)}")

    print("\n### ۲) چیستانِ اولِ دورِ بعد باید ریست را نشان دهد")
    after = riddles.new_riddle(chat_id, user_id)
    size_after = len(riddles._seen(user_id))
    check("حجمِ پیشرفت پس از دورِ کامل به ۱ برگشت", size_after == 1,
          f"{size_after}")
    check("چیستانِ دورِ جدید از فهرست است", after in [q for q, _ in riddles.RIDDLES])

    riddles._PROGRESS_STORE.flush()
    persisted_after = _persisted(user_key)
    check("ردیفِ ذخیره‌شده هم ریست شد (نمی‌ماند روی ۱۰۰)",
          persisted_after == 1, f"{persisted_after}")

    print("\n### ۳) دورِ دوم هم مثلِ دورِ اول کامل و بدونِ تکرار است")
    second = [after]
    for _ in range(total - 1):
        second.append(riddles.new_riddle(chat_id, user_id))
    check("دورِ دوم هیچ تکرار داخلی ندارد", len(set(second)) == total,
          f"unique={len(set(second))}")
    check("دورِ دوم کامل شد", len(riddles._seen(user_id)) == total,
          f"{len(riddles._seen(user_id))}")

    print("\n### ۴) کاربرِ دیگر مستقل است (ریستِ یکی، دیگری را خراب نمی‌کند)")
    other_id = 909091
    for _ in range(total):
        riddles.new_riddle(chat_id, other_id)
    riddles._PROGRESS_STORE.flush()
    riddles.new_riddle(chat_id, user_id)   # ریستِ کاربرِ اول
    riddles._PROGRESS_STORE.flush()
    check("پیشرفتِ کاربرِ دوم دست‌نخورده ماند",
          _persisted(riddles._user_key(other_id)) == total,
          f"{_persisted(riddles._user_key(other_id))}")
    check("پیشرفتِ کاربرِ اول ریست شد",
          _persisted(user_key) == 1, f"{_persisted(user_key)}")

    _reset_state()
    print("\n" + "=" * 60)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
