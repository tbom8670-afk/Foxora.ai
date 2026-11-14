# -*- coding: utf-8 -*-
"""💾 وضعیتِ پایدارِ قابلیت «تایم جفت» (Mirror Time).

یک فایلِ مستقل در پوشهٔ configِ رانتایم: ``mirror_time.json``
(``~/.{INSTANCE}/config/mirror_time.json``). هیچ فایلِ موجودِ دیگری
دست نمی‌خورد و هیچ کلیدی به ``config.json``/``group_rules.json`` اضافه
نمی‌شود.

ساختار:

    {
      "version": 1,
      "sent": {"2026-11-13 5:5": {"-100123": 1763000000.0}},   # هر تایم/هر گروه یک بار
      "cards": {"-100123": {"id": 4242, "slot": "2026-11-13 5:5",
                            "text": "♡ ↻ تایم : 5:5 🦊🦋"}}
    }

* ``sent`` ⇒ ری‌استارت هم تایمِ ارسال‌شده را دوباره نمی‌فرستد.
* ``cards`` ⇒ شناسهٔ پیامِ تایم برای تشخیصِ ریپلایِ «فدات» می‌ماند؛ اگر
  ارسال شناسه برنگرداند (این نسخهٔ SPlusthon اغلب ``None`` می‌دهد) با
  متنِ همان کارت در تاریخچهٔ گروه پیدا و همین‌جا ذخیره می‌شود.
"""
from __future__ import annotations

import json
import os
import time

from modules.atomic_write import write_json
from modules.runtime_paths import runtime_config_file

FILE = runtime_config_file("mirror_time.json")

#: چند تایمِ آخر نگه داشته شود (بیشتر از این، فایل بی‌دلیل بزرگ می‌شود).
KEEP_SLOTS = 8

_cache = None
_cache_mtime = None


def _mtime():
    try:
        return os.stat(FILE).st_mtime_ns
    except OSError:
        return None


def _empty():
    return {"version": 1, "sent": {}, "cards": {}}


def load() -> dict:
    """وضعیت را با کشِ mtime می‌خواند؛ هر خطایی ⇒ وضعیتِ خالی (fail-open)."""
    global _cache, _cache_mtime
    mtime = _mtime()
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    data = _empty()
    if mtime is not None:
        try:
            with open(FILE, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            if isinstance(raw, dict):
                sent = raw.get("sent")
                cards = raw.get("cards")
                if isinstance(sent, dict):
                    data["sent"] = sent
                if isinstance(cards, dict):
                    data["cards"] = cards
        except Exception:
            data = _empty()
    _cache = data
    _cache_mtime = mtime
    return _cache


def save(data) -> None:
    global _cache, _cache_mtime
    slots = list(data.get("sent") or {})
    # فقط KEEP_SLOTS تایمِ آخر (کلیدِ «YYYY-MM-DD H:M» ترتیبِ زمانی دارد).
    for stale in sorted(slots)[:-KEEP_SLOTS]:
        data["sent"].pop(stale, None)
    try:
        write_json(FILE, data, indent=2)
    except Exception:
        pass
    _cache = data
    _cache_mtime = _mtime()


# ---------------------------------------------------------------------------
# کلیدِ تایم
# ---------------------------------------------------------------------------
def slot_key(moment) -> str:
    """کلیدِ یکتای هر تایم: ``YYYY-MM-DD H:M``."""
    return "%04d-%02d-%02d %d:%d" % (
        moment.year, moment.month, moment.day,
        int(moment.hour), int(moment.minute),
    )


# ---------------------------------------------------------------------------
# ارسال‌شده‌ها
# ---------------------------------------------------------------------------
def already_sent(slot: str, chat_id) -> bool:
    row = (load().get("sent") or {}).get(slot) or {}
    return str(chat_id) in row


def mark_sent(slot: str, chat_id, message_id=None, text=None) -> None:
    data = load()
    row = data["sent"].setdefault(slot, {})
    row[str(chat_id)] = time.time()
    if text:
        data["cards"][str(chat_id)] = {
            "id": int(message_id) if isinstance(message_id, int) else 0,
            "slot": slot,
            "text": str(text),
        }
    save(data)


# ---------------------------------------------------------------------------
# کارتِ آخرِ هر گروه (برای تطبیقِ ریپلایِ «فدات»)
# ---------------------------------------------------------------------------
def card_for(chat_id):
    """``{"id": int|0, "slot": str, "text": str}`` یا ``None``."""
    card = (load().get("cards") or {}).get(str(chat_id))
    if not isinstance(card, dict):
        return None
    return card


def set_card_id(chat_id, message_id, text=None, slot=None) -> None:
    data = load()
    card = dict(data["cards"].get(str(chat_id)) or {})
    card["id"] = int(message_id) if isinstance(message_id, int) else 0
    if text:
        card["text"] = str(text)
    if slot:
        card["slot"] = slot
    data["cards"][str(chat_id)] = card
    save(data)


def known_ids(chat_id):
    """شناسه‌های ثبت‌شدهٔ کارتِ تایم در این گروه."""
    card = card_for(chat_id) or {}
    found = card.get("id")
    return {int(found)} if isinstance(found, int) and found > 0 else set()


def reset() -> None:
    """پاک‌کردنِ کاملِ وضعیت (فقط برای تست/بازنشانیِ دستی)."""
    global _cache, _cache_mtime
    _cache = _empty()
    _cache_mtime = None
    try:
        write_json(FILE, _empty(), indent=2)
    except Exception:
        pass


__all__ = [
    "FILE",
    "KEEP_SLOTS",
    "load",
    "save",
    "slot_key",
    "already_sent",
    "mark_sent",
    "card_for",
    "set_card_id",
    "known_ids",
    "reset",
]
