# -*- coding: utf-8 -*-
"""🔐 یک سیاستِ واحد برای مجوز «مالک ثبت‌شده / ادمین ثبت‌شده».

همان توابعِ موجودِ پروژه استفاده می‌شوند (``admin_tools``) تا هیچ سیستم
مجوزِ دومی ساخته نشود. تفاوت مهم با مسیر بقیهٔ دستورها:

* ادمینِ **واقعیِ سروش‌پلاس** (native) اینجا کافی نیست؛ فقط مالک اصلی ربات،
  مالک ثبت‌شدهٔ گروه و ادمینِ ثبت‌شدهٔ گروه.
"""
from __future__ import annotations

from modules import admin_tools


def is_owner(chat_id, user_id) -> bool:
    if user_id is None:
        return False
    if admin_tools.is_global_owner(user_id):
        return True
    owner = admin_tools.get_group_owner(chat_id)
    return owner is not None and str(user_id) == str(owner)


def is_registered_admin(chat_id, user_id, username=None) -> bool:
    if user_id is None:
        return False
    if is_owner(chat_id, user_id):
        return True
    return admin_tools.is_admin(chat_id, user_id, username)


def owner_id(chat_id):
    """شناسهٔ مالک ثبت‌شدهٔ گروه (عددی) — برای پرونده‌های گزارش."""
    owner = admin_tools.get_group_owner(chat_id)
    if owner is None:
        return None
    try:
        return int(owner)
    except (TypeError, ValueError):
        return str(owner)
