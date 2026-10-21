#!/usr/bin/env python3
"""🔎 بررسی اینکه «کدِ در حال اجرا» همان کدِ روی دیسک است.

بعد از pull/merge/stash گاهی فایل‌ها درست‌اند ولی پروسهٔ قدیمی هنوز زنده
است، یا bytecode کهنه مانده. این ابزار دقیقاً همین را تشخیص می‌دهد.

    python3 tools/verify_deployment.py
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK, NO, INFO = "✅", "❌", "•"

# فایل‌هایی که برای کار کردن «موجودی» و «فروشگاه» حتماً لازم‌اند.
REQUIRED = [
    "handlers/economy_handler.py",
    "economy/__init__.py",
    "economy/storage.py",
    "economy/coins/accounts.py",
    "economy/ui/balance_menu.py",
    "economy/ui/shop_menu.py",
    "economy/shop/store.py",
    # بخش پروفایل
    "economy/catalog.py",
    "economy/profiles.py",
    "economy/ui/profile_menu.py",
]

# نشانه‌هایی که حتماً باید داخل سورس باشند.
MARKERS = {
    "handlers/message_handler.py": [
        "from handlers.economy_handler import",
        "await handle_economy(",
    ],
    "handlers/economy_handler.py": [
        "ECONOMY HANDLER ENTER",
        "ECONOMY BALANCE READ",
    ],
    "economy/ui/balance_menu.py": ['COMMAND = "موجودی"'],
    "economy/ui/shop_menu.py": ['COMMAND = "فروشگاه"'],
    "economy/ui/profile_menu.py": [
        'COMMAND_REGISTER = "ثبت پرفایل"',
        'COMMAND_SHOW = "پرفایلم"',
        'COMMAND_DELETE = "حذف پرفایل"',
    ],
}

# فایل‌هایی که باید حذف شده باشند (سیستم سکهٔ قدیمی).
MUST_BE_GONE = ["modules/coins.py", "modules/game_points.py"]


def header(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def check_files():
    header("۱) فایل‌های لازم روی دیسک")
    missing = []
    for rel in REQUIRED:
        path = ROOT / rel
        if path.exists():
            print(f"{OK} {rel}")
        else:
            print(f"{NO} {rel}  ← وجود ندارد")
            missing.append(rel)
    return missing


def check_markers():
    header("۲) محتوای سورس (آیا کد جدید است؟)")
    problems = []
    for rel, needles in MARKERS.items():
        path = ROOT / rel
        if not path.exists():
            print(f"{NO} {rel} وجود ندارد")
            problems.append(rel)
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in needles:
            if needle in text:
                print(f"{OK} {rel}: {needle[:44]}")
            else:
                print(f"{NO} {rel}: {needle[:44]}  ← پیدا نشد")
                problems.append(f"{rel}:{needle}")
    return problems


def check_removed():
    header("۳) بقایای سیستم قدیمی")
    leftovers = []
    for rel in MUST_BE_GONE:
        if (ROOT / rel).exists():
            print(f"{NO} {rel} هنوز هست → کد قدیمی")
            leftovers.append(rel)
        else:
            print(f"{OK} {rel} حذف شده")

    # bytecode یتیم: .pyc بدون .py متناظر
    orphans = []
    for cache in ROOT.rglob("__pycache__"):
        for pyc in cache.glob("*.pyc"):
            stem = pyc.name.split(".")[0]
            if not (cache.parent / f"{stem}.py").exists():
                orphans.append(pyc)
    if orphans:
        print(f"{NO} {len(orphans)} فایل .pyc یتیم (بدون سورس) پیدا شد")
        for pyc in orphans[:6]:
            print(f"     {pyc.relative_to(ROOT)}")
        leftovers.append("orphan-pyc")
    else:
        print(f"{OK} هیچ .pyc یتیمی نیست")

    # bytecode کهنه: .pyc قدیمی‌تر از سورس
    stale = []
    for cache in ROOT.rglob("__pycache__"):
        for pyc in cache.glob("*.pyc"):
            stem = pyc.name.split(".")[0]
            source = cache.parent / f"{stem}.py"
            if source.exists() and pyc.stat().st_mtime < source.stat().st_mtime:
                stale.append(pyc)
    if stale:
        print(f"{NO} {len(stale)} فایل .pyc کهنه‌تر از سورس")
        leftovers.append("stale-pyc")
    else:
        print(f"{OK} هیچ .pyc کهنه‌ای نیست")
    return leftovers


def check_git():
    header("۴) وضعیت Git")
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT,
                                  capture_output=True, text=True,
                                  timeout=20).stdout.strip()
        except Exception:
            return ""

    head = run("rev-parse", "--short", "HEAD")
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    print(f"{INFO} HEAD  : {head}  ({branch})")

    dirty = [l for l in run("status", "--porcelain").splitlines()
             if l and ".pyc" not in l and "__pycache__" not in l]
    if dirty:
        print(f"{NO} {len(dirty)} فایل تغییر‌یافته/ناشناخته:")
        for entry in dirty[:8]:
            print(f"     {entry}")
    else:
        print(f"{OK} درخت کاری تمیز است")

    conflicts = run("diff", "--name-only", "--diff-filter=U")
    if conflicts:
        print(f"{NO} کانفلیکت حل‌نشده:\n{conflicts}")
    else:
        print(f"{OK} هیچ کانفلیکت حل‌نشده‌ای نیست")
    return bool(conflicts)


def check_running_process():
    header("۵) پروسهٔ در حال اجرا")
    try:
        out = subprocess.run(["ps", "-ef"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        print(f"{INFO} ps در دسترس نیست")
        return
    procs = [l for l in out.splitlines()
             if "main.py" in l and "grep" not in l]
    if not procs:
        print(f"{INFO} هیچ پروسهٔ main.py در حال اجرا نیست")
        print("   (اگر ربات روشن است، از همین دستگاه اجرا نشده)")
        return
    print(f"{INFO} {len(procs)} پروسهٔ main.py:")
    for proc in procs:
        print(f"     {proc[:110]}")
    if len(procs) > 1:
        print(f"{NO} بیش از یک پروسه! نسخهٔ قدیمی ممکن است هنوز زنده باشد")
        print("   pkill -f 'python3 main.py'")


def check_import():
    header("۶) آیا برنامه قابل بارگذاری است؟")
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import handlers.message_handler as m;"
        "import handlers.economy_handler as e;"
        "import economy;"
        "from economy.ui import balance_menu, shop_menu, profile_menu;"
        "n=len(economy.catalog.all_items());"
        "t,_=shop_menu.render_items();"
        "assert n==32, 'catalog has %%d items, expected 32' %% n;"
        "assert 'نشان روباه' in t, 'shop list is missing the fixed items';"
        "print('IMPORT_OK', "
        "balance_menu.is_command('موجودی'), "
        "shop_menu.is_command('فروشگاه'), "
        "profile_menu.is_show_command('پرفایلم'), "
        "profile_menu.is_register_command('ثبت پرفایل'), "
        "profile_menu.is_delete_command('حذف پرفایل'), "
        "'items=%%d' %% n)" % str(ROOT)
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=90)
    if result.returncode == 0 and "IMPORT_OK" in result.stdout:
        print(f"{OK} {result.stdout.strip()}")
        return True
    print(f"{NO} بارگذاری شکست خورد:")
    tail = (result.stderr or result.stdout).strip().splitlines()
    for row in tail[-6:]:
        print(f"     {row}")
    return False


def fingerprint():
    header("۷) اثر انگشت نسخه")
    digest = hashlib.sha256()
    for rel in sorted(REQUIRED + list(MARKERS)):
        path = ROOT / rel
        if path.exists():
            digest.update(path.read_bytes())
    print(f"{INFO} fingerprint: {digest.hexdigest()[:16]}")
    print("   این مقدار را با خروجی همین ابزار روی دستگاه دیگر مقایسه کنید.")


def main():
    print("🔎 بررسی سلامت نصب پس از pull / merge / stash")
    print(f"ریشه: {ROOT}")

    missing = check_files()
    problems = check_markers()
    leftovers = check_removed()
    conflicts = check_git()
    check_running_process()
    importable = check_import()
    fingerprint()

    header("نتیجه")
    broken = missing or problems or conflicts or not importable
    if broken:
        print(f"{NO} نصب ناقص است.")
        if missing:
            print(f"   فایل‌های غایب: {missing}")
        if problems:
            print(f"   نشانه‌های غایب: {problems}")
        print("\n   بازیابی:")
        print("     git fetch origin")
        print("     git reset --hard origin/main")
        print("     find . -name __pycache__ -type d -exec rm -rf {} +")
        print("     pkill -f 'python3 main.py'; python3 main.py")
    else:
        print(f"{OK} سورس کامل و سالم است.")
        if leftovers:
            print(f"{NO} ولی بقایای کهنه هست: {leftovers}")
            print("   find . -name __pycache__ -type d -exec rm -rf {} +")
        print("\n   اگر باز هم پاسخ نمی‌گیرید:")
        print("     ۱. گروه باید «فعال» باشد → در گروه بنویسید: فعال")
        print("     ۲. ربات را کامل ری‌استارت کنید")
        print("     ۳. grep ECONOMY logs/bot.log | tail -20")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
