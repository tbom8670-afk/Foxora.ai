"""📄 فهرست‌های فیلتر (نام کانال / جملهٔ بیوگرافی) از فایل‌های config.

هر خط یک عبارت است؛ خطوط خالی و خطوطی که با ``#`` شروع می‌شوند نادیده
گرفته می‌شوند. فایل‌ها با mtime کش می‌شوند، پس ویرایش دستی فایل بدون
ری‌استارت ربات اعمال می‌شود و خواندن در مسیر داغ تقریباً رایگان است.

مسیر فایل‌ها (اولی که وجود داشته باشد):

1. ``runtime_config_file(name)`` — پوشهٔ config همان instance (پیشنهادشده)
2. ``<project>/config/<name>`` — نسخهٔ داخل مخزن
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

from modules.runtime_paths import PROJECT_ROOT, runtime_config_file

CHANNEL_LIST_FILE = "profile_channel_names.txt"
BIO_LIST_FILE = "profile_bio_sentences.txt"

_SOURCE_COMMENT = "#"


def _mtime(path: Path) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return -1.0


def _resolve(name: str) -> Path:
    """تازه‌ترین نسخهٔ موجود فایل فهرست را برمی‌گرداند.

    دو نسخه ممکن است وجود داشته باشد: نسخهٔ runtime (که
    ``runtime_config_file`` از پروژه کپی می‌کند) و نسخهٔ داخل مخزن
    (``config/``). اگر فقط یکی باشد همان، و اگر هر دو باشند **تازه‌ترین**
    انتخاب می‌شود تا ویرایش دستی هرکدام از آن‌ها بی‌اثر نماند.
    """
    candidates = []
    try:
        candidates.append(Path(runtime_config_file(name)))
    except Exception:
        pass
    candidates.append(Path(PROJECT_ROOT) / "config" / name)
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return candidates[0]
    return max(existing, key=_mtime)


def _read(path: Path) -> Tuple[str, List[str]]:
    """``(امضای فایل, عبارت‌ها)`` — امضا برای کش mtime است."""
    try:
        stat = os.stat(path)
        signature = f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return f"{path}:missing", []
    items: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                text = line.strip()
                if not text or text.startswith(_SOURCE_COMMENT):
                    continue
                items.append(text)
    except Exception:
        return signature, []
    return signature, items


_cache: dict = {}


def _load(name: str) -> List[str]:
    path = _resolve(name)
    signature, items = _read(path)
    cached = _cache.get(name)
    if cached is not None and cached[0] == signature:
        return cached[1]
    _cache[name] = (signature, items)
    return items


def channel_names() -> List[str]:
    """نام‌های کانالِ فیلترشده."""
    return list(_load(CHANNEL_LIST_FILE))


def bio_sentences() -> List[str]:
    """جمله‌های بیوگرافیِ فیلترشده."""
    return list(_load(BIO_LIST_FILE))


def filter_count() -> int:
    """تعداد کل نام‌ها + جمله‌ها (برای پیام «فعال شد»)."""
    return len(channel_names()) + len(bio_sentences())


def is_empty() -> bool:
    """``True`` اگر هیچ فیلتری تنظیم نشده باشد."""
    return filter_count() == 0


def reload() -> None:
    """کش را پاک می‌کند (برای تست/دستور ادمین)."""
    _cache.clear()
