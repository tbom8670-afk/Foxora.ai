# -*- coding: utf-8 -*-
"""🦊 قابلیت «تایم جفت» (Mirror Time) — بستهٔ مستقل.

* هر ساعتِ جفت (``1:1`` … ``23:23`` به وقتِ تهران) پیامِ
  ``♡ ↻ تایم : 5:5 🦊🦋`` (کاملاً Bold) در گروه‌های فعال ارسال می‌شود.
* هر تایم برای هر گروه **فقط یک بار**؛ وضعیت روی دیسک ذخیره می‌شود، پس
  ری‌استارت هم آن را تکرار نمی‌کند.
* اگر کاربر روی **همان پیامِ تایم** ریپلای کند و «فدات» بنویسد، پاسخ
  ``فدامون 🥲`` داده می‌شود — تشخیص بر اساسِ شناسهٔ پیام، نه واژه.

هیچ قابلیت، قالب، صف یا رفتارِ موجودی تغییر نمی‌کند؛ این بسته فقط:

1. یک حلقهٔ پس‌زمینهٔ مستقل (``service.watcher``) دارد که در ``core`` با
   ``asyncio.create_task`` بالا می‌آید، و
2. یک گاردِ بسیار سبک در ابتدای مسیرِ پیام (``service.handle_reply``) که
   فقط برای پیام‌های حاویِ «فدات» اجرا می‌شود.
"""
from . import service, state, texts  # noqa: F401

FADAT_REPLY = texts.FADAT_REPLY
TIME_TEMPLATE = texts.TIME_TEMPLATE

is_fadat_text = service.is_fadat_text
handle_reply = service.handle_reply
watcher = service.watcher
send_slot = service.send_slot
next_pair_time = service.next_pair_time
is_pair_time = service.is_pair_time
render = texts.render

__all__ = [
    "service",
    "state",
    "texts",
    "FADAT_REPLY",
    "TIME_TEMPLATE",
    "is_fadat_text",
    "handle_reply",
    "watcher",
    "send_slot",
    "next_pair_time",
    "is_pair_time",
    "render",
]
