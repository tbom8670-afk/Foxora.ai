# -*- coding: utf-8 -*-
"""⛔️ «متخلف» — کاربری که اجازهٔ ارسال پیام در گروه را از دست می‌دهد.

این قابلیت عضوِ چهارمِ پکیج ``modules/manage_features`` است و مثل سه قابلیت
دیگر (اختصاصی / پیام زماندار / گزارش) ذخیره‌سازی، صف و روتر مستقل خودش را
دارد. هیچ‌کدام از ماژول‌های مشترک (``group_actions``، ``group_stats``،
``admin_tools`` …) تغییر رفتار نمی‌دهند؛ فقط توابع/کلیدهای **اضافی** استفاده
می‌شوند.

جریان کار (مطابق خواستهٔ کارفرما):

1. **دستور «متخلف» با ریپلای** — فقط ``access.is_registered_admin`` (مالک یا
   ادمین **ثبت‌شدهٔ ربات**). ادمین عادیِ خودِ سروش که در ربات ثبت نشده باشد
   رد می‌شود.
2. **گیت حذف** در اولین نقطهٔ خط لولهٔ پیام (پیش از ضد اسپم، بازی‌ها، آمار و
   روتر دستورها) هر پیامِ تازهٔ آن کاربر را در همان گروه حذف می‌کند: متن،
   رسانه، استیکر، فوروارد، via-bot و پیامِ **ویرایش‌شده**. گروه‌های دیگر
   دست‌نخورده می‌مانند (رکوردها per-group هستند).
3. **«لغو متخلف» با ریپلای** — رکورد حذف و کاربر دوباره می‌تواند پیام بدهد.
4. **محافظت‌شده‌ها** (هرگز متخلف نمی‌شوند و پیامشان حذف نمی‌شود):
   مالک ثبت‌شدهٔ گروه، مالک سراسری ربات، سازندهٔ گروه
   (``ChannelParticipantCreator``) و خود ربات.
5. **دفاع دوم در زمان اجرا**: گیت حذف پیش از هر حذفی مالک/سازندهٔ کش‌شده/ربات
   را دوباره بررسی می‌کند؛ حتی اگر رکوردِ قدیمی یا اشتباهی در storage باشد
   حذف انجام نمی‌شود و رکوردِ اشتباه پاک می‌شود.
6. **متخلفِ ادمین ثبت‌شده** هم معمول باید حذف شود؛ پیامش (حتی «لغو متخلف»
   برای خودش) پیش از روتر دستورها حذف و پردازش متوقف می‌شود.

شناساییِ نقش‌ها در کد (سؤال کارفرما: «ادمین‌های گروه را چطور بشناسد؟»):

* **ادمین/مالک ثبت‌شدهٔ ربات** ⇒ ``access.is_registered_admin`` که روی
  ``admin_tools``/``admin_storage`` سوار است (فهرست ثبت‌شدهٔ خودِ ربات).
* **مالک ثبت‌شدهٔ گروه** ⇒ ``access.is_group_owner`` /
  ``admin_tools.get_group_owner``.
* **مالک سراسری ربات** ⇒ ``admin_tools.is_global_owner`` (تنها منبع:
  ``config/owner.json``).
* **سازندهٔ گروه** ⇒ API رسمی: ``channels.GetParticipantRequest`` (نوعِ
  ``ChannelParticipantCreator``) یا فهرستِ ادمین‌ها
  (``ChannelParticipantsAdmins``). هیچ حدسی بر پایهٔ نام/یوزرنیم زده نمی‌شود.
* **خود ربات** ⇒ ``bot.bot_account_id`` (با چند منبع پشتیبان).
* **ادمین بک‌گراند/عادی گروه** ⇒ ``ChannelParticipantAdmin`` — این‌ها
  **محافظت‌شده نیستند** و مثل هر کاربر دیگر می‌توانند متخلف شوند.
* **ادمین بی‌نام** (پیامی که با هویتِ خودِ گروه می‌آید) قابل شناسایی نیست؛
  گیت این‌گونه پیام‌ها را نادیده می‌گیرد (محدودیت مستند‌شدهٔ API رسمی).

ذخیره‌سازی: ``violators.json`` با ساختارِ دقیقاً
``{chat_id: {user_id: {since, set_by, set_by_username}}}`` — اتمیک
(``storage.Storage``: temp + ``os.replace`` + fsync + ``.bak``) و با
**ایندکس در حافظه** برای جست‌وجوی O(1) در مسیر گرم (هیچ خواندنِ فایلی برای
هر پیام؛ فقط یک ``stat`` در ثانیه برای دیدنِ تغییرِ خارجی).

حذف: دسته‌ای با پنجرهٔ کوتاه (``DELETE_FLUSH_SECONDS``) به‌ازای هر گروه و از
طریق ``rpc.call`` (تایم‌اوت + FloodWait + backoff) با **peer صحیحِ کانال**
(``peers.group_peer``). حذف‌های این قابلیت با شمارندهٔ **جدا** در آمار ثبت
می‌شوند (``group_stats.add_violator_deleted``) و جزو اسپم/حذف دستی شمرده
نمی‌شوند.
"""
from __future__ import annotations

import asyncio

from modules import group_stats
from modules.manage_features import access, exclusive, peers, storage
from modules.manage_features.queue import PerGroupQueue
from modules.manage_features.rpc import call

try:  # pragma: no cover - کتابخانه در محیط واقعی نصب است
    from splusthon.tl import types
except ImportError:                                  # pragma: no cover
    types = None


# ---------------------------------------------------------------------------
#  ثابت‌ها
# ---------------------------------------------------------------------------
#: پنجرهٔ کوتاهِ دسته‌بندیِ حذف به‌ازای هر گروه (ثانیه). پیام‌های هم‌زمانِ
#: همین گروه در یک RPC حذف می‌شوند؛ «فوری» به‌معنای زیر نیم‌ثانیه است.
DELETE_FLUSH_SECONDS = 0.4
#: سقفِ شناسه‌ها در هر RPC حذف (خودِ SPlusthon هم ۱۰۰تایی chunk می‌کند).
DELETE_BATCH_SIZE = 100
#: حداکثر انتظارِ گیت برای تکمیلِ حذفِ همین پیام (بعد از آن پردازش متوقف
#: می‌شود ولی حذف در پس‌زمینه ادامه دارد).
GATE_WAIT_SECONDS = 2.0
#: TTL کشِ شناسهٔ سازندهٔ گروه (یک ساعت) — طبق خواستهٔ کارفرما.
CREATOR_CACHE_TTL = 3600.0
#: حداکثر فاصلهٔ بررسیِ mtime فایل storage برای دیدنِ تغییرِ خارجی.
INDEX_REFRESH_SECONDS = 1.0
#: لاگِ خلاصهٔ حذف‌های ناموفق: حداکثر یک بار در دقیقه (جلوگیری از لاگِ اسپم).
FAIL_LOG_INTERVAL = 60.0
#: اطلاع به مالک دربارهٔ از دست رفتنِ حق حذف: حداکثر یک بار در ساعت.
OWNER_NOTIFY_INTERVAL = 3600.0
#: مهلتِ هر RPC حذف/اطلاع‌رسانی (همان پوششِ امنِ قابلیت‌ها).
RPC_TIMEOUT = 10.0
MAX_RETRY = 3
#: مهلتِ خواندن‌های سبک (سازنده/حقوق) — کوتاه‌تر از مسیر حذف.
READ_RPC_TIMEOUT = 8.0

#: صف مستقلِ حذف — کارهای سنگین هر گروه پشت سر هم و بدون قفل‌کردنِ بقیه.
QUEUE = PerGroupQueue("violator_delete", max_pending_per_key=8,
                      max_pending_total=256, task_timeout=30.0)


# ---------------------------------------------------------------------------
#  خطاهای معنادار (لایهٔ دستور آنها را به پیام فارسی نگاشت می‌کند)
# ---------------------------------------------------------------------------
class ViolationError(RuntimeError):
    """خطای فنی/عمومی — «ثبت انجام نشد»."""


class ProtectedUser(RuntimeError):
    """هدف محافظت‌شده است (مالک/مالک سراسری/سازنده/ربات)."""


class CreatorUnknown(RuntimeError):
    """نتوانستیم سازندهٔ گروه را از API رسمی تشخیص بدهیم ⇒ ثبت نمی‌کنیم."""


class BotLacksDeleteRights(RuntimeError):
    """ربات حق «حذف پیام» را در این گروه ندارد."""


class AlreadyViolator(RuntimeError):
    pass


class NotViolator(RuntimeError):
    pass


# ---------------------------------------------------------------------------
#  حافظهٔ درونی
# ---------------------------------------------------------------------------
#: chat_key → set(user_key) — ایندکسِ O(1) برای مسیر گرم.
_INDEX: dict = {}
_INDEX_READY = False
_LAST_INDEX_CHECK = 0.0
#: chat_key → (monotonic, creator_id|None) — کش سازنده با TTL.
_CREATOR_CACHE: dict = {}
#: chat_key → asyncio.Lock (single-flight برای resolve سازنده).
_CREATOR_LOCKS: dict = {}
#: chat_key → [(message_id, future)] — پیام‌های در انتظارِ حذف.
_PENDING: dict = {}
#: chat_key → asyncio.Task (کارگرِ حذفِ همان گروه).
_FLUSHERS: dict = {}
#: chat_key → {"count", "last_log", "last_notify"} — خلاصهٔ خطاها.
_FAIL_STATE: dict = {}


def _log(logger, message, *, error=False):
    if logger is None:
        return
    try:
        (logger.log_error if error else logger.log_info)(message)
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  ایندکسِ حافظه‌ای (O(1) برای هر پیام)
# ---------------------------------------------------------------------------
def _build_index() -> dict:
    data = storage.VIOLATORS_STORAGE.load() or {}
    index: dict = {}
    if not isinstance(data, dict):
        return index
    for chat_key, record in data.items():
        if not isinstance(record, dict) or not record:
            continue
        users = {str(user_key) for user_key, entry in record.items()
                 if isinstance(entry, dict)}
        if users:
            index[str(chat_key)] = users
    return index


def _refresh_index(force: bool = False) -> None:
    """تازه‌کردنِ ایندکس از دیسک — حداکثر یک ``stat`` در ثانیه.

    مسیر گرم هیچ‌وقت منتظر دیسک نمی‌ماند: اگر بررسیِ tازه وجود نداشته باشد،
    ایندکسِ قبلی (که با ``update`` تازه شده) استفاده می‌شود.
    """
    global _INDEX_READY, _LAST_INDEX_CHECK
    now = storage.monotonic()
    if _INDEX_READY and not force and (now - _LAST_INDEX_CHECK) < \
            INDEX_REFRESH_SECONDS:
        return
    _LAST_INDEX_CHECK = now
    try:
        fresh = _build_index()
    except Exception:                                # noqa: BLE001
        return
    _INDEX.clear()
    _INDEX.update(fresh)
    _INDEX_READY = True


def is_violator(chat_id, user_id) -> bool:
    """آیا این کاربر در این گروه متخلف است؟ (فقط جست‌وجوی حافظه‌ای)"""
    if user_id is None:
        return False
    users = _INDEX.get(storage.group_key(chat_id))
    if not users:
        return False
    return peers.user_key(user_id) in users


def get_record(chat_id, user_id):
    """رکوردِ پایدارِ یک کاربر (کپی) یا ``None``."""
    data = storage.VIOLATORS_STORAGE.load() or {}
    group = data.get(storage.group_key(chat_id))
    if not isinstance(group, dict):
        return None
    entry = group.get(peers.user_key(user_id))
    return dict(entry) if isinstance(entry, dict) else None


def list_violators(chat_id) -> list:
    """همهٔ رکوردهای متخلفانِ یک گروه (برای لاگ/بازیابی/تست)."""
    data = storage.VIOLATORS_STORAGE.load() or {}
    group = data.get(storage.group_key(chat_id))
    if not isinstance(group, dict):
        return []
    return [dict(entry) for entry in group.values() if isinstance(entry, dict)]


def count(chat_id) -> int:
    return len(list_violators(chat_id))


def _save_record(chat_id, identifier, *, actor_id=None, username=""):
    """نوشتن رکورد با ساختارِ خواسته‌شده + تازه‌کردنِ ایندکس.

    ``username`` نامِ کاربریِ **عامل** (ادمینِ ثبت‌کننده) است و در
    ``set_by_username`` می‌رود. ساختارِ رکورد عیناً
    ``{since, set_by, set_by_username}`` است و هیچ کلیدِ اضافه‌ای ندارد.
    """
    key = storage.group_key(chat_id)
    user_key = peers.user_key(identifier)
    entry = {
        "since": storage.now(),
        "set_by": storage.user_key(actor_id) if actor_id is not None else None,
        "set_by_username": str(username or "").lstrip("@"),
    }

    def mutate(data):
        group = data.get(key)
        if not isinstance(group, dict):
            group = {}
            data[key] = group
        group[user_key] = entry
        return entry

    saved = storage.VIOLATORS_STORAGE.update(mutate)
    _INDEX.setdefault(key, set()).add(user_key)
    return saved


def _remove_record(chat_id, identifier) -> bool:
    """حذف رکورد + تازه‌کردنِ ایندکس. خروجی: رکوردی حذف شد یا نه."""
    key = storage.group_key(chat_id)
    user_key = peers.user_key(identifier)
    removed = False

    def mutate(data):
        nonlocal removed
        group = data.get(key)
        if isinstance(group, dict) and user_key in group:
            group.pop(user_key, None)
            removed = True
            if not group:
                data.pop(key, None)
        return removed

    storage.VIOLATORS_STORAGE.update(mutate)
    users = _INDEX.get(key)
    if users is not None:
        users.discard(user_key)
        if not users:
            _INDEX.pop(key, None)
    return removed


# ---------------------------------------------------------------------------
#  محافظت‌شده‌ها — بدون RPC (مالک ثبت‌شده / مالک سراسری / ربات)
# ---------------------------------------------------------------------------
def is_protected_identity(chat_id, user_id) -> bool:
    """مالکِ ثبت‌شدهٔ همین گروه یا مالکِ سراسری ربات (بدون هیچ RPC).

    ⚠️ ادمینِ عادیِ گروه (native) اینجا محافظت‌شده **نیست**: طبق خواستهٔ
    کارفرما حتی ادمینِ گروه و ادمینِ ثبت‌شدهٔ ربات هم می‌توانند متخلف شوند.
    """
    identifier = peers.user_identifier(user_id)
    if identifier is None:
        # هویت ناشناس ⇒ محافظت‌شده فرض می‌شود (حذف/ثبت انجام نمی‌شود).
        return True
    try:
        if access.is_owner(chat_id, identifier):
            return True
    except Exception:                                # noqa: BLE001
        pass
    try:
        from modules import admin_tools
        if admin_tools.is_global_owner(identifier):
            return True
    except Exception:                                # noqa: BLE001
        pass
    return False


def _bot_id(bot):
    """شناسهٔ خودِ ربات از معتبرترین منبعِ موجود (بدون RPC)."""
    for candidate in (getattr(bot, "bot_account_id", None),
                      getattr(getattr(bot, "client", None), "bot_id", None),
                      getattr(getattr(bot, "me", None), "id", None)):
        identifier = peers.user_identifier(candidate)
        if identifier is not None:
            return identifier
    return None


# ---------------------------------------------------------------------------
#  سازندهٔ گروه — API رسمی + کشِ TTL
# ---------------------------------------------------------------------------
def is_creator_participant(participant) -> bool:
    """آیا این شرکت‌کننده «سازندهٔ گروه» است؟ (``ChannelParticipantCreator``)

    تشخیص از **نام کلاسِ واقعیِ API** و پرچم ``is_creator`` (لایه‌های جدیدی
    که Creator را Admin + flag می‌فرستند). هیچ حدسی بر پایهٔ نام کاربر زده
    نمی‌شود.
    """
    if participant is None:
        return False
    if "Creator" in type(participant).__name__:
        return True
    return bool(getattr(participant, "is_creator", False))


async def resolve_creator_id(bot, chat_id, *, logger=None):
    """``(creator_user_id, error)`` — شناسهٔ سازندهٔ گروه از API رسمی.

    مسیر ۱: ``get_participants`` با فیلترِ ``ChannelParticipantsAdmins`` و
    یافتن ردیفِ ``ChannelParticipantCreator``. مسیر ۲ (پشتیبان): اولین ردیفِ
    ``get_participants`` که در TL همان ``chatParticipantCreator`` است — ولی
    فقط وقتی خودِ ردیف هم صریحاً «سازنده» باشد (هیچ حدسی).

    ``(None, error)`` یعنی خطای فنی؛ ``(None, None)`` یعنی API ردیفِ سازنده
    را نداد. در هر دو حالت «سازنده ناشناس» است و ثبتِ متخلف انجام نمی‌شود.
    """
    try:
        peer, _channel = await exclusive._require_channel(bot, chat_id)
    except Exception as error:                       # noqa: BLE001
        return None, error

    if types is not None and hasattr(types, "ChannelParticipantsAdmins"):
        try:
            rows = await call(
                lambda: bot.client.get_participants(
                    peer, filter=types.ChannelParticipantsAdmins(),
                    limit=200),
                logger=logger, timeout=READ_RPC_TIMEOUT, max_retry=1,
                what=f"VIOLATOR CREATOR LIST chat={chat_id}")
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            return None, error
        for row in list(rows or []):
            participant = getattr(row, "participant", None)
            if not (is_creator_participant(participant)
                    or getattr(row, "is_creator", False)):
                continue
            identifier = peers.user_identifier(row)
            if identifier is None:
                identifier = peers.user_identifier(
                    getattr(participant, "user_id", None))
            if identifier is not None:
                return identifier, None

    # پشتیبان: اولین عضوِ فهرست کامل — فقط اگر خودِ API صریحاً Creator بگوید.
    try:
        first_rows = await call(
            lambda: bot.client.get_participants(peer, limit=1),
            logger=logger, timeout=READ_RPC_TIMEOUT, max_retry=1,
            what=f"VIOLATOR CREATOR FIRST chat={chat_id}")
        rows_list = list(first_rows or [])
    except asyncio.CancelledError:
        raise
    except Exception as error:                       # noqa: BLE001
        return None, error
    if rows_list:
        first = rows_list[0]
        participant = getattr(first, "participant", None)
        if is_creator_participant(participant) or \
                getattr(first, "is_creator", False):
            identifier = peers.user_identifier(first)
            if identifier is None:
                identifier = peers.user_identifier(
                    getattr(participant, "user_id", None))
            if identifier is not None:
                return identifier, None
    return None, None


def cached_creator_id(chat_id):
    """``(creator_id, fresh)`` — مقدارِ کش‌شده اگر تازه (< TTL) باشد."""
    entry = _CREATOR_CACHE.get(storage.group_key(chat_id))
    if entry is None:
        return None, False
    stamp, creator_id = entry
    if storage.monotonic() - stamp > CREATOR_CACHE_TTL:
        return None, False
    return creator_id, True


async def creator_id_cached(bot, chat_id, *, logger=None, force=False):
    """شناسهٔ سازنده با کشِ TTL و single-flight (حداکثر یک RPC در ساعت/گروه).

    اگر رکوردِ اشتباهی برای سازنده در storage باشد (مثلاً انتقال مالکیتِ
    گروه پس از ثبت)، همین‌جا پاک می‌شود تا دفاع دومِ گیت همیشه درست بماند.
    """
    key = storage.group_key(chat_id)
    if not force:
        creator_id, fresh = cached_creator_id(chat_id)
        if fresh:
            return creator_id
    lock = _CREATOR_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CREATOR_LOCKS[key] = lock
    async with lock:
        if not force:
            creator_id, fresh = cached_creator_id(chat_id)
            if fresh:
                return creator_id
        creator_id, error = await resolve_creator_id(bot, chat_id,
                                                     logger=logger)
        _CREATOR_CACHE[key] = (storage.monotonic(), creator_id)
        if creator_id is None:
            _log(logger,
                 f"VIOLATOR CREATOR UNKNOWN chat_id={chat_id} "
                 f"error={error!r}")
        elif is_violator(chat_id, creator_id):
            # رکوردِ اشتباه: سازنده هرگز متخلف نیست ⇒ حذف رکورد.
            try:
                _remove_record(chat_id, creator_id)
                _log(logger,
                     f"VIOLATOR CREATOR RECORD DROPPED chat_id={chat_id} "
                     f"user_id={creator_id}")
            except Exception:                        # noqa: BLE001
                pass
        return creator_id


def _prime_creator_cache(bot, chat_id, logger=None) -> None:
    """پرکردنِ کش سازنده در پس‌زمینه (مسیر دستور را بلاک نمی‌کند)."""
    try:
        asyncio.ensure_future(_prime_creator_cache_via_queue(bot, chat_id,
                                                             logger))
    except RuntimeError:                             # noqa: BLE001
        pass


async def _prime_creator_cache_via_queue(bot, chat_id, logger=None):
    try:
        await QUEUE.run(
            key=storage.group_key(chat_id),
            job=lambda: creator_id_cached(bot, chat_id, logger=logger,
                                          force=True),
            logger=logger)
    except Exception as error:                       # noqa: BLE001
        _log(logger,
             f"VIOLATOR CREATOR PRIME FAILED chat_id={chat_id} "
             f"error={error!r}")


# ---------------------------------------------------------------------------
#  ثبت / لغو
# ---------------------------------------------------------------------------
async def _ensure_not_creator(bot, chat_id, identifier, *, entity=None,
                              logger=None):
    """اگر هدف سازندهٔ گروه بود ⇒ ``ProtectedUser``؛ اگر ندانستیم ⇒
    ``CreatorUnknown`` (ثبت انجام نمی‌شود)."""
    participant = None
    read_error = None
    try:
        participant, read_error = await exclusive.read_participant(
            bot, chat_id, identifier, entity=entity, attempts=2,
            backoff=(0.0, 0.5))
    except exclusive.ExcludedUser:
        # کاربر بن‌شدهٔ ربات است؛ سازندهٔ گروه نمی‌تواند بن شده باشد، ولی
        # برای اطمینان مسیر دوم (فهرست) هم اجرا می‌شود.
        participant, read_error = None, "excluded-user"
    except Exception as error:                       # noqa: BLE001
        participant, read_error = None, repr(error)

    if participant is not None:
        if is_creator_participant(participant):
            raise ProtectedUser(
                f"user {identifier} is the group creator of {chat_id}")
        return

    # مسیر دوم: سازنده را از فهرست می‌خوانیم و مقایسه می‌کنیم.
    creator_id, creator_error = await resolve_creator_id(bot, chat_id,
                                                         logger=logger)
    if creator_id is None:
        raise CreatorUnknown(
            f"cannot determine creator of {chat_id}: "
            f"read_error={read_error!r} list_error={creator_error!r}")
    if peers.user_key(creator_id) == peers.user_key(identifier):
        raise ProtectedUser(
            f"user {identifier} is the group creator of {chat_id}")


async def mark(bot, chat_id, user_id, *, actor_id=None, name="", username="",
               entity=None, logger=None):
    """ثبت کاربر به‌عنوان «متخلف» — خروجی: رکوردِ ذخیره‌شده.

    ترتیب بررسی‌ها عمداً محافظه‌کارانه است: هیچ رکوردی پیش از ردِّ
    محافظت‌شده‌ها، تشخیصِ سازنده و بررسیِ حقِ حذفِ ربات نوشته نمی‌شود.
    """
    identifier = peers.user_identifier(entity) if entity is not None \
        else peers.user_identifier(user_id)
    if identifier is None:
        raise ViolationError(f"invalid user identity: {user_id!r}")

    # ۱) محافظت‌شده‌ها: مالکِ ثبت‌شدهٔ گروه، مالک سراسری، خود ربات.
    if is_protected_identity(chat_id, identifier):
        raise ProtectedUser(f"user {identifier} is a protected owner")
    bot_id = _bot_id(bot)
    if bot_id is not None and \
            peers.user_key(identifier) == peers.user_key(bot_id):
        raise ProtectedUser(f"user {identifier} is the bot itself")

    # ۲) سازندهٔ گروه — اگر نتوانستیم تشخیص بدهیم، ثبت نمی‌کنیم.
    await _ensure_not_creator(bot, chat_id, identifier, entity=entity,
                              logger=logger)

    # ۳) تکراری؟
    if is_violator(chat_id, identifier):
        raise AlreadyViolator(f"user {identifier} already violator")

    # ۴) حقِ حذف پیام برای ربات (API رسمی). ``None`` = نامعلوم ⇒ لاگ و ادامه
    #    (مثل سیاستِ بقیهٔ قابلیت‌ها: خطای خواندنِ موقت جلوی کار را نمی‌گیرد).
    rights = await exclusive.bot_admin_rights(bot, chat_id)
    if rights is not None and \
            not bool(getattr(rights, "delete_messages", False)):
        raise BotLacksDeleteRights(
            f"bot lacks delete_messages right in {chat_id}")
    if rights is None:
        _log(logger,
             f"VIOLATOR BOT RIGHTS UNKNOWN chat_id={chat_id} (proceeding)")

    # ۵) اگر کاربر رکورد «اختصاصی» فعال داشت، لغو شود تا ادمینِ اختصاصی
    #    عملاً بدون محدودیت پیام ندهد (مسیر لغوِ موجود).
    if exclusive.is_exclusive(chat_id, identifier):
        try:
            await exclusive.revoke(bot, chat_id, identifier,
                                   reason="marked_violator", entity=entity)
            _log(logger,
                 f"VIOLATOR EXCLUSIVE REVOKED chat_id={chat_id} "
                 f"user_id={identifier}")
        except Exception as error:                   # noqa: BLE001
            _log(logger,
                 f"VIOLATOR EXCLUSIVE REVOKE FAILED chat_id={chat_id} "
                 f"user_id={identifier} error={error!r}", error=True)

    # ۶) ثبت رکوردِ پایدار + تازه‌کردنِ ایندکس.
    record = _save_record(chat_id, identifier, actor_id=actor_id,
                          username=username)

    # ۷) پرکردنِ کش سازنده برای گیتِ اجرا (پس‌زمینه، بدون بلاک).
    _prime_creator_cache(bot, chat_id, logger)
    _log(logger,
         f"VIOLATOR MARKED chat_id={chat_id} user_id={identifier} "
         f"actor={actor_id}")
    return record


def unmark(chat_id, user_id, *, reason="manual", logger=None) -> bool:
    """حذف رکورد «متخلف» — خروجی ``True`` اگر رکوردی حذف شده باشد."""
    identifier = peers.user_identifier(user_id)
    if identifier is None:
        raise ViolationError(f"invalid user identity: {user_id!r}")
    if not is_violator(chat_id, identifier):
        raise NotViolator(f"user {identifier} is not a violator")
    removed = _remove_record(chat_id, identifier)
    _log(logger,
         f"VIOLATOR UNMARKED chat_id={chat_id} user_id={identifier} "
         f"reason={reason}")
    return removed


# ---------------------------------------------------------------------------
#  گیتِ حذف — اولین نقطهٔ خط لولهٔ پیام
# ---------------------------------------------------------------------------
def _is_unidentifiable_sender(sender, chat_id) -> bool:
    """ادمین بی‌نام: پیام با هویتِ خودِ گروه می‌آید (from_id = کانال).

    API رسمی راهی برای شناساییِ چنین فرستنده‌ای نمی‌دهد؛ این محدودیت
    مستند شده است و گیت این‌گونه پیام‌ها را نادیده می‌گیرد (نه حذف، نه ثبت).
    """
    if sender is None:
        return True
    class_name = type(sender).__name__.lower()
    if "channel" in class_name or "chat" in class_name:
        return True
    if getattr(sender, "channel_id", None) is not None:
        return True
    try:
        if chat_id is not None and str(getattr(sender, "id", None)) == \
                str(chat_id):
            return True
    except Exception:                                # noqa: BLE001
        pass
    return False


def _is_service_message(event) -> bool:
    """پیامِ سرویس (ورود/خروج/تغییرِ عنوان…) با ``delete_messages`` حذف
    نمی‌شود؛ گیت آن را رد می‌کند تا خطای بی‌فایده تولید نشود."""
    message = getattr(event, "message", None)
    if message is None:
        return False
    return getattr(message, "action", None) is not None


async def _protected_at_runtime(bot, chat_id, user_id, *, logger=None):
    """دفاع دوم در زمان اجرا — پیش از هر حذف.

    مالکِ ثبت‌شده (``access.is_owner``)، مالک سراسری، خود ربات و
    سازندهٔ **کش‌شده**ٔ گروه هرگز حذف نمی‌شوند — حتی اگر رکوردِ قدیمی یا
    اشتباهی در storage باشد. اگر کش سازنده سرد باشد، یک بار (به‌ازای هر
    گروه در ساعت) با single-flight resolve می‌شود.
    """
    identifier = peers.user_identifier(user_id)
    if identifier is None:
        return True
    if is_protected_identity(chat_id, identifier):
        return True
    bot_id = _bot_id(bot)
    if bot_id is not None and \
            peers.user_key(identifier) == peers.user_key(bot_id):
        return True
    creator_id = await creator_id_cached(bot, chat_id, logger=logger)
    if creator_id is not None and \
            peers.user_key(creator_id) == peers.user_key(identifier):
        return True
    return False


def _drop_wrong_record(chat_id, user_id, *, logger=None, reason="protected"):
    """رکوردِ اشتباهیِ یک کاربرِ محافظت‌شده را پاک می‌کند (خودِ درمان)."""
    try:
        if _remove_record(chat_id, user_id):
            _log(logger,
                 f"VIOLATOR WRONG RECORD DROPPED chat_id={chat_id} "
                 f"user_id={user_id} reason={reason}")
    except Exception:                                # noqa: BLE001
        pass


async def gate_message(bot, event, chat_id, user_id, sender, message_id,
                       *, logger=None) -> bool:
    """گیتِ حذف. خروجی ``True`` ⇒ پیام مصرف شد و پردازش متوقف می‌شود.

    این تابع هیچ‌وقت استثنا نمی‌دهد؛ هر خطای غیرمنتظره فقط لاگ می‌شود و
    ``False`` برمی‌گردد تا مسیر پیام آسیب نبیند.
    """
    try:
        if chat_id is None or user_id is None:
            return False
        if getattr(event, "is_private", False):
            return False
        if _is_unidentifiable_sender(sender, chat_id):
            return False
        if _is_service_message(event):
            return False
        _refresh_index()
        if not is_violator(chat_id, user_id):
            return False
        # دفاع دوم: محافظت‌شده‌ها هرگز حذف نمی‌شوند.
        if await _protected_at_runtime(bot, chat_id, user_id, logger=logger):
            _drop_wrong_record(chat_id, user_id, logger=logger)
            return False
        if message_id is None:
            # بدون شناسهٔ پیام حذف ممکن نیست، ولی پردازش هم انجام نمی‌شود.
            return True
        await _enqueue_delete(bot, chat_id, message_id, logger=logger)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as error:                       # noqa: BLE001
        _log(logger,
             f"VIOLATOR GATE ERROR chat_id={chat_id} user_id={user_id} "
             f"error={error!r}", error=True)
        return False


# ---------------------------------------------------------------------------
#  حذفِ دسته‌ای (پنجرهٔ کوتاه به‌ازای هر گروه)
# ---------------------------------------------------------------------------
def _ensure_flusher(bot, chat_id, key, logger=None):
    task = _FLUSHERS.get(key)
    if task is not None and not task.done():
        return task
    try:
        task = asyncio.ensure_future(_flush_loop(bot, chat_id, key, logger))
    except RuntimeError:                             # noqa: BLE001
        return None
    _FLUSHERS[key] = task
    return task


async def _enqueue_delete(bot, chat_id, message_id, logger=None):
    """شناسهٔ پیام را در صفِ حذفِ همان گروه می‌گذارد و منتظرِ حذف می‌ماند.

    انتظار با سقفِ ``GATE_WAIT_SECONDS`` است؛ اگر حذف طول کشید، پردازش
    متوقف می‌شود ولی حذف در پس‌زمینه ادامه دارد.
    """
    key = storage.group_key(chat_id)
    try:
        numeric = int(message_id)
    except (TypeError, ValueError):
        return
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    # استثنای احتمالیِ آینده همین‌جا بازیابی می‌شود تا هشدارِ
    # «exception was never retrieved» تولید نشود.
    future.add_done_callback(
        lambda done: None if done.cancelled() else done.exception())
    _PENDING.setdefault(key, []).append((numeric, future))
    _ensure_flusher(bot, chat_id, key, logger)
    try:
        await asyncio.wait_for(asyncio.shield(future), GATE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        pass
    except Exception as error:                       # noqa: BLE001
        _log(logger,
             f"VIOLATOR FLUSH ERROR chat_id={chat_id} error={error!r}",
             error=True)
    finally:
        # اگر پیامِ تازه‌ای (مثلاً همین شناسهٔ ما) پیش از خروجِ کارگر رسیده
        # باشد، کارگرِ تازه لازم است تا هیچ شناسه‌ای رها نشود.
        if _PENDING.get(key):
            _ensure_flusher(bot, chat_id, key, logger)


async def _flush_loop(bot, chat_id, key, logger=None):
    """کارگرِ حذفِ یک گروه: تا وقتی پیام تازه‌ای هست، دسته حذف می‌کند."""
    try:
        while True:
            batch = _PENDING.get(key) or []
            if not batch:
                return 0
            _PENDING[key] = []
            # پنجرهٔ کوتاه: پیام‌های هم‌زمانِ همین گروه به همین دسته می‌پیوندند.
            if DELETE_FLUSH_SECONDS > 0:
                await asyncio.sleep(DELETE_FLUSH_SECONDS)
                extra = _PENDING.get(key) or []
                if extra:
                    batch.extend(extra)
                    _PENDING[key] = []
            ids = [numeric for numeric, _future in batch]
            futures = [future for _numeric, future in batch]
            try:
                deleted = await QUEUE.run(
                    key=key,
                    job=lambda batch_ids=ids: _delete_ids(
                        bot, chat_id, batch_ids, logger),
                    logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception as error:               # noqa: BLE001
                await _note_failure(bot, chat_id, error, logger=logger,
                                    count=len(ids))
                for future in futures:
                    if not future.done():
                        future.set_exception(error)
                return 0
            for future in futures:
                if not future.done():
                    future.set_result(deleted)
    finally:
        if _FLUSHERS.get(key) is asyncio.current_task():
            _FLUSHERS.pop(key, None)


async def _delete_batch_rpc(bot, peer, ids, *, logger=None,
                            what="VIOLATOR DELETE"):
    """یک RPC حذف با پوششِ امن (تایم‌اوت + FloodWait + تلاش مجدد).

    ``factory`` از طریق ``rpc.call`` فراخوانی می‌شود (هر تلاش یک coroutine
    تازه می‌سازد) — هیچ‌وقت coroutineِ آماده پاس داده نمی‌شود.
    """
    try:
        return await call(bot.client.delete_messages, peer, ids,
                          revoke=True, logger=logger, timeout=RPC_TIMEOUT,
                          max_retry=MAX_RETRY, what=what)
    except TypeError as error:
        # کلاینتِ بدون کلیدواژهٔ revoke (پیش‌فرضِ خودش هم revoke=True است).
        if "revoke" not in str(error).lower():
            raise
        return await call(bot.client.delete_messages, peer, ids,
                          logger=logger, timeout=RPC_TIMEOUT,
                          max_retry=MAX_RETRY, what=what)


def _count_affected(result, requested) -> int:
    rows = result if isinstance(result, (list, tuple)) else [result]
    counted = 0
    for row in rows:
        value = getattr(row, "pts_count", None)
        if isinstance(value, int) and value >= 0:
            counted += value
    if counted <= 0:
        # رفتار خودِ کتابخانه: خطا raise نمی‌شود ⇒ «درخواستی» شمرده می‌شود.
        return requested
    return min(counted, requested)


async def _delete_ids(bot, chat_id, ids, logger=None):
    """حذفِ دسته‌ای با **peer صحیحِ کانال** و شمارشِ واقعی."""
    peer = peers.group_peer(bot, chat_id)
    deleted = 0
    for start in range(0, len(ids), DELETE_BATCH_SIZE):
        batch = [int(value) for value in ids[start:start + DELETE_BATCH_SIZE]]
        if not batch:
            continue
        what = f"VIOLATOR DELETE chat={chat_id} count={len(batch)}"
        try:
            result = await _delete_batch_rpc(bot, peer, batch, logger=logger,
                                             what=what)
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            _note_failure(bot, chat_id, error, logger=logger, count=len(batch))
            continue
        deleted += _count_affected(result, len(batch))
    if deleted > 0:
        _record_stats(chat_id, deleted)
    return deleted


def _record_stats(chat_id, deleted_count) -> None:
    """ثبت در آمارِ موجود با **شمارندهٔ جدا** (نه اسپم، نه حذف دستی)."""
    try:
        group_stats.add_violator_deleted(chat_id, count=deleted_count)
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  خطاها: خلاصهٔ دقیقه‌ای + اطلاعٍ ساعتی به مالک
# ---------------------------------------------------------------------------
async def _note_failure(bot, chat_id, error, *, logger=None, count=1):
    """لاگِ خلاصه (یک بار در دقیقه) و اطلاع به مالک (یک بار در ساعت)."""
    key = storage.group_key(chat_id)
    state = _FAIL_STATE.setdefault(
        key, {"count": 0, "last_log": 0.0, "last_notify": 0.0})
    state["count"] += int(count)
    now = storage.monotonic()
    notify = False
    if now - state["last_log"] >= FAIL_LOG_INTERVAL:
        state["last_log"] = now
        logged = state["count"]
        state["count"] = 0
        _log(logger,
             f"VIOLATOR DELETE FAILED SUMMARY chat_id={chat_id} "
             f"failed={logged} last_error={error!r}", error=True)
        if now - state["last_notify"] >= OWNER_NOTIFY_INTERVAL:
            state["last_notify"] = now
            notify = True
    if notify:
        await _notify_owner(bot, chat_id, logger=logger)


async def _notify_owner(bot, chat_id, logger=None):
    """اطلاع به مالکِ ثبت‌شدهٔ گروه (در همان گروه، مثل قالبِ «گزارش»)."""
    from modules.manage_features import texts

    text, entities = texts.violator_rights_lost()
    try:
        peer = peers.group_peer(bot, chat_id)
        await call(bot.client.send_message, peer, text,
                   formatting_entities=entities, logger=logger,
                   timeout=RPC_TIMEOUT, max_retry=1,
                   what=f"VIOLATOR OWNER NOTIFY chat={chat_id}")
    except Exception as error:                       # noqa: BLE001
        _log(logger,
             f"VIOLATOR OWNER NOTIFY FAILED chat_id={chat_id} "
             f"error={error!r}")


# ---------------------------------------------------------------------------
#  بازیابی پس از ری‌استارت
# ---------------------------------------------------------------------------
def recover(bot, logger=None) -> dict:
    """بازیابی: رکوردها روی دیسک هستند و ایندکس بازسازی می‌شود.

    هیچ RPCای در مسیرِ راه‌اندازی زده نمی‌شود؛ فقط کش سازندهٔ گروه‌های
    دارای رکورد در پس‌زمینه پر می‌شود تا دفاع دومِ گیت از اولین پیام آماده
    باشد.
    """
    data = storage.VIOLATORS_STORAGE.load() or {}
    groups = 0
    users = 0
    if isinstance(data, dict):
        for chat_key, record in data.items():
            if not isinstance(record, dict) or not record:
                continue
            groups += 1
            users += sum(1 for entry in record.values()
                         if isinstance(entry, dict))
            _prime_creator_cache(bot, chat_key, logger)
    _refresh_index(force=True)
    _log(logger, f"VIOLATOR RECOVERY groups={groups} users={users}")
    return {"groups": groups, "users": users}


def reset_all() -> None:
    """پاک‌سازی کامل (تست/ری‌ست دستی)."""
    global _INDEX_READY
    storage.VIOLATORS_STORAGE.reset()
    _INDEX.clear()
    _INDEX_READY = False
    _PENDING.clear()
    _CREATOR_CACHE.clear()
    _CREATOR_LOCKS.clear()
    _FAIL_STATE.clear()
    for task in list(_FLUSHERS.values()):
        try:
            task.cancel()
        except Exception:                            # noqa: BLE001
            pass
    _FLUSHERS.clear()
