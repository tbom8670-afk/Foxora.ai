#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""📥 ورودِ جمله‌های فیلتر از یک فایل متنی — خط فرمان (Termux).

کاربرد: فایلی که روی گوشی/Termux دارید را بدهید تا جمله‌های مربوط به
«کانال» و «بیوگرافی» هرکدام در فهرستِ درستِ خودشان بنشینند.

    # فایلی که سرخط دارد (CHANNEL / BIO)
    python3 tools/profile_guard_import.py ~/filters.txt

    # فایلی که سرخط ندارد: بخش را صریح بدهید
    python3 tools/profile_guard_import.py ~/channels.txt --channel
    python3 tools/profile_guard_import.py ~/bios.txt --bio

    # فقط ببینید چه چیزی اضافه می‌شود (بدون نوشتن)
    python3 tools/profile_guard_import.py ~/filters.txt --dry-run

قالب فایل:

    CHANNEL
    فیلم و سریال
    فیلم سوپر

    BIO
    بیا تو کانال ما فیلم هست

(«[channel]» / «[bio]»، «کانال» و «بیوگرافی» هم پذیرفته می‌شوند.)

تکرارها نوشته نمی‌شوند، پس اجرای دوبارهٔ همان فایل بی‌خطر است.
ربات فایل را بدون ری‌استارت می‌خواند.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.profile_guard import importer  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ورودِ جمله‌های فیلتر کانال/بیوگرافی از فایل متنی"
    )
    parser.add_argument("file", help="مسیر فایل (پشتیبانی از ~)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--channel", action="store_true",
                       help="همهٔ خطوط = نام کانال (وقتی فایل سرخط ندارد)")
    group.add_argument("--bio", action="store_true",
                       help="همهٔ خطوط = جملهٔ بیوگرافی (وقتی فایل سرخط ندارد)")
    parser.add_argument("--dry-run", action="store_true",
                        help="فقط گزارش؛ هیچ فایلی نوشته نمی‌شود")
    args = parser.parse_args(argv)

    section = None
    if args.channel:
        section = importer.SECTION_CHANNEL
    elif args.bio:
        section = importer.SECTION_BIO

    report = importer.import_file(args.file, default_section=section,
                                  dry_run=args.dry_run)
    errors = report.get("errors") or []
    if errors:
        print("❌ ورود انجام نشد:")
        for error in errors:
            print("   •", error)
        return 2

    added = report.get("added") or {}
    dup = report.get("skipped_duplicates") or {}
    bad = report.get("skipped_unusable") or {}
    totals = report.get("totals") or {}
    prefix = "🔎 (dry-run) " if args.dry_run else "✅ "
    print(prefix + "نتیجهٔ ورودِ فایل:", report.get("source"))
    print("   نام کانال اضافه‌شده :", int(added.get(importer.SECTION_CHANNEL, 0) or 0))
    print("   جمله بیو اضافه‌شده  :", int(added.get(importer.SECTION_BIO, 0) or 0))
    duplicates = (int(dup.get(importer.SECTION_CHANNEL, 0) or 0)
                  + int(dup.get(importer.SECTION_BIO, 0) or 0))
    if duplicates:
        print("   تکراری (نادیده)     :", duplicates)
    unusable = (int(bad.get(importer.SECTION_CHANNEL, 0) or 0)
                + int(bad.get(importer.SECTION_BIO, 0) or 0))
    if unusable:
        print("   بی‌حرف/فقط‌ایموجی    :", unusable)
    for section_name, path in (report.get("files") or {}).items():
        print(f"   فایل مقصد ({section_name}) : {path}")
    if totals:
        print("   مجموع فهرست‌ها       :",
              int(totals.get(importer.SECTION_CHANNEL, 0) or 0), "نام +",
              int(totals.get(importer.SECTION_BIO, 0) or 0), "جمله")
    return 0 if (report.get("ok") or args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
