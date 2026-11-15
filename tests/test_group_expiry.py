"""⏳ تاریخ انقضای گروه — تست کامل و مستقل.

پوشش: ثبت، جایگزینی تاریخ، ماندگاری پس از ری‌استارت، انقضای خودکار،
غیرفعال شدن گروه، فعال‌سازی مجدد، دسترسی مالک، و نبود تداخل با هر
سیستم دیگر.

    python tests/test_group_expiry.py
"""
import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import handlers.group_expiry_handler as geh
import modules.group_expiry as ge
import modules.owner_check as oc

PASSED = FAILED = 0
CHAT = -1001234567890
OWNER_ID = oc.get_owner()["user_id"]


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class User:
    def __init__(self, uid, name="U"):
        self.id = uid
        self.first_name = name
        self.last_name = None
        self.username = None


class Chat:
    def __init__(self, title="گروه تست"):
        self.title = title


class Event:
    def __init__(self, private=False, title="گروه تست"):
        self.out = []
        self.entities = []
        self.is_private = private
        self._chat = Chat(title)

    async def reply(self, text, formatting_entities=None, **kwargs):
        self.out.append(text)
        self.entities.append(formatting_entities or [])
        return None

    async def get_chat(self):
        return self._chat

    def said(self, needle):
        return any(needle in m for m in self.out)


class Client:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, target, text, formatting_entities=None, **kw):
        if self.fail:
            raise ValueError("cannot resolve peer")
        self.sent.append((target, text, formatting_entities or []))
        return True


class Logger:
    def __init__(self):
        self.info, self.errors = [], []

    def log_info(self, m):
        self.info.append(m)

    def log_error(self, m):
        self.errors.append(m)

    def has(self, needle):
        return any(needle in m for m in self.info + self.errors)


class Bot:
    def __init__(self, client=None):
        self.client = client or Client()
        self.logger = Logger()


def use_temp_file():
    """هر تست روی فایل تازهٔ خودش کار می‌کند."""
    ge.FILE = Path(tempfile.mkdtemp()) / "group_expiry.json"
    ge._cache = None
    ge._cache_mtime = None


def decode_span(text, offset, length):
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


# ===========================================================================
# تطبیق دستور
# ===========================================================================
def test_command_matching():
    print("\n### 🎯 تطبیق دقیق چهار دستور")
    check("«۵ روز» تطبیق می‌کند", ge.match_command("۵ روز") == "۵ روز")
    check("«یک هفته» تطبیق می‌کند", ge.match_command("یک هفته") == "یک هفته")
    check("«دو هفته» تطبیق می‌کند", ge.match_command("دو هفته") == "دو هفته")
    check("«یک ماه» تطبیق می‌کند", ge.match_command("یک ماه") == "یک ماه")
    check("«۱ روز» تطبیق می‌کند", ge.match_command("۱ روز") == "۱ روز")
    check("مدت «۱ روز» ۱ روز است", ge.duration_days("۱ روز") == 1)
    check("«۴۰ روز» تطبیق می‌کند", ge.match_command("۴۰ روز") == "۴۰ روز")
    check("مدت «۴۰ روز» ۴۰ روز است", ge.duration_days("۴۰ روز") == 40)
    check("دستورها شامل ۱ تا ۴۰ روز هستند", len(ge.COMMANDS) >= 40)

    check("مدت «۵ روز» ۵ روز است", ge.duration_days("۵ روز") == 5)
    check("مدت «یک هفته» ۷ روز است", ge.duration_days("یک هفته") == 7)
    check("مدت «دو هفته» ۱۴ روز است", ge.duration_days("دو هفته") == 14)
    check("مدت «یک ماه» ۲۹ روز است", ge.duration_days("یک ماه") == 29)

    check("نیم‌فاصله پذیرفته می‌شود",
          ge.match_command("یک\u200cهفته") == "یک هفته")
    check("فاصلهٔ اضافه پذیرفته می‌شود",
          ge.match_command("  دو   هفته  ") == "دو هفته")
    check("ی و ک عربی پذیرفته می‌شود",
          ge.match_command("ي\u0643 ماه".replace("\u0643", "ك")) is not None
          or ge.match_command("یك ماه") == "یک ماه")


def test_no_prefix_collision():
    """هیچ startswith عمومی نباید این دستورها را بلعد یا بسازد."""
    print("\n### 🎯 نبود تداخل با دستورهای دیگر")
    for text in (
        "یک هفته دیگر", "ثبت یک ماه", "دو هفته مانده", "یک ماهه",
        "هفته", "ماه", "یک", "دو", "ثبت اسم یک هفته", "قفل یک ماه",
        "یک هفتهگی", "حذف دو هفته",
    ):
        check(f"«{text}» تطبیق نمی‌کند", ge.match_command(text) is None,
              f"-> {ge.match_command(text)!r}")

    # و برعکس: دستورهای دیگر ربات نباید به این مسیر بیفتند
    for other in ("اسم فامیل", "حدس ایموجی", "چیستان", "بقا", "قفل",
                  "باز", "فعال", "غیر فعال", "ثبت ادمین", "راهنما"):
        check(f"دستور «{other}» وارد مسیر انقضا نمی‌شود",
              ge.match_command(other) is None)


# ===========================================================================
# ثبت و جایگزینی
# ===========================================================================
def test_set_and_replace():
    print("\n### 📝 ثبت و جایگزینی تاریخ")
    use_temp_file()
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    result = ge.set_expiry(CHAT, "یک هفته", title="گ", now=now)
    check("ثبت موفق بود", result is not None)
    check("مدت ۷ روز است",
          (result["expires_at"] - result["activated_at"]).days == 7)
    check("انقضا دقیقاً ۷ روز بعد است",
          result["expires_at"] == now + timedelta(days=7))
    check("رکورد ذخیره شد", ge.has_expiry(CHAT))

    later = now + timedelta(days=2)
    replaced = ge.set_expiry(CHAT, "یک ماه", title="گ", now=later)
    check("جایگزینی موفق بود", replaced is not None)
    check("مدت جدید ۲۹ روز است",
          (replaced["expires_at"] - replaced["activated_at"]).days == 29)
    check("تاریخ فعال‌سازی از لحظهٔ جدید است",
          ge.activated_at(CHAT) == later)
    check("انقضای قبلی جایگزین شد",
          ge.expires_at(CHAT) == later + timedelta(days=29))
    check("فقط یک رکورد برای این گروه هست", len(ge.all_records()) == 1)

    check("دستور نامعتبر ثبت نمی‌شود",
          ge.set_expiry(CHAT, "سه هفته", now=now) is None)


def test_per_group_isolation():
    print("\n### 📝 جدا بودن گروه‌ها")
    use_temp_file()
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    ge.set_expiry(-1001111111111, "یک هفته", now=now)
    ge.set_expiry(-1002222222222, "یک ماه", now=now)

    check("گروه اول ۷ روز دارد",
          ge.expires_at(-1001111111111) == now + timedelta(days=7))
    check("گروه دوم ۲۹ روز دارد",
          ge.expires_at(-1002222222222) == now + timedelta(days=29))
    check("گروه سوم هیچ رکوردی ندارد", not ge.has_expiry(-1003333333333))
    check("دو رکورد مستقل ذخیره شد", len(ge.all_records()) == 2)

    # شکل کوتاه و -100 باید به یک رکورد نگاشت شوند
    check("شناسهٔ کوتاه همان رکورد را می‌دهد",
          ge.expires_at(1111111111) == ge.expires_at(-1001111111111))


def test_persistence_across_restart():
    print("\n### 💾 ماندگاری پس از ری‌استارت")
    use_temp_file()
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, "دو هفته", title="گروه من", now=now)
    expected = ge.expires_at(CHAT)
    path = ge.FILE

    check("فایل روی دیسک ساخته شد", path.exists())

    # شبیه‌سازی ری‌استارت: کش پاک، ماژول از صفر می‌خواند
    ge._cache = None
    ge._cache_mtime = None
    check("پس از ری‌استارت رکورد باقی است", ge.has_expiry(CHAT))
    check("تاریخ انقضا تغییر نکرد", ge.expires_at(CHAT) == expected)
    check("عنوان گروه حفظ شد", ge.get_record(CHAT).get("title") == "گروه من")
    check("مدت ذخیره‌شده ۱۴ روز است", ge.get_record(CHAT)["days"] == 14)

    # فایل خراب نباید ربات را بشکند
    path.write_text("{ this is not json", encoding="utf-8")
    ge._cache = None
    ge._cache_mtime = None
    check("فایل خراب باعث خطا نمی‌شود", ge.all_records() == {})
    check("گروه پس از خرابی فایل منقضی شمرده نمی‌شود", not ge.is_expired(CHAT))


# ===========================================================================
# انقضا
# ===========================================================================
def test_expiry_timing():
    print("\n### ⏰ محاسبهٔ دقیق انقضا")
    use_temp_file()
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, "یک هفته", now=now)

    check("یک ثانیه قبل، منقضی نیست",
          not ge.is_expired(CHAT, now=now + timedelta(days=7, seconds=-1)))
    check("دقیقاً در لحظهٔ انقضا، منقضی است",
          ge.is_expired(CHAT, now=now + timedelta(days=7)))
    check("بعد از انقضا، منقضی است",
          ge.is_expired(CHAT, now=now + timedelta(days=8)))
    check("گروه بدون رکورد هرگز منقضی نیست",
          not ge.is_expired(-1009999999999))

    left = ge.seconds_left(CHAT, now=now + timedelta(days=6))
    check("ثانیهٔ باقی‌مانده درست است", abs(left - 86400) < 1, f"-> {left}")
    check("پس از انقضا باقی‌مانده صفر است",
          ge.seconds_left(CHAT, now=now + timedelta(days=9)) == 0)


def test_due_groups_and_notify():
    print("\n### ⏰ فهرست گروه‌های منقضی و جلوگیری از اعلام تکراری")
    use_temp_file()
    past = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ge.set_expiry(-1001111111111, "یک هفته", now=past)
    ge.set_expiry(-1002222222222, "یک ماه",
                  now=datetime(2026, 7, 30, tzinfo=timezone.utc))

    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    due = ge.due_groups(now=now)
    check("فقط گروه منقضی‌شده در فهرست است", len(due) == 1, f"-> {len(due)}")
    check("گروه درست انتخاب شد", due[0][0] == "1111111111")

    check("علامت‌گذاری موفق بود", ge.mark_notified("1111111111"))
    check("پس از علامت‌گذاری دیگر در فهرست نیست",
          ge.due_groups(now=now) == [])
    check("علامت‌گذاری دوباره اثری ندارد",
          ge.mark_notified("1111111111") is False)
    check("پرچم اعلام ذخیره شد", ge.was_notified(-1001111111111))

    # تمدید باید پرچم را پاک کند
    ge.set_expiry(-1001111111111, "یک هفته", now=now)
    check("پس از تمدید، پرچم اعلام پاک شد",
          not ge.was_notified(-1001111111111))


# ===========================================================================
# پیام‌ها
# ===========================================================================
def test_confirmation_message():
    print("\n### 💬 پیام تأیید و entity ها")
    activated = datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)
    expires = activated + timedelta(days=29)
    text, spans = ge.build_confirmation(activated, expires)

    check("سرصفحه درست است", text.startswith("✅ تاریخ انقضای گروه تنظیم شد."))
    check("برچسب فعال‌سازی موجود است", "📅 تاریخ فعال‌سازی:" in text)
    check("برچسب انقضا موجود است", "⏳ تاریخ انقضا:" in text)

    kinds = [k for k, _, _ in spans]
    check("دو blockquote دارد", kinds.count("blockquote") == 2)
    check("دو bold دارد", kinds.count("bold") == 2)

    fragments = [decode_span(text, o, l) for _, o, l in spans]
    check("هر چهار span روی تاریخ‌ها می‌افتد",
          all("ساعت" in f and "/" in f for f in fragments),
          f"-> {fragments}")
    check("blockquote و bold روی یک بازه‌اند",
          fragments[0] == fragments[1] and fragments[2] == fragments[3])
    check("تاریخ فعال‌سازی و انقضا متفاوت‌اند", fragments[0] != fragments[2])
    check("ارقام فارسی هستند", any(d in fragments[0] for d in "۰۱۲۳۴۵۶۷۸۹"))


def test_expired_message():
    print("\n### 💬 پیام غیرفعال‌سازی خودکار")
    text, spans = ge.build_expired_message()
    check("متن دقیقاً مطابق خواسته است",
          text == ("⛔️ مهلت فعالیت ربات در این گروه به پایان رسید برای "
                   "تمدید اشتراک دستور مهلت گروه را ارسال کنید"),
          f"-> {text}")
    check("کل متن bold است", len(spans) == 1 and spans[0][0] == "bold")
    check("bold کل متن را می‌پوشاند",
          decode_span(text, spans[0][1], spans[0][2]) == text)


# ===========================================================================
# هندلر
# ===========================================================================
def test_owner_only():
    print("\n### 🔐 فقط مالک اصلی")
    use_temp_file()

    async def run(uid):
        bot, event = Bot(), Event()
        consumed = await geh.handle(
            bot, event, CHAT, User(uid), "یک هفته", bot.logger)
        return consumed, event, bot

    consumed, event, bot = asyncio.run(run(OWNER_ID))
    check("مالک: پیام مصرف شد", consumed is True)
    check("مالک: تأیید ارسال شد", event.said("تاریخ انقضای گروه تنظیم شد"))
    check("مالک: رکورد ثبت شد", ge.has_expiry(CHAT))
    check("مالک: ثبت لاگ شد", bot.logger.has("GROUP EXPIRY SET"))

    use_temp_file()
    consumed, event, bot = asyncio.run(run(999999))
    check("غیرمالک: پیام مصرف شد و جلوتر نمی‌رود", consumed is True)
    check("غیرمالک: هیچ پاسخی داده نشد", event.out == [], f"-> {event.out}")
    check("غیرمالک: هیچ رکوردی ثبت نشد", not ge.has_expiry(CHAT))
    check("غیرمالک: رد شدن لاگ شد",
          bot.logger.has("reason=not_global_owner"))


def test_handler_ignores_other_text():
    print("\n### 🔐 هندلر متن‌های دیگر را دست نمی‌زند")
    use_temp_file()

    async def run(text):
        bot, event = Bot(), Event()
        return await geh.handle(
            bot, event, CHAT, User(OWNER_ID), text, bot.logger), event

    for text in ("اسم فامیل", "یک هفته دیگر", "چیستان", "سلام", "فعال"):
        consumed, event = asyncio.run(run(text))
        check(f"«{text}» مصرف نمی‌شود", consumed is False)
        check(f"«{text}» پاسخی تولید نمی‌کند", event.out == [])
    check("هیچ رکوردی ساخته نشد", not ge.has_expiry(CHAT))


def test_handler_entities_are_real():
    print("\n### 💬 entity های واقعی splusthon")
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
    use_temp_file()

    async def run():
        bot, event = Bot(), Event()
        await geh.handle(bot, event, CHAT, User(OWNER_ID),
                         "دو هفته", bot.logger)
        return event

    event = asyncio.run(run())
    entities = event.entities[0]
    check("چهار entity ساخته شد", len(entities) == 4, f"-> {len(entities)}")
    check("دو blockquote واقعی",
          sum(isinstance(e, MessageEntityBlockquote) for e in entities) == 2)
    check("دو bold واقعی",
          sum(isinstance(e, MessageEntityBold) for e in entities) == 2)


# ===========================================================================
# مسدودسازی و فعال‌سازی مجدد
# ===========================================================================
def test_blocks_message_after_expiry():
    print("\n### 🚫 توقف قابلیت‌ها پس از انقضا")
    use_temp_file()
    past = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, "یک هفته", now=past)

    check("گروه منقضی است", ge.is_expired(CHAT))
    check("کاربر عادی مسدود می‌شود",
          geh.blocks_message(CHAT, User(555)) is True)
    check("مالک اصلی مسدود نمی‌شود",
          geh.blocks_message(CHAT, User(OWNER_ID)) is False)

    use_temp_file()
    ge.set_expiry(CHAT, "یک ماه")
    check("گروه فعال: کاربر عادی مسدود نمی‌شود",
          geh.blocks_message(CHAT, User(555)) is False)
    check("گروه بدون رکورد: مسدود نمی‌شود",
          geh.blocks_message(-1008888888888, User(555)) is False)


def test_reactivation():
    print("\n### 🔄 فعال‌سازی مجدد توسط مالک")
    use_temp_file()
    past = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, "یک هفته", now=past)
    ge.mark_notified(CHAT)
    check("گروه منقضی و اعلام‌شده است",
          ge.is_expired(CHAT) and ge.was_notified(CHAT))

    async def run():
        bot, event = Bot(), Event()
        await geh.handle(bot, event, CHAT, User(OWNER_ID),
                         "یک ماه", bot.logger)
        return event

    event = asyncio.run(run())
    check("تأیید تمدید ارسال شد", event.said("تاریخ انقضای گروه تنظیم شد"))
    check("گروه دیگر منقضی نیست", not ge.is_expired(CHAT))
    check("پرچم اعلام پاک شد", not ge.was_notified(CHAT))
    check("کاربر عادی دیگر مسدود نیست",
          geh.blocks_message(CHAT, User(555)) is False)
    check("مدت جدید ۲۹ روز است", ge.get_record(CHAT)["days"] == 29)


# ===========================================================================
# ناظر پس‌زمینه
# ===========================================================================
def test_watcher_deactivates():
    print("\n### 🤖 غیرفعال‌سازی خودکار بدون هیچ پیامی")
    use_temp_file()
    past = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ge.set_expiry(-1001111111111, "یک هفته", title="گروه الف", now=past)
    ge.set_expiry(-1002222222222, "یک ماه", title="گروه ب")  # هنوز فعال

    deactivated = []

    def deactivate(chat_id, title):
        deactivated.append((chat_id, title))

    bot = Bot()
    closed = asyncio.run(geh.check_once(bot, deactivate, logger=bot.logger))

    check("دقیقاً یک گروه بسته شد", closed == 1, f"-> {closed}")
    check("گروه درست غیرفعال شد",
          len(deactivated) == 1 and deactivated[0][1] == "گروه الف",
          f"-> {deactivated}")
    check("پیام اعلام ارسال شد", len(bot.client.sent) == 1)
    check("متن پیام درست است",
          bot.client.sent[0][1] == ge.EXPIRED_MESSAGE)
    check("پیام bold است", len(bot.client.sent[0][2]) == 1)
    check("غیرفعال‌سازی لاگ شد", bot.logger.has("GROUP EXPIRY DEACTIVATED"))
    check("گروه فعال دست نخورد",
          all(c[0] != -1002222222222 for c in deactivated))

    # اجرای دوباره نباید تکرار کند
    bot2 = Bot()
    again = asyncio.run(geh.check_once(bot2, deactivate, logger=bot2.logger))
    check("بار دوم چیزی بسته نمی‌شود", again == 0)
    check("پیام تکراری ارسال نمی‌شود", bot2.client.sent == [])


def test_watcher_survives_send_failure():
    print("\n### 🤖 شکست ارسال پیام ناظر را متوقف نمی‌کند")
    use_temp_file()
    past = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, "یک هفته", now=past)

    deactivated = []
    bot = Bot(Client(fail=True))
    closed = asyncio.run(geh.check_once(
        bot, lambda c, t: deactivated.append(c), logger=bot.logger))

    check("گروه با وجود شکست ارسال، غیرفعال شد", len(deactivated) == 1)
    check("گروه شمرده شد", closed == 1)
    check("شکست ارسال لاگ شد", bot.logger.has("NOTICE FAILED"))
    check("گروه دوباره اعلام نمی‌شود", ge.was_notified(CHAT))


def test_watcher_loop_runs():
    print("\n### 🤖 حلقهٔ ناظر")
    use_temp_file()
    past = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, "یک هفته", now=past)
    deactivated = []
    bot = Bot()

    rounds = asyncio.run(geh.run_expiry_watcher(
        bot, lambda c, t: deactivated.append(c),
        interval=0.01, logger=bot.logger, iterations=3))
    check("حلقه سه بار اجرا شد", rounds == 3, f"-> {rounds}")
    check("گروه فقط یک بار بسته شد", len(deactivated) == 1,
          f"-> {deactivated}")

    # خطای deactivate نباید حلقه را بکشد
    use_temp_file()
    ge.set_expiry(CHAT, "یک هفته", now=past)

    def boom(chat_id, title):
        raise RuntimeError("db down")

    bot2 = Bot()
    rounds = asyncio.run(geh.run_expiry_watcher(
        bot2, boom, interval=0.01, logger=bot2.logger, iterations=2))
    check("حلقه با وجود خطا ادامه یافت", rounds == 2)
    check("خطای غیرفعال‌سازی لاگ شد",
          bot2.logger.has("DEACTIVATE FAILED"))


def test_end_to_end_lifecycle():
    """چرخهٔ کامل: ثبت → ری‌استارت → انقضا → مسدودی → فعال‌سازی مجدد."""
    print("\n### 🔁 چرخهٔ کامل عمر")
    use_temp_file()
    start = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)

    async def owner_sends(text):
        bot, event = Bot(), Event()
        await geh.handle(bot, event, CHAT, User(OWNER_ID), text, bot.logger)
        return bot, event

    # ۱) ثبت
    bot, event = asyncio.run(owner_sends("یک هفته"))
    check("۱) تاریخ ثبت شد", event.said("تاریخ انقضای گروه تنظیم شد"))
    check("۱) کاربر عادی مسدود نیست",
          not geh.blocks_message(CHAT, User(555)))

    # ۲) ری‌استارت
    ge._cache = None
    ge._cache_mtime = None
    check("۲) رکورد پس از ری‌استارت باقی است", ge.has_expiry(CHAT))

    # ۳) انقضا: تاریخ را به گذشته می‌بریم
    ge.set_expiry(CHAT, "یک هفته",
                  now=datetime(2026, 7, 1, tzinfo=timezone.utc))
    check("۳) گروه منقضی شد", ge.is_expired(CHAT))

    # ۴) ناظر گروه را می‌بندد
    closed_groups = []
    watcher_bot = Bot()
    asyncio.run(geh.check_once(
        watcher_bot, lambda c, t: closed_groups.append(c),
        logger=watcher_bot.logger))
    check("۴) گروه خودکار غیرفعال شد", len(closed_groups) == 1)
    check("۴) پیام پایان مهلت ارسال شد",
          watcher_bot.client.sent[0][1] == ge.EXPIRED_MESSAGE)

    # ۵) همه چیز متوقف است، جز مالک
    check("۵) کاربر عادی مسدود است", geh.blocks_message(CHAT, User(555)))
    check("۵) مالک عبور می‌کند",
          not geh.blocks_message(CHAT, User(OWNER_ID)))

    # ۶) فعال‌سازی مجدد
    bot, event = asyncio.run(owner_sends("دو هفته"))
    check("۶) تمدید انجام شد", event.said("تاریخ انقضای گروه تنظیم شد"))
    check("۶) گروه دیگر منقضی نیست", not ge.is_expired(CHAT))
    check("۶) کاربر عادی آزاد شد",
          not geh.blocks_message(CHAT, User(555)))
    check("۶) مدت جدید ۱۴ روز است", ge.get_record(CHAT)["days"] == 14)


# ===========================================================================
# استقلال کامل
# ===========================================================================
def test_full_independence():
    print("\n### 🔒 استقلال کامل از سایر سیستم‌ها")
    source = (ROOT / "modules" / "group_expiry.py").read_text(encoding="utf-8")
    forbidden = (
        "game", "coins", "emoji", "flag", "riddle", "fill_blank",
        "name_family", "group_memory", "group_actions", "lock",
        "multiple_choice", "word_correction", "fox_games", "spam",
    )
    leaked = [w for w in forbidden if f"import {w}" in source
              or f"from modules.{w}" in source]
    check("ماژول هیچ سیستم دیگری را import نمی‌کند", not leaked,
          f"-> {leaked}")
    # فقط import های واقعی مهم‌اند، نه اشاره در توضیحات.
    import ast as _ast
    tree = _ast.parse(source)
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    check("ماژول به splusthon وابسته نیست", "splusthon" not in imported,
          f"-> {sorted(imported)}")
    # تنها وابستگیِ داخلیِ پروژه، ماژولِ مرکزیِ زمان (time_utils) است.
    check("فقط کتابخانهٔ استاندارد + ماژولِ مرکزیِ زمان import می‌شود",
          imported <= {"json", "os", "tempfile", "datetime", "pathlib",
                       "zoneinfo", "modules", "re"},
          f"-> {sorted(imported)}")
    import re as _re
    _internal = _re.findall(r"from\s+modules\.([\w.]+)\s+import", source)
    check("تنها ماژولِ داخلیِ پروژه time_utils (مرکزیِ زمان) است",
          set(_internal) <= {"time_utils", "runtime_paths"}, f"-> {_internal}")

    check("فایل ذخیره‌سازی اختصاصی است",
          ge.FILE.name == "group_expiry.json"
          or "group_expiry" in str(ge.FILE))

    # هیچ ماژول دیگری در این فایل نمی‌نویسد
    import economy.storage as economy_storage
    import modules.group_storage as gs
    check("group_storage فایل دیگری دارد", gs.FILE.name == "groups.json")
    check("اقتصاد فایل دیگری دارد",
          economy_storage.DATA_FILE.name == "economy.json")

    # اجرای بازی‌ها نباید رکورد انقضا را تغییر دهد
    use_temp_file()
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, "یک ماه", now=now)
    before = ge.expires_at(CHAT)

    import modules.emoji_guess as eg
    import modules.riddles as rd
    from modules.fox_games import vampire as vp
    eg.reset_all()
    rd.reset_all()
    vp.reset_all()
    eg.start(CHAT, 1)
    rd.new_riddle(CHAT, 1)
    vp.start(CHAT)
    check("اجرای بازی‌ها تاریخ انقضا را تغییر نداد",
          ge.expires_at(CHAT) == before)

    eg.reset_all()
    rd.reset_all()
    vp.reset_all()
    check("ریست بازی‌ها رکورد انقضا را پاک نکرد", ge.has_expiry(CHAT))


def test_handler_independence():
    print("\n### 🔒 استقلال هندلر")
    source = (ROOT / "handlers" / "group_expiry_handler.py").read_text(
        encoding="utf-8")
    for forbidden in ("fox_games", "emoji_guess", "name_family",
                      "group_memory", "multiple_choice", "coins"):
        check(f"هندلر «{forbidden}» را import نمی‌کند",
              forbidden not in source)


def test_clear_expiry():
    print("\n### 🧹 حذف رکورد")
    use_temp_file()
    ge.set_expiry(CHAT, "یک هفته")
    check("رکورد وجود دارد", ge.has_expiry(CHAT))
    check("حذف موفق بود", ge.clear_expiry(CHAT) is True)
    check("رکورد پاک شد", not ge.has_expiry(CHAT))
    check("حذف دوباره False می‌دهد", ge.clear_expiry(CHAT) is False)
    check("گروه پاک‌شده منقضی شمرده نمی‌شود", not ge.is_expired(CHAT))


def main():
    test_command_matching()
    test_no_prefix_collision()
    test_set_and_replace()
    test_per_group_isolation()
    test_persistence_across_restart()
    test_expiry_timing()
    test_due_groups_and_notify()
    test_confirmation_message()
    test_expired_message()
    test_owner_only()
    test_handler_ignores_other_text()
    test_handler_entities_are_real()
    test_blocks_message_after_expiry()
    test_reactivation()
    test_watcher_deactivates()
    test_watcher_survives_send_failure()
    test_watcher_loop_runs()
    test_end_to_end_lifecycle()
    test_full_independence()
    test_handler_independence()
    test_clear_expiry()

    print("\n" + "=" * 52)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 52)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
