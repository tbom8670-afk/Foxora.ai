# -*- coding: utf-8 -*-
"""🎭 تست «تیکه کلام» — تصادفی، بدون تکرار برای یک کاربر، بدون تکرار هم‌زمان.

قراردادها:

1. دستور «تیکه کلام» کار می‌کند و متن با Bold واقعی سروش فرستاده می‌شود.
2. همهٔ متن‌های فهرست استفاده می‌شوند؛ تا وقتی یک کاربر همهٔ N جمله را
   نگرفته، هیچ جمله‌ای برایش تکرار نمی‌شود (دستهٔ بُر کامل خالی می‌شود).
3. یک جمله هم‌زمان به دو کاربر داده نمی‌شود (پنجرهٔ تازگی).
4. وضعیت روی دیسک ذخیره می‌شود و بعد از ری‌استارت ادامه پیدا می‌کند.
5. «راهنمای روباه» و «لیست کاربران» این دستور را معرفی می‌کنند.
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from modules import tikka_kalam  # noqa: E402

CHAT = -100555111

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
        self.entities = []

    async def get_chat(self):
        return types.SimpleNamespace(id=self.chat_id)

    async def get_sender(self):
        return self.sender

    async def reply(self, text, formatting_entities=None, **kwargs):
        self.replies.append(text)
        self.entities.append(list(formatting_entities or []))

    async def respond(self, text, **kwargs):
        self.replies.append(text)
        self.entities.append([])

    def said(self, needle):
        return any(needle in message for message in self.replies)


def fresh_state():
    tmp = Path(tempfile.mkdtemp()) / "tikka.json"
    tikka_kalam.set_state_path(tmp)
    tikka_kalam.reset_all()
    tikka_kalam.set_state_path(tmp)
    return tmp


def test_texts_are_clean_and_unique():
    print("\n### ۱ فهرست متن‌ها سالم و بدون تکرار است")
    texts = list(tikka_kalam.TIKKAS)
    check("فهرست کامل است (۹۷ جمله)", len(texts) >= 95, f"-> {len(texts)}")
    check("هیچ متن خالی/فاصله‌ی خالی نیست",
          all(text.strip() for text in texts))
    check("هیچ متن تکراری نیست", len(set(texts)) == len(texts),
          f"-> {len(set(texts))} از {len(texts)}")
    check("فاصله‌های اضافی تمیز شده",
          not any("  " in text or text != text.strip() for text in texts))
    check("همهٔ متن‌ها فارسی/خوانا هستند",
          all(any("\u0600" <= char <= "\u06FF" for char in text)
              for text in texts))
    check("تعداد کل با TOTAL هم‌خوان است", tikka_kalam.TOTAL == len(texts))


def test_one_user_gets_all_texts_before_any_repeat():
    print("\n### ۲ برای یک کاربر هیچ متنی تکرار نمی‌شود تا همه بیایند")
    fresh_state()
    seen = []
    for _ in range(tikka_kalam.TOTAL):
        seen.append(tikka_kalam.next_tikka(CHAT, 1001))
    check("همهٔ متن‌ها یک بار آمدند", len(set(seen)) == tikka_kalam.TOTAL,
          f"-> {len(set(seen))} از {tikka_kalam.TOTAL}")
    check("هیچ تکراری در دورهٔ اول نبود", len(seen) == len(set(seen)))
    # دورهٔ دوم هم باید کامل و بدون تکرار باشد
    second = [tikka_kalam.next_tikka(CHAT, 1001)
              for _ in range(tikka_kalam.TOTAL)]
    check("دورهٔ دوم هم همهٔ متن‌ها را پوشش داد",
          len(set(second)) == tikka_kalam.TOTAL,
          f"-> {len(set(second))}")
    status = tikka_kalam.stats(CHAT, 1001)
    check("دو دوره = دو برابر تعداد کل برداشت",
          status["draws"] == 2 * tikka_kalam.TOTAL, f"-> {status}")


def test_two_users_do_not_get_the_same_line_at_once():
    print("\n### ۳ دو کاربر هم‌زمان یک جمله نمی‌گیرند")
    fresh_state()
    draws = []
    for index in range(30):
        user_id = 2000 + (index % 2)   # دو کاربر یکی‌درمیان
        draws.append(tikka_kalam.next_tikka(CHAT, user_id))
    check("هیچ جمله‌ای در ۳۰ برداشت پشت‌سرهم تکرار نشد",
          len(set(draws)) == len(draws), f"-> {len(draws) - len(set(draws))} تکرار")
    # ۲۰ کاربر هم‌زمان: باید ۲۰ جملهٔ متفاوت بگیرند
    many = [tikka_kalam.next_tikka(CHAT, 5000 + offset) for offset in range(20)]
    check("۲۰ کاربر مختلف ۲۰ جملهٔ متفاوت گرفتند",
          len(set(many)) == 20, f"-> {len(set(many))}")


def _simulate_restart():
    """همهٔ حافظهٔ درون‌پروسه پاک می‌شود؛ فقط فایل روی دیسک می‌ماند."""
    tikka_kalam._STATE.clear()
    tikka_kalam._STATE.update({"users": {}, "recent": []})
    tikka_kalam._LOADED[0] = False


def test_state_survives_restart():
    print("\n### ۴ وضعیت بعد از ری‌استارت/ریست ادامه پیدا می‌کند")
    tmp = fresh_state()
    first = [tikka_kalam.next_tikka(CHAT, 3001) for _ in range(10)]
    _simulate_restart()
    tikka_kalam.set_state_path(tmp)
    rest = [tikka_kalam.next_tikka(CHAT, 3001)
            for _ in range(tikka_kalam.TOTAL - 10)]
    all_draws = first + rest
    check("بعد از ری‌استارت هم هیچ تکراری نبود",
          len(set(all_draws)) == tikka_kalam.TOTAL,
          f"-> {len(set(all_draws))} از {tikka_kalam.TOTAL}")
    check("فایل وضعیت روی دیسک هست", tmp.exists(), f"-> {tmp}")


def test_never_repeats_even_after_many_requests():
    print("\n### ۵ حتی با صدها بار پرسیدن و ریست، تکرار نمی‌شود")
    tmp = fresh_state()
    draws = []
    for index in range(300):
        if index % 37 == 0:          # شبیه‌سازی ری‌استارت‌های پیاپی
            _simulate_restart()
            tikka_kalam.set_state_path(tmp)
        draws.append(tikka_kalam.next_tikka(CHAT, 6001))
    blocks_ok = True
    for start in range(0, len(draws) - tikka_kalam.TOTAL + 1,
                       tikka_kalam.TOTAL):
        block = draws[start:start + tikka_kalam.TOTAL]
        if len(set(block)) != len(block):
            blocks_ok = False
            break
    check("در هر دورهٔ کامل هیچ جمله‌ای تکرار نشد", blocks_ok,
          f"-> {len(draws)} برداشت")
    first_cycle = draws[:tikka_kalam.TOTAL]
    check("دورهٔ اول کامل و بدون تکرار بود",
          len(set(first_cycle)) == tikka_kalam.TOTAL)
    status = tikka_kalam.stats(CHAT, 6001)
    check("شمارندهٔ دوره‌ها درست چرخید",
          status["cycle"] == 300 // tikka_kalam.TOTAL + 1, f"-> {status}")


def test_bot_knows_what_it_told_the_user():
    print("\n### ۶ ربات می‌داند به هر کاربر چه تیکه‌هایی گفته است")
    tmp = fresh_state()
    told = [tikka_kalam.next_tikka(CHAT, 7001) for _ in range(12)]
    remembered = tikka_kalam.history(CHAT, 7001)
    check("تاریخچه دقیقاً همان جمله‌های گفته‌شده است",
          remembered[-12:] == told, f"-> {remembered[-3:]}")
    check("حافظه بین دو کاربر قاطی نمی‌شود",
          tikka_kalam.history(CHAT, 7002) == [])
    status = tikka_kalam.stats(CHAT, 7001)
    check("تعداد «گفته‌شده» ثبت شده", status["used"] == 12, f"-> {status}")
    check("تعداد باقی‌مانده درست است",
          status["remaining"] == tikka_kalam.TOTAL - 12, f"-> {status}")
    import json as _json
    payload = _json.loads(tmp.read_text(encoding="utf-8"))
    check("فایل وضعیت نسخهٔ ۲ و شامل used است",
          payload.get("version") == 2
          and len(payload["users"][f"{CHAT}:7001"]["used"]) == 12,
          f"-> {list(payload)[:4]}")
    check("ربات در استفادهٔ عادی هیچ‌وقت فایل را پاک نمی‌کند",
          tikka_kalam.__file__ and
          "unlink" not in (ROOT / "modules" / "tikka_kalam.py")
          .read_text(encoding="utf-8").split("def reset_all")[0])


def test_command_sends_bold_text():
    print("\n### ۷ دستور «تیکه کلام» پاسخ Bold می‌دهد")

    async def scenario():
        import handlers.message_handler as message_handler
        from test_court_join_all_users import build_handler_bot

        fresh_state()
        bot = build_handler_bot(set(), game_answer=False)
        event = Ev("تیکه کلام", 4001, message_id=11)
        await message_handler.handle_new_message(bot, event)
        check("دستور پاسخ گرفت", bool(event.replies), f"-> {event.replies}")
        if not event.replies:
            return
        reply = event.replies[0]
        check("عنوان تیکه کلام در پاسخ هست", "تیکه کلام" in reply,
              f"-> {reply!r}")
        check("یکی از متن‌های فهرست در پاسخ آمده",
              any(text in reply for text in tikka_kalam.TIKKAS),
              f"-> {reply!r}")
        entities = event.entities[0] if event.entities else []
        covers_all = any(
            getattr(entity, "offset", None) == 0
            and getattr(entity, "length", 0) >= len(reply) - 2
            for entity in entities
        )
        check("کل متن با Bold فرستاده شد (entity سروش)", covers_all,
              f"-> {entities} len={len(reply)}")
        check("Markdown خام در متن نیست",
              "**" not in reply and "__" not in reply, f"-> {reply!r}")

        # دو کاربر پشت سر هم: پاسخ‌ها متفاوت‌اند
        second = Ev("تیکه کلام", 4002, message_id=12)
        await message_handler.handle_new_message(bot, second)
        check("کاربر دوم جملهٔ متفاوتی گرفت",
              second.replies and second.replies[0] != reply,
              f"-> {second.replies[:1]}")
        check("هیچ خطایی در لاگ نیست", not bot.logger.errors,
              f"-> {bot.logger.errors[-2:]}")

    asyncio.run(scenario())


def test_guide_mentions_the_command():
    print("\n### ۸ راهنمای روباه دستور را معرفی می‌کند")
    source = (ROOT / "handlers" / "message_handler.py").read_text(encoding="utf-8")
    check("در راهنما نوشته «برای دریافت تیکه کلام بنویسید»",
          "برای دریافت تیکه کلام بنویسید" in source)
    check("دستور «تیکه کلام» به راهنما اضافه شده",
          '"تیکه کلام\\n\\n"' in source or "تیکه کلام\\n\\n" in source)
    check("بخش راهنما Bold می‌شود", '"🎭 تیکه کلام:"' in source)
    dispatch = (ROOT / "modules" / "group_dispatch.py").read_text(encoding="utf-8")
    check("دستور روی خط «دستور» جابجایی پیام است",
          '"تیکه کلام"' in dispatch)
    module = (ROOT / "modules" / "tikka_kalam.py").read_text(encoding="utf-8")
    check("ماژول هیچ تماس شبکه‌ای ندارد",
          "urllib" not in module and "https://" not in module)


def main():
    print("=" * 58)
    print("🎭 تست «تیکه کلام» (تصادفی، بدون تکرار، Bold)")
    print("=" * 58)
    test_texts_are_clean_and_unique()
    test_one_user_gets_all_texts_before_any_repeat()
    test_two_users_do_not_get_the_same_line_at_once()
    test_state_survives_restart()
    test_never_repeats_even_after_many_requests()
    test_bot_knows_what_it_told_the_user()
    test_command_sends_bold_text()
    test_guide_mentions_the_command()
    print("\n" + "=" * 58)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 58)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
