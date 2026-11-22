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


#: عمر کش فهرست ادمین‌های گروه. کوتاه نگه داشته می‌شود تا حتی اگر رویداد
#: تغییر نقش سروش‌پلاس از دست رفت، عزلِ ادمین حداکثر یکی‌دو دقیقه بعد
#: دیده شود و کاربرِ عادی‌شده دوباره مثل بقیه مشمول قوانین گروه شود.
#: (مسیر اصلیِ تشخیص عزل، رویداد آنیِ ``note_participant_role`` است.)
GROUP_ADMINS_TTL = 120.0
#: فاصلهٔ حداقلی سینک پس‌زمینهٔ فهرست ادمین‌ها برای هر گروه.
SYNC_MIN_INTERVAL = 60.0


def _list_cache(bot):
    cache = getattr(bot, "native_group_admin_ids_cache", None)
    if cache is None:
        try:
            cache = bot.native_group_admin_ids_cache = {}
        except Exception:
            return {}
    return cache


def _cached_admin_ids(bot, chat_id):
    """فهرست کش‌شدهٔ ادمین‌های گروه اگر تازه است؛ وگرنه ``None``."""
    if bot is None or chat_id is None:
        return None
    entry = _list_cache(bot).get(normalize_group_id(chat_id))
    if entry and entry[1] > time.monotonic():
        return frozenset(entry[0])
    return None


async def fetch_group_admin_ids(bot, chat_id, resolved_chat=None, *,
                                force=False):
    """فهرست شناسهٔ ادمین‌های واقعی همین گروه؛ ``None`` یعنی نامعلوم.

    چند مسیر مستقل، به ترتیب — تا وقتی یکی جواب بدهد:
      ۱) ``channels.GetParticipants`` با فیلتر ادمین‌ها (گروه‌های کانالی)
      ۲) ``messages.GetFullChat`` (گروه‌های ساده)
    نتیجه (حتی برای خواندن‌های بعدی هندلر) در همان کش مشترک
    ``bot.native_group_admin_ids_cache`` می‌نشیند. هیچ استثنایی بیرون
    نمی‌رود؛ خطای همهٔ مسیرها یعنی «نامعلوم»، نه «ادمین نیست».
    """
    if bot is None or chat_id is None:
        return None
    if not force:
        cached = _cached_admin_ids(bot, chat_id)
        if cached is not None:
            return cached
    client = getattr(bot, "client", None)
    if client is None or not callable(client):
        return None

    ids = None
    # مسیر ۱: فهرست ادمین‌های کانال/سوپرگروه.
    try:
        from splusthon import utils as _utils
        from splusthon.tl import functions, types
        peer = resolved_chat
        if peer is not None:
            try:
                peer = _utils.get_input_peer(peer) or peer
            except Exception:
                peer = resolved_chat
        if peer is None:
            peer = chat_id
        response = await client(functions.channels.GetParticipantsRequest(
            channel=peer,
            filter=types.ChannelParticipantsAdmins(),
            offset=0, limit=200, hash=0,
        ))
        found = set()
        for entry in getattr(response, "users", ()) or ():
            entry_id = getattr(entry, "id", None)
            if entry_id is not None:
                found.add(str(entry_id))
        for participant in getattr(response, "participants", ()) or ():
            participant_id = getattr(participant, "user_id", None)
            if participant_id is None:
                participant_id = getattr(
                    getattr(participant, "peer", None), "user_id", None)
            if participant_id is not None and participant_is_admin(participant):
                found.add(str(participant_id))
        ids = frozenset(found)
    except Exception:
        ids = None

    # مسیر ۲: گروه ساده.
    if ids is None:
        try:
            from splusthon.tl import functions
            short_id = int(normalize_group_id(chat_id))
            response = await client(
                functions.messages.GetFullChatRequest(chat_id=short_id))
            container = getattr(
                getattr(response, "full_chat", None), "participants", None)
            found = set()
            for participant in getattr(container, "participants", ()) or ():
                if not participant_is_admin(participant):
                    continue
                participant_id = getattr(participant, "user_id", None)
                if participant_id is None:
                    participant_id = getattr(
                        getattr(participant, "peer", None), "user_id", None)
                if participant_id is not None:
                    found.add(str(participant_id))
            ids = frozenset(found)
        except Exception:
            ids = None

    if ids is None:
        try:
            fail_state = bot._native_admin_list_fail
        except Exception:
            fail_state = None
        if fail_state is None:
            try:
                fail_state = bot._native_admin_list_fail = {}
            except Exception:
                fail_state = {}
        fail_state[normalize_group_id(chat_id)] = time.monotonic() + 60.0
        return None
    cache = _list_cache(bot)
    cache[normalize_group_id(chat_id)] = (
        ids, time.monotonic() + GROUP_ADMINS_TTL)
    if len(cache) > 1000:
        cache.pop(next(iter(cache)), None)
    return ids


def invalidate_native_admin(bot, chat_id, user_id) -> bool:
    """حذف آنیِ نقش ادمینِ کش‌شده — کاربر از همین لحظه «کاربر عادی» است.

    وقتی سروش‌پلاس خبر می‌دهد کاربر دیگر ادمین/سازندهٔ گروه نیست (عزل،
    تنزل به عضو عادی، خروج یا اخراج)، نباید تا انقضای کش صبر کرد: هم
    رکورد تک‌کاربره می‌رود، هم شناسه‌اش از فهرست ادمین‌های گروه خط
    می‌خورد تا هیچ‌یک از مسیرهای معافیت (هندلر اصلی، لاین سبک اسپم،
    گاردها، دورهٔ حذف) او را دیگر ادمین نبینند. فقط حافظه است؛ RPC ندارد.
    """
    if bot is None or chat_id is None or user_id is None:
        return False
    changed = False
    key = (normalize_group_id(chat_id), str(user_id))
    cache = getattr(bot, "native_group_admin_cache", None)
    if isinstance(cache, dict) and cache.pop(key, None) is not None:
        changed = True
    entry = _list_cache(bot).get(key[0])
    if entry and key[1] in entry[0]:
        _list_cache(bot)[key[0]] = (
            frozenset(item for item in entry[0] if item != key[1]),
            entry[1],
        )
        changed = True
    return changed


def note_participant_role(bot, chat_id, user_id, participant):
    """اعمال آنیِ نقش تازهٔ کاربر از روی رویداد تغییر عضویت سروش‌پلاس.

    ``participant`` نقش **فعلی** است (``new_participant`` رویداد خام)؛
    ``None`` یعنی دیگر عضویتی ندارد. اگر ادمین/سازنده شده، نقش و فهرست
    همان لحظه به‌روز می‌شود؛ وگرنه — مهم‌تر از همه برای عزل — معافیتش
    همان لحظه باطل می‌شود و از پیام بعدی مثل کاربر عادی با او رفتار
    می‌شود. خروجی: ``True`` ادمین است، ``False`` نیست، ``None`` نامعلوم.
    """
    if bot is None or chat_id is None or user_id is None:
        return None
    if participant_is_admin(participant):
        _store_role(bot, chat_id, user_id, True, _POSITIVE_TTL)
        cache = _list_cache(bot)
        key = normalize_group_id(chat_id)
        entry = cache.get(key)
        if entry and entry[1] > time.monotonic():
            cache[key] = (
                frozenset(set(entry[0]) | {str(user_id)}), entry[1])
        return True
    invalidate_native_admin(bot, chat_id, user_id)
    return False


async def sync_group_admins(bot, chat_id, resolved_chat=None):
    """سینک پس‌زمینه و کم‌هزینهٔ فهرست ادمین‌های گروه (حداکثر هر ۱ دقیقه).

    از مسیر پیام صدا زده می‌شود تا کش ادمین‌ها **پیش از** اولین تخلف
    گرم باشد و معافیت به RPC لحظهٔ تخلف وابسته نماند. اگر فهرست تازه
    است یا سینک به‌تازگی انجام شده، بی‌هزینه برمی‌گردد.
    """
    if bot is None or chat_id is None or not enabled():
        return None
    if _cached_admin_ids(bot, chat_id) is not None:
        return _cached_admin_ids(bot, chat_id)
    state = getattr(bot, "_native_admin_sync_next", None)
    if state is None:
        try:
            state = bot._native_admin_sync_next = {}
        except Exception:
            state = {}
    key = normalize_group_id(chat_id)
    now = time.monotonic()
    if state.get(key, 0) > now:
        return None
    state[key] = now + SYNC_MIN_INTERVAL
    ids = await fetch_group_admin_ids(bot, chat_id, resolved_chat)
    if ids is not None:
        try:
            bot.logger.log_info(
                "NATIVE ADMIN SYNC "
                f"chat_id={chat_id} admins={len(ids)}"
            )
        except Exception:
            pass
    return ids


async def is_native_admin(bot, chat_id, user_id, user=None,
                          resolved_chat=None) -> bool:
    """نقش واقعی کاربر در همین گروه: ادمین/سازندهٔ بومی هست یا نه.

    ترتیب منابع (از ارزان/مطمئن به گران): نقش همراه رویداد → کش نقش و
    فهرست ادمین‌های گروه → خواندن فهرست ادمین‌های گروه → یک
    ``get_permissions`` (با کش نتیجه). خطای همهٔ مسیرها یعنی «نامعلوم»
    و محافظه‌کارانه ``False`` برگردانده می‌شود تا مسیر عادی خراب نشود.
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
    ids = await fetch_group_admin_ids(bot, chat_id, resolved_chat)
    if ids is not None:
        result = str(user_id) in ids
        _store_role(bot, chat_id, user_id, result,
                    _POSITIVE_TTL if result else _NEGATIVE_TTL)
        return result
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
