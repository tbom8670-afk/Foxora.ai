# -*- coding: utf-8 -*-
"""👥 یک ``GetFullUserRequest`` مشترک برای همهٔ مصرف‌کننده‌ها.

«پروفایل گارد» و «لینک بیو» هر دو برای همان کاربر، پشتِ‌سرِهم،
``GetFullUserRequest`` می‌زدند (لاگ: دو درخواست با ۲۰ms فاصله برای یک
کاربر). هر کدام یک رفت‌وبرگشت به سرور و یک اسلاتِ Governor مصرف می‌کرد.

این ماژول فقط خودِ RPC را مشترک می‌کند و به کش/TTL هیچ‌کدام از دو سرویس
دست نمی‌زند:

* **single-flight:** اگر برای همین کاربر درخواستی در راه است، همه منتظر
  همان پاسخ می‌مانند.
* **بازاستفادهٔ چندثانیه‌ای:** پاسخی که همین چند ثانیهٔ پیش رسیده دوباره
  استفاده می‌شود (``FULL_USER_SHARE_SECONDS``، پیش‌فرض ۵).
* مسیرهای «شاهدِ تازه قبل از مجازات» با ``fresh=True`` همیشه یک پاسخِ
  تازه می‌گیرند (به درخواستِ در راه وصل می‌شوند یا RPC نو می‌زنند).
* خطاها ذخیره نمی‌شوند و همان‌طور به هر مصرف‌کننده می‌رسند.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict

MAX_ENTRIES = 512
_HARD_TIMEOUT = 60.0

_RECENT = OrderedDict()   # key → (monotonic, full)
_INFLIGHT = {}            # key → asyncio.Task
STATS = {"rpc": 0, "reused": 0, "joined": 0}


def share_seconds() -> float:
    try:
        return max(0.0, min(30.0, float(os.getenv("FULL_USER_SHARE_SECONDS", "5"))))
    except (TypeError, ValueError):
        return 5.0


def _key(user_id):
    try:
        return str(int(user_id))
    except (TypeError, ValueError):
        return str(user_id)


def invalidate(user_id) -> None:
    _RECENT.pop(_key(user_id), None)


def clear() -> None:
    _RECENT.clear()
    _INFLIGHT.clear()
    for name in STATS:
        STATS[name] = 0


def _swallow(task) -> None:
    # خطا را هر منتظر جداگانه می‌بیند؛ این فقط جلوی هشدار «never retrieved»
    # را برای حالتی می‌گیرد که همهٔ منتظرها زودتر رفته باشند.
    if not task.cancelled():
        task.exception()


async def _run(key, client, make_request):
    try:
        full = await asyncio.wait_for(client(make_request()), timeout=_HARD_TIMEOUT)
        _RECENT[key] = (time.monotonic(), full)
        _RECENT.move_to_end(key)
        while len(_RECENT) > MAX_ENTRIES:
            _RECENT.popitem(last=False)
        return full
    finally:
        if _INFLIGHT.get(key) is asyncio.current_task():
            _INFLIGHT.pop(key, None)


async def get_full_user(client, make_request, user_id, *, timeout,
                        fresh: bool = False):
    """پاسخِ خامِ ``GetFullUserRequest`` برای ``user_id``.

    ``make_request`` تابعِ بدون‌آرگومانی است که خودِ درخواست را می‌سازد.
    ``asyncio.TimeoutError`` و خطاهای RPC مثل فراخوانیِ مستقیم بالا می‌روند.
    """
    if user_id is None:
        STATS["rpc"] += 1
        return await asyncio.wait_for(client(make_request()), timeout=timeout)
    key = _key(user_id)
    if not fresh:
        entry = _RECENT.get(key)
        if entry is not None:
            if time.monotonic() - entry[0] <= share_seconds():
                STATS["reused"] += 1
                return entry[1]
            _RECENT.pop(key, None)
    task = _INFLIGHT.get(key)
    if task is None or task.done():
        STATS["rpc"] += 1
        task = asyncio.ensure_future(_run(key, client, make_request))
        task.add_done_callback(_swallow)
        _INFLIGHT[key] = task
    else:
        STATS["joined"] += 1
    return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
