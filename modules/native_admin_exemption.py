# -*- coding: utf-8 -*-
"""🛡️ معافیت سراسری ادمین‌های بومی گروه (نقش واقعی سروش‌پلاس).

سیاست (خواستهٔ کارفرما): ربات در **هیچ گروهی** به کاربری که در خودِ
سروش‌پلاس ادمین یا سازندهٔ همان گروه است «گیر» نمی‌دهد — دقیقاً مثل
حالت VIP:

* هیچ اخطار خودکاری (مسیر اصلی، هوش مصنوعی ضدتبلیغ، پروفایل‌گارد،
  فیلترها و…) برای او ثبت نمی‌شود؛
* پیامش بابت تخلف حذف نمی‌شود و وارد چرخهٔ سکوت/بن/دورهٔ حذف پنج‌دقیقه‌ای
  نمی‌شود؛
* اخطار/اخراج دستیِ ادمین ثبت‌شده هم روی او اجرا نمی‌شود (مثل VIP).

و خط قرمزِ هم‌زمان: این معافیت **فقط معافیت از مجازات است، نه مجوز**.
تا وقتی کاربر در ربات «ادمین ثبت‌شده» نشده باشد (یا مالک گروه/مالک
سراسری نباشد) هیچ دستور مدیریتی برای او اجرا نمی‌شود؛ همهٔ بررسی‌های
مجوز دستورها مثل قبل فقط از ``admin_tools.has_admin_permission`` و
``_has_group_management_permission`` می‌آیند و این ماژول هرگز در آن‌ها
دخالت نمی‌کند.

کلید خاموش‌کردن سراسری (برگرداندن رفتار قبلیِ «فقط اخطار»):
``BOT_NATIVE_ADMIN_EXEMPT=0`` در ``.env`` (پیش‌فرض: روشن).

تشخیص نقش فقط از منابع واقعی است: نقش همراه رویداد (participant)،
کش نقشِ روی bot (همان کشی که ``handlers.message_handler`` پر می‌کند) و
در نهایت ``client.get_permissions`` — با کش کوتاه‌مدت تا در مسیر داغ
RPC تکراری ساخته نشود. هیچ حدسی از روی نام/یوزرنیم زده نمی‌شود.
"""
from __future__ import annotations

import os
import time

from modules.group_id import normalize_group_id

#: نام کلاس‌های participant که در سروش‌پلاس یعنی «ادمین یا سازندهٔ گروه».
ADMIN_PARTICIPANT_NAMES = frozenset({
    "ChannelParticipantAdmin", "ChannelParticipantCreator",
    "ChatParticipantAdmin", "ChatParticipantCreator",
})

_POSITIVE_TTL = 60.0    # نقش ادمین تأییدشده؛ کوتاه تا عزل سریع دیده شود.
_NEGATIVE_TTL = 90.0    # عضو عادی؛ هم‌ارز _NATIVE_ADMIN_NEGATIVE_TTL هندلر.
_FAIL_TTL = 180.0       # خطای RPC؛ هم‌ارز _NATIVE_ADMIN_FAIL_TTL هندلر.


def enabled() -> bool:
    """آیا معافیت سراسری ادمین بومی روشن است؟ (پیش‌فرض: بله)."""
    raw = os.environ.get("BOT_NATIVE_ADMIN_EXEMPT", "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def participant_is_admin(participant) -> bool:
    """تشخیص نقش از روی کلاس واقعی participant سروش‌پلاس."""
    if participant is None:
        return False
    return type(participant).__name__ in ADMIN_PARTICIPANT_NAMES


def _cache(bot):
    cache = getattr(bot, "native_group_admin_cache", None)
    if cache is None:
        try:
            cache = bot.native_group_admin_cache = {}
        except Exception:
            return {}
    return cache


def _cached_role(bot, chat_id, user_id):
    """نقش کش‌شده: ``True``/``False`` اگر معلوم است، وگرنه ``None``.

    هر دو کش هندلر خوانده می‌شود: کش تک‌کاربره و فهرست ادمین‌های گروه.
    فقط lookup حافظه‌ای است؛ هیچ RPCای ندارد.
    """
    if bot is None or chat_id is None or user_id is None:
        return None
    now = time.monotonic()
    key = (normalize_group_id(chat_id), str(user_id))
    cached = (getattr(bot, "native_group_admin_cache", None) or {}).get(key)
    if cached and cached[1] > now:
        return bool(cached[0])
    admin_list = (getattr(bot, "native_group_admin_ids_cache", None)
                  or {}).get(key[0])
    if admin_list and admin_list[1] > now:
        return key[1] in admin_list[0]
    return None


def cached_native_admin(bot, chat_id, user_id) -> bool:
    """آیا از روی کشِ موجود معلوم است کاربر ادمین بومی این گروه است؟"""
    return _cached_role(bot, chat_id, user_id) is True


def _store_role(bot, chat_id, user_id, value, ttl):
    key = (normalize_group_id(chat_id), str(user_id))
    cache = _cache(bot)
    cache[key] = (bool(value), time.monotonic() + ttl)
    if len(cache) > 5000:
        cache.pop(next(iter(cache)), None)


async def is_native_admin(bot, chat_id, user_id, user=None) -> bool:
    """نقش واقعی کاربر در همین گروه: ادمین/سازندهٔ بومی هست یا نه.

    ترتیب منابع: نقش همراه رویداد → کش روی bot → یک
    ``get_permissions`` (با کش نتیجه). خطای RPC یعنی «نامعلوم» و
    محافظه‌کارانه ``False`` برگردانده می‌شود تا مسیر عادی خراب نشود.
    """
    if bot is None or chat_id is None or user_id is None:
        return False
    participant = getattr(user, "participant", None) if user is not None else None
    if participant_is_admin(participant):
        _store_role(bot, chat_id, user_id, True, _POSITIVE_TTL)
        return True
    cached = _cached_role(bot, chat_id, user_id)
    if cached is not None:
        return cached
    client = getattr(bot, "client", None)
    get_permissions = getattr(client, "get_permissions", None)
    if not callable(get_permissions):
        return False
    try:
        permissions = await get_permissions(chat_id, user or user_id)
        result = bool(
            getattr(permissions, "is_admin", False)
            or getattr(permissions, "is_creator", False)
        )
    except Exception:
        _store_role(bot, chat_id, user_id, False, _FAIL_TTL)
        return False
    _store_role(bot, chat_id, user_id, result,
                _POSITIVE_TTL if result else _NEGATIVE_TTL)
    return result


async def exempt(bot, chat_id, user_id, user=None) -> bool:
    """معافیت کامل این کاربر در این گروه (روشن‌بودن قابلیت + نقش بومی)."""
    if not enabled():
        return False
    return await is_native_admin(bot, chat_id, user_id, user)
