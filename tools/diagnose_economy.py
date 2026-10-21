#!/usr/bin/env python3
"""🩺 عیب‌یابی مسیر دستورهای «موجودی» و «فروشگاه».

این ابزار را روی همان دستگاهی اجرا کنید که ربات را اجرا می‌کند:

    cd ~/Download/sorous-plus-antispam-bot-old
    python3 tools/diagnose_economy.py

بدون نیاز به اتصال شبکه، دقیقاً همان هندلری را که در زمان اجرا ثبت
می‌شود صدا می‌زند و نشان می‌دهد پیام وارد کدام تابع می‌شود و کجا متوقف
می‌گردد.
"""
import asyncio
import ast
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK = "✅"
NO = "❌"
INFO = "•"


def line(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)



# ---------------------------------------------------------------------------
# ۰) آیا کدی که اجرا می‌شود همان کد جدید است؟
# ---------------------------------------------------------------------------
def check_running_version():
    line("۰) آیا فایل‌های روی این دستگاه به‌روز هستند؟")
    handler = ROOT / "handlers" / "message_handler.py"
    core = ROOT / "core" / "bot_working_split_ok.py"

    src = handler.read_text(encoding="utf-8")
    has_import = "from handlers.economy_handler import" in src
    has_call = "await handle_economy(" in src
    print(f"{OK if has_import else NO} message_handler اقتصاد را import می‌کند: {has_import}")
    print(f"{OK if has_call else NO} message_handler اقتصاد را صدا می‌زند : {has_call}")

    eco = ROOT / "handlers" / "economy_handler.py"
    print(f"{OK if eco.exists() else NO} فایل economy_handler.py موجود است  : {eco.exists()}")
    print(f"{OK if (ROOT / 'economy').is_dir() else NO} پوشهٔ economy/ موجود است           : {(ROOT / 'economy').is_dir()}")

    # نشانهٔ قطعی کد قدیمی: سیستم سکهٔ حذف‌شده
    legacy = ROOT / "modules" / "coins.py"
    if legacy.exists():
        print(f"{NO} modules/coins.py هنوز هست → کد قدیمی است!")
    else:
        print(f"{OK} modules/coins.py حذف شده (کد جدید)")

    # فایل‌های .pyc کهنه می‌توانند کد قدیمی را زنده نگه دارند
    stale = []
    for cache in ROOT.rglob("__pycache__"):
        for pyc in cache.glob("*.pyc"):
            source = cache.parent / (pyc.name.split(".")[0] + ".py")
            if source.exists() and pyc.stat().st_mtime < source.stat().st_mtime:
                stale.append(pyc)
    if stale:
        print(f"{NO} {len(stale)} فایل .pyc کهنه پیدا شد")
        print("   پاک کنید: find . -name __pycache__ -type d -exec rm -rf {} +")
    else:
        print(f"{OK} هیچ .pyc کهنه‌ای نیست")

    if not (has_import and has_call):
        print("\n" + "!" * 58)
        print("کد این دستگاه قدیمی است. اجرا کنید:")
        print("   git pull")
        print("   pkill -f 'python3 main.py'")
        print("   find . -name __pycache__ -type d -exec rm -rf {} +")
        print("   python3 main.py")
        print("!" * 58)
    return has_import and has_call


# ---------------------------------------------------------------------------
# ۱) وجود داشتن کد
# ---------------------------------------------------------------------------
def check_code_exists():
    line("۱) آیا کد این دو دستور اصلاً وجود دارد؟")
    ok = True
    try:
        import handlers.economy_handler as eh
        from economy.ui import balance_menu, shop_menu
        print(f"{OK} فایل هندلر: {eh.__file__}")
        print(f"{OK} تابع handle موجود است: {callable(eh.handle)}")
        print(f"{OK} دستور موجودی : {balance_menu.COMMAND!r}")
        print(f"{OK} دستور فروشگاه: {shop_menu.COMMAND!r}")
        print(f"{OK} تشخیص «موجودی» : {balance_menu.is_command('موجودی')}")
        print(f"{OK} تشخیص «فروشگاه»: {shop_menu.is_command('فروشگاه')}")
    except Exception as error:
        print(f"{NO} import ناموفق: {error!r}")
        ok = False
    return ok


# ---------------------------------------------------------------------------
# ۲) ثبت در router
# ---------------------------------------------------------------------------
def check_router_registration():
    line("۲) آیا در router ثبت شده‌اند؟")
    source = (ROOT / "handlers" / "message_handler.py").read_text(
        encoding="utf-8")
    has_import = "from handlers.economy_handler import" in source
    print(f"{OK if has_import else NO} import در message_handler: {has_import}")

    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and \
                node.name == "handle_new_message":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and sub.id == "handle_economy":
                    calls.append(sub.lineno)
    calls = sorted(set(calls))
    print(f"{OK if calls else NO} فراخوانی داخل handle_new_message: خط {calls}")
    return has_import and bool(calls)


# ---------------------------------------------------------------------------
# ۳) دیتابیس
# ---------------------------------------------------------------------------
def check_database():
    line("۳) دیتابیس اقتصاد")
    import economy
    import economy.storage as storage
    path = storage.DATA_FILE
    print(f"{INFO} مسیر فایل   : {path}")
    print(f"{INFO} پوشه هست    : {path.parent.exists()}")
    import os
    writable = os.access(path.parent, os.W_OK)
    print(f"{OK if writable else NO} قابل نوشتن  : {writable}")
    print(f"{INFO} فایل هست    : {path.exists()}")
    try:
        balance = economy.get_balance(1)
        print(f"{OK} خواندن موجودی کار می‌کند: {balance}")
        return writable
    except Exception as error:
        print(f"{NO} خواندن موجودی شکست خورد: {error!r}")
        return False


# ---------------------------------------------------------------------------
# ۴) وضعیت گروه‌ها
# ---------------------------------------------------------------------------
def check_groups():
    line("۴) وضعیت گروه‌ها (دستور فقط در گروه فعال کار می‌کند)")
    path = ROOT / "config" / "groups.json"
    if not path.exists():
        print(f"{NO} فایل groups.json وجود ندارد")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    active = {k: v for k, v in data.items() if v.get("active")}
    inactive = {k: v for k, v in data.items() if not v.get("active")}
    print(f"{INFO} کل گروه‌ها : {len(data)}")
    print(f"{OK} فعال       : {len(active)}")
    print(f"{NO} غیرفعال    : {len(inactive)}")
    if inactive:
        print("\n   گروه‌های غیرفعال (دستور در این‌ها کار نمی‌کند):")
        for key, value in list(inactive.items())[:10]:
            print(f"     - {key}  {value.get('title', '')}")
        print("\n   برای فعال کردن، در همان گروه بنویسید: فعال")


# ---------------------------------------------------------------------------
# ۵) اجرای واقعی هندلر
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, captured):
        self.captured = captured
        self.me = types.SimpleNamespace(id=555, username="aifox")

    def on(self, event):
        def deco(fn):
            self.captured.append(fn)
            return fn
        return deco

    async def connect(self):
        return None

    async def get_me(self):
        return self.me

    async def send_message(self, *a, **k):
        return True

    async def run_until_disconnected(self):
        raise SystemExit

    def add_event_handler(self, *a, **k):
        return None


class _Logger:
    def __init__(self):
        self.info, self.errors = [], []

    def log_info(self, m):
        self.info.append(m)

    def log_error(self, m):
        self.errors.append(m)


class _CM:
    def get(self, k, d=None):
        return d


class _Tracker:
    def get_count(self, *a):
        return 0

    def get_all_counts(self):
        return {}


class _Detector:
    def is_spam(self, *a, **k):
        return False, None

    def check_message(self, *a, **k):
        return False, None


class _Chat:
    def __init__(self, cid, title="گروه"):
        self.id = cid
        self.title = title


class _Msg:
    _n = 5000

    def __init__(self, t):
        _Msg._n += 1
        self.message = t
        self.id = _Msg._n
        self.entities = None
        self.file = None


class _User:
    def __init__(self, uid):
        self.id = uid
        self.first_name = "کاربر"
        self.last_name = None
        self.username = None


class _Event:
    def __init__(self, text, uid, chat_id):
        self.message = _Msg(text)
        self.chat_id = chat_id
        self.is_private = False
        self.out = False
        self.replies = []
        self._u = _User(uid)
        self._cid = chat_id
        self.reply_to = None

    async def get_chat(self):
        return _Chat(self._cid)

    async def get_sender(self):
        return self._u

    async def reply(self, text, **k):
        self.replies.append(text)

    async def respond(self, text, **k):
        self.replies.append(text)


async def _build():
    import core.bot_working_split_ok as core
    captured = []
    bot = core.SoroushAntiSpamBot.__new__(core.SoroushAntiSpamBot)
    bot.client = _FakeClient(captured)
    bot.logger = _Logger()
    bot.config_manager = _CM()
    bot.tracker = _Tracker()
    bot.detector = _Detector()
    bot.group_timer_tasks = {}
    bot.bot_account_id = 555
    bot.punished_users = set()
    bot.spam_burst_messages = {}
    bot.spammer_messages = {}
    bot.spam_burst_users = set()

    async def _noop(*a, **k):
        return None

    bot.initialize_client = _noop
    try:
        await asyncio.wait_for(bot.run(), timeout=3)
    except (SystemExit, asyncio.TimeoutError):
        pass
    except Exception:
        pass
    handlers = [f for f in captured
                if getattr(f, "__name__", "") == "new_message_handler"]
    return bot, (handlers[0] if handlers else None)


def check_live_run():
    line("۵) اجرای واقعی: پیام «موجودی» و «فروشگاه» به هندلر ثبت‌شده")
    import modules.group_storage as gs

    chat_id = -100777000111
    gs.activate_group(chat_id, "گروه عیب‌یابی")

    async def scenario():
        bot, handler = await _build()
        if handler is None:
            return None, None, None
        results = {}
        for command in ("موجودی", "فروشگاه"):
            event = _Event(command, 424242, chat_id)
            await handler(event)
            results[command] = event.replies
        return bot, handler, results

    bot, handler, results = asyncio.run(scenario())

    if handler is None:
        print(f"{NO} هندلر پیام اصلاً ثبت نشد")
        return

    print(f"{OK} هندلر ثبت‌شده پیدا شد: {handler.__name__}")
    for command, replies in results.items():
        if replies:
            first = replies[0].splitlines()[0]
            print(f"{OK} «{command}» پاسخ داد: {first}")
        else:
            print(f"{NO} «{command}» هیچ پاسخی نداد")

    print("\n--- لاگ مرحله‌به‌مرحله ---")
    stages = [m for m in bot.logger.info if "ECONOMY" in m]
    if stages:
        for message in stages:
            print(f"   {message.splitlines()[0]}")
    else:
        print(f"   {NO} هیچ لاگ ECONOMY ثبت نشد")

    errors = [m for m in bot.logger.errors if "ECONOMY" in m]
    if errors:
        print("\n--- خطاها ---")
        for message in errors:
            print(f"   {NO} {message.splitlines()[0]}")

    try:
        gs.deactivate_group(chat_id, "گروه عیب‌یابی")
    except Exception:
        pass


def main():
    print("🩺 عیب‌یابی دستورهای «موجودی» و «فروشگاه»")
    print(f"ریشهٔ پروژه: {ROOT}")
    check_running_version()
    check_code_exists()
    check_router_registration()
    check_database()
    check_groups()
    check_live_run()

    line("خلاصه")
    print("اگر بخش ۵ پاسخ داد ولی روی گوشی کار نمی‌کند، یعنی کد سالم است و")
    print("مشکل از محیط اجراست. محتمل‌ترین دلیل‌ها:")
    print("  ۱. گروه شما «فعال» نیست  → در گروه بنویسید: فعال")
    print("  ۲. ربات با کد قدیمی اجرا می‌شود → git pull و ری‌استارت کامل")
    print("  ۳. پروسهٔ قدیمی هنوز زنده است → pkill -f 'python3 main.py'")
    print("\nبرای دیدن لاگ زندهٔ گوشی:")
    print("  grep ECONOMY logs/bot.log | tail -20")


if __name__ == "__main__":
    main()
