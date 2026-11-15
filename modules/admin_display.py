# -*- coding: utf-8 -*-
"""نمایشِ ادمین‌های ثبت‌شده: ``@username``، وگرنه نامِ نمایشی.

ریشهٔ باگِ «ادمین ثبت شده ولی در لیست نیست»: شناسه در ``admins.json`` به شکل
**رشته** ذخیره می‌شود و ``get_entity("123")`` آن را یوزرنیم/شماره حساب می‌کرد
و شکست می‌خورد؛ ادمینِ بدون یوزرنیم هم بی‌صدا از لیست حذف می‌شد. اینجا شناسه
عددی می‌شود، نام ذخیره می‌شود و هیچ ادمینی از لیست حذف نمی‌شود.
"""
from __future__ import annotations

import asyncio

from modules import admin_storage
from modules.user_display import format_user

UNKNOWN = "کاربر ناشناس"
ENTITY_TIMEOUT = 6.0


def entry_fields(entry):
    """``(user_id:int|None, username:str, name:str)`` از رکوردِ ذخیره‌شده."""
    if isinstance(entry, dict):
        user_id, username = entry.get("user_id"), entry.get("username")
        name = entry.get("name")
    else:
        user_id, username, name = None, entry, None
    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    username = str(username or "").strip().lstrip("@")
    return user_id, username, " ".join(str(name or "").split())


def display_name(user) -> str:
    """فقط نامِ نمایشی (بدون یوزرنیم)."""
    first = getattr(user, "first_name", None)
    last = getattr(user, "last_name", None)
    return " ".join(" ".join(
        str(part) for part in (first, last) if part).split())


async def fetch_entity(bot, target):
    try:
        return await asyncio.wait_for(bot.client.get_entity(target),
                                      ENTITY_TIMEOUT)
    except Exception:                                # noqa: BLE001
        return None


async def admin_label(bot, chat_id, entry, known=None):
    """برچسبِ نمایشیِ یک ادمینِ ثبت‌شده — هرگز خالی نیست.

    ``known``: نگاشتِ اختیاریِ ``user_id → entity`` (مثلاً ادمین‌های واقعیِ
    گروه که همان لحظه از API گرفته شده‌اند) تا درخواستِ اضافه لازم نشود.
    """
    user_id, username, name = entry_fields(entry)
    entity = (known or {}).get(user_id) if user_id is not None else None
    if entity is None and not username and user_id is not None:
        entity = await fetch_entity(bot, user_id)
    if entity is not None:
        label = format_user(entity, fallback="")
        try:
            admin_storage.remember_name(
                chat_id, user_id, display_name(entity),
                getattr(entity, "username", None))
        except Exception:                            # noqa: BLE001
            pass
        if label:
            return label
    if username:
        return "@" + username
    return name or UNKNOWN
