# -*- coding: utf-8 -*-
"""🔐 یک سیاستِ واحد برای مجوز «مالک ثبت‌شده / ادمین ثبت‌شده».

همان توابعِ موجودِ پروژه استفاده می‌شوند (``admin_tools``) تا هیچ سیستم
مجوزِ دومی ساخته نشود. تفاوت مهم با مسیر بقیهٔ دستورها:

* ادمینِ **واقعیِ سروش‌پلاس** (native) اینجا کافی نیست؛ فقط مالک اصلی ربات،
  مالک ثبت‌شدهٔ گروه و ادمینِ ثبت‌شدهٔ گروه.

مالکیت سه سطح دارد (مهم برای «تأیید گزارش»):

1. **مالکِ ثبت‌شدهٔ همین گروه** — تنها کسی که تصمیمِ گزارش را می‌گیرد؛ چون کارت
   «⛔️ مالک محترم: @…» داخل همان گروه و با همان شناسه ارسال می‌شود.
2. **مالک سراسری ربات** — فقط وقتی گروه هیچ مالکِ ثبت‌شده‌ای ندارد.
3. ادمینِ ثبت‌شدهٔ گروه — اجازهٔ «اختصاصی»/«پیام زماندار» دارد، ولی **حق
   تصمیمِ گزارش** ندارد.
"""
from __future__ import annotations

from modules import admin_tools


def group_owner(chat_id):
    """مالکِ ثبت‌شدهٔ همین گروه (عددی) — بدون در نظر گرفتن مالک سراسری."""
    return admin_tools.get_group_owner(chat_id)


def is_group_owner(chat_id, user_id) -> bool:
    """آیا این کاربر مالکِ ثبت‌شدهٔ همین گروه است؟"""
    if user_id is None:
        return False
    owner = group_owner(chat_id)
    return owner is not None and str(user_id) == str(owner)


def is_owner(chat_id, user_id) -> bool:
    """مالکِ گروه؛ و اگر گروه مالک ثبت‌شده ندارد، مالک سراسری ربات."""
    if user_id is None:
        return False
    if is_group_owner(chat_id, user_id):
        return True
    if group_owner(chat_id) is not None:
        return False
    return admin_tools.is_global_owner(user_id)


def is_registered_admin(chat_id, user_id, username=None) -> bool:
    if user_id is None:
        return False
    if is_owner(chat_id, user_id) or is_group_owner(chat_id, user_id):
        return True
    return admin_tools.is_admin(chat_id, user_id, username)


def owner_id(chat_id):
    """شناسهٔ مالک برای پروندهٔ گزارش (عددی و پایدار).

    اول مالکِ ثبت‌شدهٔ همین گروه، بعد (اگر نبود) مالک سراسری ربات.
    """
    owner = group_owner(chat_id)
    if owner is None:
        owner = admin_tools.get_global_owner_id()
    if owner is None:
        return None
    try:
        return int(owner)
    except (TypeError, ValueError):
        return str(owner)
