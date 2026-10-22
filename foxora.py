#!/usr/bin/env python3
"""Foxora — لانچر قطعی ربات Foxora (Instance مستقل ساخته‌شده از نسخهٔ واقعی B2).

کار این فایل فقط این است که **قبل از import شدن هر ماژول پروژه** متغیر
``BOT_INSTANCE`` را روی ``foxora`` قرار دهد و بعد همان entrypoint واقعی
(main.py) را اجرا کند. همه‌ی مسیرهای runtime توسط
``modules/runtime_paths.py`` بر اساس همین instance جدا می‌شوند:

    python3 foxora.py  ->  ~/.foxora/

جدایی کامل runtime state این instance از B2 (ربات ضد هرزنامه سروش پلاس):
    config/: owner.json, config.json, groups.json, group_expiry.json,
             banned_users.json, bot.pid, state های bot_detector/group_level/...
    logs/:   bot.log, error.log, backups/, archive/
    db/:     bot.sqlite3

قفل PID (bot.pid) به‌صورت per-instance است؛ Foxora هرگز پروسه‌ی B2 را
مسدود یا دست‌کاری نمی‌کند و بالعکس.

Session: سلسله‌مراتب خواندن session string از محیط/.env:
    SOROUSH_SESSION_STRING_FOXORA  →  SOROUSH_SESSION_STRING
session فقط در حافظه است (StringSession) و هیچ فایل session روی دیسک
نوشته نمی‌شود؛ بنابراین session بین Foxora و B2 انتقال پیدا نمی‌کند.
"""
import os

# MUST be قبل از import هر ماژول پروژه: modules/runtime_paths در زمان
# import، INSTANCE_NAME/DATA_DIR را از BOT_INSTANCE می‌سازد و .env هنوز
# لود نشده است. همین خط، instance را برای کل اجرای foxora.py قطعی می‌کند.
os.environ["BOT_INSTANCE"] = "foxora"

import asyncio

# import AFTER تعیین instance: بدنه‌ی main.py (تعیین dotenv و importهای
# پروژه) حالا با BOT_INSTANCE=foxora اجرا می‌شود. بلاک if __name__ ==
# "__main__" در main.py هنگام import اجرا نمی‌شود.
import main

if __name__ == "__main__":
    asyncio.run(main.main())
