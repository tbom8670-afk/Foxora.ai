#!/usr/bin/env python3
"""🔁 جبران دستی تبدیل‌هایی که با نرخ قدیمی انجام شده‌اند.

ربات این کار را هنگام راه‌اندازی خودکار انجام می‌دهد. این ابزار برای
وقتی است که می‌خواهید *پیش از* اجرای ربات نتیجه را ببینید یا مطمئن
شوید انجام شده است.

    python3 tools/fix_old_conversions.py          # فقط گزارش
    python3 tools/fix_old_conversions.py --apply  # اعمال
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import economy  # noqa: E402
from economy import settings, upgrade_migration  # noqa: E402


def _fa(value):
    return str(value).translate(
        {ord(str(i)): p for i, p in enumerate("۰۱۲۳۴۵۶۷۸۹")})


def main():
    apply = "--apply" in sys.argv
    config = settings.load()

    print("🔁 جبران تبدیل‌های قدیمی")
    print(f"فایل اقتصاد: {economy.storage.DATA_FILE}")
    print()
    print("نرخ فعلی تبدیل:")
    print(f"  {_fa(config['BronzeToSilverCost'])} برنز ➜ "
          f"{_fa(config['BronzeToSilverGain'])} نقره")
    print(f"  {_fa(config['SilverToGoldCost'])} نقره ➜ "
          f"{_fa(config['SilverToGoldGain'])} طلا")
    print()

    pending = upgrade_migration.preview()
    if not pending:
        print("✅ هیچ کیف پولی بدهکار نیست؛ همه با نرخ جدید هم‌خوان‌اند.")
        return 0

    print(f"{len(pending)} کیف پول باید جبران شود:")
    for key, owed in sorted(pending.items()):
        chat, _, user = key.partition(":")
        parts = ", ".join(f"{coin} +{_fa(amount)}"
                          for coin, amount in owed.items())
        print(f"  گروه {chat} · کاربر {user} → {parts}")

    if not apply:
        print()
        print("برای اعمال:")
        print("  python3 tools/fix_old_conversions.py --apply")
        return 0

    print()
    paid = upgrade_migration.run()
    economy.storage.flush()
    print(f"✅ {len(paid)} کیف پول جبران شد.")
    for key, granted in sorted(paid.items()):
        chat, _, user = key.partition(":")
        parts = ", ".join(f"{coin} +{_fa(amount)}"
                          for coin, amount in granted.items())
        print(f"  گروه {chat} · کاربر {user} → {parts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
