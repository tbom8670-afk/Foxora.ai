"""تستِ قابلیتِ «حالت بحرانی سپر محافظتی».

بدونِ هیچ درخواستِ شبکه‌ای واقعی: لایهٔ RPC با `FakeGroupActions` جایگزین
می‌شود و همهٔ storageها روی یک دایرکتوریِ موقت می‌روند.

اجرا:  python3 tests/test_emergency_shield.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "splusthon" not in sys.modules:
    fake = types.ModuleType("splusthon")
    fake.Button = object
    fake.types = types.ModuleType("splusthon.types")
    tl = types.ModuleType("splusthon.tl")
    tl_types = types.ModuleType("splusthon.tl.types")

    class _Bold:
        def __init__(self, offset=0, length=0, **_kwargs):
            self.offset = offset
            self.length = length

    class _Quote:
        def __init__(self, offset=0, length=0, **_kwargs):
            self.offset = offset
            self.length = length

    tl_types.MessageEntityBold = _Bold
    tl_types.MessageEntityBlockquote = _Quote
    fake.tl = tl
    tl.types = tl_types
    tl.functions = types.ModuleType("splusthon.tl.functions")
    sys.modules["splusthon"] = fake
    sys.modules["splusthon.tl"] = tl
    sys.modules["splusthon.tl.types"] = tl_types
    sys.modules["splusthon.tl.functions"] = tl.functions
    sys.modules["splusthon.types"] = fake.types

import modules.emergency_shield as es
import modules.group_banned_words_control as gbc
import modules.bio_link_storage as bls
import modules.warning_threshold as wt
import modules.punishment_mode as pm
import modules.spam_history as sh
from modules.ad_spam_guard import storage as ad_storage
from modules.profile_guard import storage as pg_storage

PASSED = FAILED = 0


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


def _is_entity(entity, name: str) -> bool:
    """تشخیصِ نوعِ موجودیت — در محیطِ واقعی ``splusthon`` نامِ کلاس همان
    است. در استابِ تست هم کلاس‌هایِ هم‌نام ساخته می‌شوند، ولی ``admin_help_builder``
    می‌تواند کلاس‌هایِ خودش را داشته باشد؛ پس علاوه بر نام، عضورت در
    ماژولِ کلاسِ هم‌نام را هم می‌پذیریم."""
    if getattr(entity, "entity_kind", None) == name:
        return True
    if type(entity).__name__ == name:
        return True
    module = sys.modules.get("splusthon.tl.types")
    cls = getattr(module, name, None) if module else None
    return cls is not None and isinstance(entity, cls)


# ---------------------------------------------------------------------------
# هارنس
# ---------------------------------------------------------------------------

class FakeLogger:
    def __init__(self):
        self.lines = []

    def log_info(self, message):
        self.lines.append(message)

    def log_error(self, message):
        self.lines.append("ERR " + message)


class FakeGroupActions:
    def __init__(self):
        self.locked = []
        self.unlocked = []

    async def lock_media(self, chat_id):
        self.locked.append(chat_id)
        return {"send_media": False, "send_gifs": False,
                "send_stickers": False, "send_messages": False}

    async def unlock_media(self, chat_id, snapshot=None):
        self.unlocked.append((chat_id, snapshot))
        return True


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return True


class FakeBot:
    def __init__(self):
        self.logger = FakeLogger()
        self.group_actions = FakeGroupActions()
        self.client = FakeClient()


class FakeReply:
    def __init__(self):
        self.texts = []
        self.entities = []

    async def reply(self, text, **kwargs):
        self.texts.append(text)
        self.entities.append(kwargs.get("formatting_entities"))
        return types.SimpleNamespace(id=1)


def fresh_storage():
    """همهٔ storageها را به یک دایرکتوریِ موقتِ **هم‌دستگاه** می‌برد.

    ``warning_threshold``/``punishment_mode`` فایلِ موقت را با
    ``tempfile.mkstemp(dir=_BASE)`` می‌سازند و ``os.replace`` می‌زنند؛ اگر
    مقصد روی یک دستگاهِ دیگر باشد خطای ``EXDEV`` می‌گیرند. برایِ همین
    دایرکتوریِ موقت، زیرِ همان پوشهٔ ``CONFIG_DIR`` ساخته می‌شود.
    """
    global _TMP
    from modules.runtime_paths import CONFIG_DIR
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TMP = tempfile.mkdtemp(
        prefix="emergency-shield-", dir=str(CONFIG_DIR)
    )
    targets = (
        (es, "FILE", es.FILE.name),
        (gbc, "FILE", "group_banned_words.json"),
        (bls, "FILE", "bio_link.json"),
        (wt, "_FILE", "warning_threshold.json"),
        (pm, "_FILE", "punishment_mode.json"),
        (ad_storage, "FILE", "ad_spam_guard.json"),
        (pg_storage, "FILE", "profile_guard.json"),
    )
    for module, attribute, name in targets:
        setattr(module, attribute, Path(_TMP) / name)
    wt._BASE = Path(_TMP)
    pm._BASE = Path(_TMP)
    for module in (es, gbc, bls):
        if hasattr(module, "reset_cache"):
            module.reset_cache()
    wt._cache = None
    wt._cache_mtime = None
    pm._cache = None
    pm._cache_mtime = None
    ad_storage._cache = None
    ad_storage._cache_mtime = None
    pg_storage._cache = None
    pg_storage._cache_mtime = None
    sh.MESSAGE_HISTORY.clear()


def cleanup_storage():
    es.reset_runtime_state()
    sh.MESSAGE_HISTORY.clear()
    if "_TMP" in globals():
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)


# ---------------------------------------------------------------------------
# ۱) دستورها و اعداد
# ---------------------------------------------------------------------------

def test_commands():
    print("\n### دستورها و اعداد")
    check("«سپر روشن 20» → ۲۰", es.parse_minutes("سپر روشن 20") == 20)
    check("«سپر روشن ۲۰» (فارسی) → ۲۰", es.parse_minutes("سپر روشن ۲۰") == 20)
    check("«سپر روشن 20 دقیقه» → ۲۰",
          es.parse_minutes("سپر روشن 20 دقیقه") == 20)
    check("«سپر روشن 1 ساعت» → ۶۰",
          es.parse_minutes("سپر روشن 1 ساعت") == 60)
    check("«سپر روشن ۱ ساعت» → ۶۰",
          es.parse_minutes("سپر روشن ۱ ساعت") == 60)
    check("«سپر روشن» بدونِ عدد → None", es.parse_minutes("سپر روشن") is None)
    check("«سپر روشن سلام» → None",
          es.parse_minutes("سپر روشن سلام") is None)

    check("«سپر روشن» → ask", es.match_command("سپر روشن") == "ask")
    check("«سپر خاموش» → off", es.match_command("سپر خاموش") == "off")
    check("«سپر روشن 20» → on", es.match_command("سپر روشن 20") == "on")
    check("«.سپر خاموش» → off", es.match_command(".سپر خاموش") == "off")
    check("متنِ نامرتبط → None", es.match_command("سلام") is None)

    check("۲۰ دقیقه", es.format_duration(20) == "20 دقیقه")
    check("۱ ساعت", es.format_duration(60) == "1 ساعت")
    check("۱ ساعت و ۳۰ دقیقه", es.format_duration(90) == "1 ساعت و 30 دقیقه")


# ---------------------------------------------------------------------------
# ۲) قالب‌هایِ پیام — عیناً طبقِ خواسته
# ---------------------------------------------------------------------------

def test_templates():
    print("\n### قالب‌هایِ پیام")
    body, entities = es.build_on_message(
        duration="1 ساعت", actor="@osine", chat_id="123456"
    )
    lines = body.split("\n")
    check("تیترِ روشن‌شدن", lines[0] == "«حالت بحرانی » سپر روشن شد ✅")
    check("شش خطِ وضعیت", lines[2:8] == list(es.STATUS_LINES))
    check("مدتِ زمان", lines[9] == "⚙ مـدت زمان فعال بودن : 1 ساعت")
    check("تنظیم‌کننده", lines[10] == "⚙ تنـظیم کننده : @osine")
    check("شناسهٔ گروه", lines[11] == "⚙ شـناسه گروه : 123456")
    quotes = [e for e in entities if _is_entity(e, "MessageEntityBlockquote")]
    bolds = [e for e in entities if _is_entity(e, "MessageEntityBold")]
    check("تیتر داخل نقل‌قول", any(q.offset == 0 for q in quotes))
    check("تیتر Bold", any(b.offset == 0 for b in bolds))
    check("بلوکِ وضعیت یک نقل‌قولِ واحد", len(quotes) == 2)
    body_len = es._u16(body)
    check("طولِ موجودیت‌ها در محدوده",
          all(0 <= e.offset and e.offset + e.length <= body_len
              for e in entities))
    check("نقل‌قولِ وضعیت شش‌خطِ کامل را می‌گیرد",
          any(q.offset == es._u16("«حالت بحرانی » سپر روشن شد ✅\n\n")
              and q.length == es._u16("\n".join(es.STATUS_LINES))
              for q in quotes))

    ask, ask_entities = es.build_ask_message()
    ask_lines = ask.split("\n")
    check("تیترِ درخواستِ تایم", ask_lines[0] == "«حالت بحرانی » سپر محافظتی")
    check("خطِ راهنما", ask_lines[2] == es.ASK_LINE)
    check("مثال‌ها", ask_lines[4:6] == list(es.ASK_EXAMPLES))
    ask_quotes = [e for e in ask_entities
                  if _is_entity(e, "MessageEntityBlockquote")]
    check("مثال‌ها در یک نقل‌قولِ واحد",
          any(q.length == es._u16("\n".join(es.ASK_EXAMPLES))
              for q in ask_quotes))
    check("طولِ موجودیت‌های پیامِ ۲ در محدوده",
          all(0 <= e.offset and e.offset + e.length <= es._u16(ask)
              for e in ask_entities))

    off, _ = es.build_off_message()
    check("پیامِ خاموش‌شدن", off == es.OFF_TEXT)
    check("متنِ خاموش‌شدن دقیق",
          off == "⚙️ : سپر محافظتی خاموش شد تنظیمات مدیریتی به قبل بازگشت "
                 "حالت مدیریت عادی")


# ---------------------------------------------------------------------------
# ۳) snapshot / اعمال / بازگشت
# ---------------------------------------------------------------------------

def test_snapshot_and_restore():
    print("\n### snapshot و بازگشت")
    fresh_storage()
    chat = -100123
    try:
        # وضعیتِ اولیه: همه خاموش جز کلمات ممنوعه (پیش‌فرض روشن)
        gbc.disable(chat)
        pg_storage.disable(chat)
        ad_storage.disable(chat)
        bls.set_enabled(chat, False)
        wt.set_threshold(chat, 5)
        pm.set_mode(chat, pm.MODE_BAN)

        bot = FakeBot()
        record = asyncio.run(es.activate(bot, chat, 20, actor=types.SimpleNamespace(
            id=1, username="osine", first_name="Ali")))
        check("رکورد فعال است", es.is_active(chat) is True)
        check("کلمات ممنوعه روشن", gbc.is_enabled(chat) is True)
        check("ضد کانال روشن", pg_storage.is_enabled(chat) is True)
        check("تبلیغ‌گر روشن", ad_storage.is_enabled(chat) is True)
        check("بیو-لینک روشن", bls.is_enabled(chat) is True)
        check("آستانهٔ اخطار ۱", wt.get_threshold(chat) == 1)
        check("مجازات سکوت", pm.is_mute(chat) is True)
        check("قفلِ رسانه اعمال شد", bot.group_actions.locked == [chat])
        check("snapshot حقوقِ رسانه دارد",
              isinstance(record["snapshot"].get("media"), dict))

        restored = asyncio.run(es.restore(bot, chat, reason="test"))
        check("بازگشت انجام شد", restored is True)
        check("دیگر فعال نیست", es.is_active(chat) is False)
        check("کلمات ممنوعه به خاموش برگشت", gbc.is_enabled(chat) is False)
        check("ضد کانال به خاموش برگشت", pg_storage.is_enabled(chat) is False)
        check("تبلیغ‌گر به خاموش برگشت", ad_storage.is_enabled(chat) is False)
        check("بیو-لینک به خاموش برگشت", bls.is_enabled(chat) is False)
        check("آستانهٔ اخطار به ۵ برگشت", wt.get_threshold(chat) == 5)
        check("مجازات به بن برگشت", pm.is_mute(chat) is False)
        check("قفلِ رسانه بازگشت", len(bot.group_actions.unlocked) == 1)
    finally:
        cleanup_storage()


def test_extend_keeps_snapshot():
    print("\n### تمدیدِ تایمر")
    fresh_storage()
    chat = -100456
    try:
        gbc.disable(chat)
        bot = FakeBot()
        asyncio.run(es.activate(bot, chat, 20))
        first = es.get_record(chat)["snapshot"]
        # بعد از روشن‌شدن، کاربر کلمات ممنوعه را خاموش می‌کند
        gbc.disable(chat)
        asyncio.run(es.activate(bot, chat, 5))
        second = es.get_record(chat)["snapshot"]
        check("snapshot بعد از تمدید دست‌نخورده است", first == second)
        check("snapshot مقدارِ اولیه را دارد",
              second.get("banned_words") is False)
    finally:
        cleanup_storage()


def test_expiry():
    print("\n### انقضایِ تایمر")
    fresh_storage()
    chat = -100789
    try:
        bot = FakeBot()
        record = asyncio.run(es.activate(bot, chat, 1))
        record["until"] = __import__("time").time() - 1
        es._store(chat, record)
        check("پس از انقضا فعال نیست", es.is_active(chat) is False)
        restored = asyncio.run(es.restore(bot, chat, reason="timer"))
        check("بازگشت پس از انقضا انجام می‌شود", restored is True)
    finally:
        cleanup_storage()


# ---------------------------------------------------------------------------
# ۴) لایهٔ اختصاصی
# ---------------------------------------------------------------------------

def test_borderline():
    print("\n### پیامِ مرزی")
    check("آیدیِ کوتاه", es.is_borderline("سلام @ab کجایی") is True)
    check("دامنهٔ بدونِ پیشوند", es.is_borderline("سلام یه سایت ببین gmail.com") is True)
    check("شمارهٔ موبایل", es.is_borderline("شماره 09123456789") is True)
    check("متنِ سالم", es.is_borderline("سلام دوستان چطورید") is False)
    check("متنِ خالی", es.is_borderline("") is False)
    check("پنجرهٔ سپر ۱۰ ثانیه", es.repeat_window(-1) == 30.0)
    fresh_storage()
    try:
        bot = FakeBot()
        asyncio.run(es.activate(bot, -100999, 20))
        check("در سپر پنجره ۱۰ ثانیه", es.repeat_window(-100999) == 10.0)
        check("در سپر حد ۳ پیام", es.repeat_limit(-100999) == 3)
    finally:
        cleanup_storage()


def test_repeat_window():
    print("\n### ضد تکرارِ سریع")
    fresh_storage()
    chat = -100321
    user = 42
    clock = {"t": 1000.0}
    real_time = sh.time
    sh.time = types.SimpleNamespace(monotonic=lambda: clock["t"])
    try:
        bot = FakeBot()
        # بدونِ سپر: ۳ پیام در کمتر از ۳۰ ثانیه → تکرار
        for index in range(3):
            sh.save_history_message(chat, user, index + 1, "سلام")
            clock["t"] += 10
        check("حالتِ عادی: ۳ پیام در ۳۰ ثانیه تکرار است",
              sh.is_repeat(chat, user, "سلام") is True)

        sh.MESSAGE_HISTORY.clear()
        asyncio.run(es.activate(bot, chat, 20))
        for index in range(3):
            sh.save_history_message(chat, user, index + 10, "سلام")
            clock["t"] += 3
        check("سپر: ۳ پیام در ۱۰ ثانیه تکرار است",
              sh.is_repeat(chat, user, "سلام") is True)

        sh.MESSAGE_HISTORY.clear()
        for index in range(2):
            sh.save_history_message(chat, user, index + 20, "سلام")
            clock["t"] += 3
        check("سپر: ۲ پیام تکراری نیست",
              sh.is_repeat(chat, user, "سلام") is False)
    finally:
        sh.time = real_time
        cleanup_storage()


# ---------------------------------------------------------------------------
# ۵) دستورهایِ متنی
# ---------------------------------------------------------------------------

def test_run_command():
    print("\n### دستورهایِ متنی")
    fresh_storage()
    chat = -100654
    try:
        bot = FakeBot()
        reply = FakeReply()
        event = types.SimpleNamespace(reply=reply.reply)
        actor = types.SimpleNamespace(id=1, username="osine", first_name="Ali")

        asyncio.run(es.run_command(bot, event, chat, "سپر روشن", actor=actor))
        check("پیامِ درخواستِ تایم داده شد", "سپر محافظتی" in reply.texts[0])

        reply2 = FakeReply()
        event2 = types.SimpleNamespace(reply=reply2.reply)
        asyncio.run(es.run_command(bot, event2, chat, "سپر روشن 1 ساعت",
                                   actor=actor))
        check("پیامِ روشن‌شدن داده شد", "سپر روشن شد" in reply2.texts[0])
        check("مدتِ زمان در پیام", "1 ساعت" in reply2.texts[0])
        check("تنظیم‌کننده در پیام", "@osine" in reply2.texts[0])
        check("گروه فعال شد", es.is_active(chat) is True)

        reply3 = FakeReply()
        event3 = types.SimpleNamespace(reply=reply3.reply)
        asyncio.run(es.run_command(bot, event3, chat, "سپر خاموش", actor=actor))
        check("پیامِ خاموش‌شدن داده شد", reply3.texts[0] == es.OFF_TEXT)
        check("گروه خاموش شد", es.is_active(chat) is False)
    finally:
        cleanup_storage()


# ---------------------------------------------------------------------------
# ۶) لیستِ ادمینی
# ---------------------------------------------------------------------------

def test_admin_help():
    print("\n### لیستِ ادمینی")
    from modules import admin_help_builder
    text, entities = admin_help_builder.build_admin_help()
    check("عنوانِ سپر در لیست است",
          "🌡 برای روشن کردن حالت بحرانی سپر محافظتی گروه بنویسید" in text)
    check("دستورِ روشن در لیست است", "سپر روشن" in text)
    check("دستورِ خاموش در لیست است", "سپر خاموش" in text)
    index = text.find("🌡 برای روشن کردن حالت بحرانی")
    block = text[index:]
    check("دو دستور زیرِ عنوان‌اند",
          block.find("سپر روشن") < block.find("سپر خاموش"))
    quotes = [e for e in entities if _is_entity(e, "MessageEntityBlockquote")]
    shield_quote = [
        q for q in quotes
        if q.offset >= index and q.offset < index + 200
    ]
    check("بلوکِ سپر نقل‌قول دارد", len(shield_quote) > 0)


# ---------------------------------------------------------------------------

def main():
    test_commands()
    test_templates()
    test_snapshot_and_restore()
    test_extend_keeps_snapshot()
    test_expiry()
    test_borderline()
    test_repeat_window()
    test_run_command()
    test_admin_help()
    print(f"\n{'=' * 46}")
    print(f"  PASSED: {PASSED}   FAILED: {FAILED}")
    print(f"{'=' * 46}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
