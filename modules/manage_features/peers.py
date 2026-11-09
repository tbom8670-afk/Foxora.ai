# -*- coding: utf-8 -*-
"""🧭 resolve کردن «گروه» برای RPCها — یک‌جا و درست.

مشکلِ واقعی: شناسهٔ گروه در این پروژه به شکلِ **مثبت** ذخیره می‌شود
(``normalize_group_id``). اگر همان عدد به متدهای SPlusthon داده شود، کتابخانه
آن را «کاربر» فرض می‌کند و برای گرفتنِ access_hash سراغ
``users.GetUsersRequest`` می‌رود؛ نتیجه: یا ``Could not find the input
entity``/خطای شبکه، یا عملیاتِ بی‌اثر (حذف/ویرایش انجام نمی‌شود و خطایی هم
برنمی‌گردد). این ماژول فقط یک قاعده دارد:

1. اگر InputPeer گروه در ``bot.reply_input_peer_cache`` بود ⇒ همان.
2. وگرنه شناسهٔ منفیِ کانال: ``-(1000000000000 + short_id)``.

هیچ RPC‌ای این‌جا زده نمی‌شود.
"""
from __future__ import annotations

from modules.group_id import normalize_group_id
from modules.manage_features import storage

CHANNEL_ID_OFFSET = 1_000_000_000_000


def group_peer(bot, chat_id):
    """InputPeer (یا شناسهٔ منفیِ امنِ) گروه برای RPCهای SPlusthon."""
    cache = getattr(bot, "reply_input_peer_cache", None) or {}
    try:
        peer = cache.get(chat_id)
    except Exception:                                # noqa: BLE001
        peer = None
    if peer is not None:
        return peer

    wanted = storage.group_key(chat_id)
    try:
        for cached_id, cached_peer in list(cache.items()):
            if cached_peer is not None and storage.group_key(cached_id) == wanted:
                return cached_peer
    except Exception:                                # noqa: BLE001
        pass

    try:
        raw = int(chat_id)
        short = int(normalize_group_id(chat_id))
    except (TypeError, ValueError):
        return chat_id
    if raw < 0:
        return raw
    return -(CHANNEL_ID_OFFSET + short)


def is_meaningful(value) -> bool:
    """آیا peer واقعاً به گروه اشاره می‌کند؟ (برای مسیرهای حساس)"""
    return value is not None
