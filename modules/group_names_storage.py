"""ذخیره‌گاه و تطبیق «فیلتر اسم» اختصاصی هر گروه.

امکانات:
  * ``add_name(group_id, name)`` — افزودن نام به لیست فیلتر اسم گروه
  * ``remove_name(group_id, name)`` — حذف نام از لیست فیلتر اسم گروه
  * ``get_names(group_id)`` — دریافت لیست نام‌های فیلترشدهٔ گروه
  * ``find_matching_filtered_name(user_or_first_name, last_name=None, filtered_names=None, group_id=None)``
    — بررسی اینکه آیا نام کاربر با یکی از نام‌های فیلترشدهٔ گروه مطابقت دارد یا خیر.
"""
from __future__ import annotations

import json
import re

from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.persian_names import match_persian_name, normalize_persian_name_text
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file("group_filtered_names.json")

_cache = None
_cache_mtime = None


def _file_mtime():
    try:
        return FILE.stat().st_mtime_ns
    except OSError:
        return None


def _clean_name_entry(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[\u200c\u200d\u200e\u200f\ufeff]+", " ", str(name))
    return " ".join(cleaned.split()).strip()


def _collapse_repeats(value: str) -> str:
    return re.sub(r"(.)\1+", r"\1", value)


def load_names() -> dict:
    global _cache, _cache_mtime
    mtime = _file_mtime()
    if _cache is not None and mtime == _cache_mtime:
        return _cache

    if mtime is None:
        _cache = {}
    else:
        try:
            loaded = json.loads(FILE.read_text(encoding="utf-8"))
            _cache = loaded if isinstance(loaded, dict) else {}
        except Exception:
            _cache = {}

    _cache_mtime = mtime
    return _cache


def save_names(data: dict) -> None:
    global _cache, _cache_mtime
    write_json(FILE, data, indent=2)
    _cache = data
    _cache_mtime = _file_mtime()


def add_name(group_id, name: str) -> bool:
    data = load_names()
    gid = normalize_group_id(group_id)
    cleaned = _clean_name_entry(name)
    if not cleaned:
        return False

    if gid not in data or not isinstance(data.get(gid), list):
        data[gid] = []

    norm_target = normalize_persian_name_text(cleaned)
    for existing in data[gid]:
        if existing == cleaned or (
            norm_target and normalize_persian_name_text(existing) == norm_target
        ):
            return False

    data[gid].append(cleaned)
    save_names(data)
    return True


def remove_name(group_id, name: str) -> bool:
    data = load_names()
    gid = normalize_group_id(group_id)
    cleaned = _clean_name_entry(name)
    if not cleaned or gid not in data or not isinstance(data.get(gid), list):
        return False

    norm_target = normalize_persian_name_text(cleaned)
    for idx, existing in enumerate(list(data[gid])):
        if existing == cleaned or (
            norm_target and normalize_persian_name_text(existing) == norm_target
        ):
            data[gid].pop(idx)
            save_names(data)
            return True

    return False


def get_names(group_id) -> list[str]:
    data = load_names()
    items = data.get(normalize_group_id(group_id), [])
    return list(items) if isinstance(items, list) else []


def _extract_first_last(user_or_first_name, last_name=None) -> tuple[str, str]:
    if user_or_first_name is None:
        return "", str(last_name or "")
    if isinstance(user_or_first_name, str):
        return user_or_first_name, str(last_name or "")
    fn = getattr(user_or_first_name, "first_name", "") or ""
    ln = getattr(user_or_first_name, "last_name", "") or ""
    if last_name:
        ln = f"{ln} {last_name}".strip()
    return str(fn), str(ln)


def matches_filtered_name(target_phrase: str, first_name: str, last_name: str = "") -> bool:
    """بررسی تطبیق نام کاربر با عبارت فیلترشده (شامل حروف کشیده یا تکراری)."""
    if not target_phrase:
        return False
    if match_persian_name(target_phrase, first_name, last_name):
        return True
    # بررسی حالت کشیده/تکرار حروف (مثلاً «علـــی» یا «عسسسل»)
    collapsed_fn = _collapse_repeats(normalize_persian_name_text(first_name))
    collapsed_ln = _collapse_repeats(normalize_persian_name_text(last_name))
    collapsed_target = _collapse_repeats(normalize_persian_name_text(target_phrase))
    if collapsed_target and match_persian_name(collapsed_target, collapsed_fn, collapsed_ln):
        return True
    return False


def find_matching_filtered_name(
    user_or_first_name,
    last_name: str | None = None,
    filtered_names: list[str] | None = None,
    *,
    group_id=None,
) -> str | None:
    """در صورت تطبیق نام کاربر با لیست فیلتر اسم گروه، نام فیلترشده را برمی‌گرداند."""
    if filtered_names is None:
        if group_id is None:
            return None
        filtered_names = get_names(group_id)
    if not filtered_names:
        return None

    fn, ln = _extract_first_last(user_or_first_name, last_name)
    if not fn.strip() and not ln.strip():
        return None

    for phrase in filtered_names:
        if matches_filtered_name(phrase, fn, ln):
            return phrase
    return None
