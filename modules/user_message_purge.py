"""🧹 پاکسازی کامل پیام‌های یک کاربر در یک گروه («پاک کامل»).

قابلیتی مستقل و خودکار است: هیچ تنظیمِ روشن/خاموشی ندارد و همان لحظه که
یک مالک/ادمینِ ثبت‌شده روی پیام کاربری ریپلای و «پاک کامل» بفرستد اجرا
می‌شود. بررسی مجوز، cooldown و قفل گروه در ``handlers/message_handler``
(کنار دستورهای «پاک» و «پاک + عدد») انجام می‌شود؛ این ماژول فقط هستهٔ
قابل تستِ «جمع‌آوری شناسه‌ها + حذف دسته‌ای» است.

چرا ``iter_messages`` و نه دیتابیس؟
    دیتابیس ``~/.foxora/db/bot.sqlite3`` (``modules/runtime_db``) هیچ
    جدولی برای پیام‌ها ندارد — فقط ``banned_users``/``user_activity``/
    ``admin_events``/``runtime_kv`` — پس ``message_id`` ها از SQLite
    قابل گرفتن نیستند. ``modules/message_tracker`` هم حافظه‌ای است
    (حداکثر ۵۰۰ پیام به‌ازای هر کاربر، نگه‌داری ۳۰ دقیقه) و فقط به‌عنوان
    منبع کمکیِ سریع با نتیجهٔ اسکن تاریخچه ادغام می‌شود. منبع اصلی و
    مرجع، ``client.iter_messages`` سروش‌پلاس (SPlusthon) است.

حذف:
    مسیر اصلی همان صفِ حذف موجود پروژه (``MessageDeleteQueue`` با
    ``priority=0`` مثل «پاک» دستی) است؛ آن صف خودش حذف را دسته‌ای
    (در عمل ۱۵تایی — طبق کامنت core، سروش درخواست حذف ۱۰۰تایی را
    مرتب timeout می‌کند)، با timeout و مدیریت FloodWait انجام می‌دهد و
    ``delete_messages`` در SPlusthon به‌صورت پیش‌فرض ``revoke=True``
    دارد (حذف برای همه). اگر صف روی bot نبود (تست/حداقل‌سازی)، مسیر
    جایگزین مستقیم با دسته‌های ۱۰۰تایی، ``revoke=True`` صریح و
    retry روی FloodWait/خطای شبکه اجرا می‌شود.

محدودهٔ اسکن:
    «از زمان فعال بودن ربات» تاریخ دقیقی در ``groups.json`` ندارد
    (فقط ``title``/``active`` ذخیره می‌شود). اگر برای گروه رکورد مهلت
    ثبت شده باشد، ``group_expiry.activated_at`` کف تاریخ اسکن می‌شود؛
    در غیر این صورت کل تاریخچهٔ در دسترس حساب ربات تا سقف
    ``FOXORA_PURGE_SCAN_LIMIT`` (پیش‌فرض ۵۰۰۰ پیامِ اخیر گروه، یا با
    فیلتر سروری ``from_user`` تا ۵۰۰۰ پیامِ خودِ کاربر) اسکن می‌شود.
    پیام‌های سیستمی (service) و پیام‌های خود ربات هرگز جمع نمی‌شوند.
"""
from __future__ import annotations

import asyncio
import inspect
import os

from modules import message_tracker

# دستور فارسی اصلی + معادل انگلیسی. مقایسه بعد از نرمال‌سازی انجام می‌شود
# (نیم‌فاصله، نویسهٔ عربی ی/ک، فاصلهٔ تکراری و پیشوند اختیاری ! / .).
COMMANDS = frozenset({"پاک کامل", "purge user", "purge"})

DEFAULT_SCAN_LIMIT = 5000
_MIN_SCAN_LIMIT = 100
_MAX_SCAN_LIMIT = 20000

# فقط مسیر جایگزینِ بدون صف؛ مسیر اصلی اندازهٔ دسته را از صف موجود می‌گیرد.
DELETE_BATCH_SIZE = 100

_FLOOD_WAIT_CAP_SECONDS = 30.0
_MAX_BATCH_ATTEMPTS = 3

_NORMALIZE_MAP = {
    "\u200c": " ",  # ZWNJ
    "\u200f": "",  # RTL mark
    "\u200e": "",  # LTR mark
    "\u064a": "\u06cc",  # Arabic yeh -> Persian yeh
    "\u0643": "\u06a9",  # Arabic kaf -> Persian kaf
}

# ارقام قالب تأیید طبق نمونهٔ خواسته‌شده: Mathematical Bold
# (U+1D7CE..U+1D7D7؛ مثلاً ۴ ← U+1D7D2)
_MATH_DIGITS = str.maketrans("0123456789", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗")


def _log_info(bot, message):
    logger = getattr(bot, "logger", None)
    if logger is not None:
        try:
            logger.log_info(message)
        except Exception:
            pass


def _log_error(bot, message):
    logger = getattr(bot, "logger", None)
    if logger is not None:
        try:
            logger.log_error(message)
        except Exception:
            pass


def normalize_purge_text(text):
    """یکسان‌سازی متن دستور، هم‌سبک ``normalize_command`` هندلر."""
    if not text:
        return ""
    normalized = str(text)
    for source, target in _NORMALIZE_MAP.items():
        normalized = normalized.replace(source, target)
    normalized = " ".join(normalized.split())
    if normalized[:1] in {"!", "/", "."}:
        normalized = normalized.lstrip("./!").strip()
    return normalized.lower()


def is_purge_command(text):
    """«پاک کامل» (و معادل انگلیسی) بعد از نرمال‌سازی اسمی دقیق تطبیق دارد؟"""
    return normalize_purge_text(text) in COMMANDS


def scan_limit():
    """سقف اسکن تاریخچه؛ با ``FOXORA_PURGE_SCAN_LIMIT`` قابل تنظیم است."""
    raw = os.environ.get("FOXORA_PURGE_SCAN_LIMIT", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_SCAN_LIMIT
    except (TypeError, ValueError):
        value = DEFAULT_SCAN_LIMIT
    return max(_MIN_SCAN_LIMIT, min(_MAX_SCAN_LIMIT, value))


def confirmation_text(deleted_count):
    """قالب ثابت پیام تأیید نهایی؛ عدد با ارقام Mathematical Bold."""
    try:
        count = max(0, int(deleted_count))
    except (TypeError, ValueError):
        count = 0
    return f"پیام های کاربر پاک شد تعداد : {str(count).translate(_MATH_DIGITS)}"


def message_sender_id(message):
    """شناسهٔ فرستنده از روی خود پیام، بدون هیچ RPC.

    پیام‌های سیستمی/کانالی فرستندهٔ کاربری ندارند و ``None`` برمی‌گردد.
    """
    if message is None:
        return None
    sender_id = getattr(message, "sender_id", None)
    if sender_id is None:
        from_id = getattr(message, "from_id", None)
        sender_id = getattr(from_id, "user_id", None)
    return sender_id


def _is_service_message(message):
    """پیام سیستمی سروش (ورود/خروج/سنجاق…) هرگز هدف پاکسازی نیست."""
    if message is None:
        return True
    if getattr(message, "action", None) is not None:
        return True
    return type(message).__name__ == "MessageService"


def _flood_wait_seconds(error):
    """مدت FloodWait بدون وابستگی به کلاس خاص نسخهٔ SPlusthon.

    هم‌الگو با ``modules/message_delete_queue`` و ``moderation_queue``:
    نام/متن خطا باید flood داشته باشد و مقدار از ``seconds``/``value``.
    """
    name = error.__class__.__name__.lower()
    try:
        text = str(error).lower()
    except Exception:
        text = ""
    if "flood" not in name and "flood" not in text:
        return None
    value = getattr(error, "seconds", None)
    if value is None:
        value = getattr(error, "value", None)
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return None


def _id_specific_error(error):
    """خطایی که فقط به یک message_id بد مربوط است (جداسازی مجاز است)."""
    name = error.__class__.__name__.lower()
    try:
        text = str(error).lower().replace(" ", "_").replace("-", "_")
    except Exception:
        text = ""
    return (
        "messageidinvalid" in name
        or "msgidinvalid" in name
        or "message_id_invalid" in text
        or "msg_id_invalid" in text
    )


def activation_since(chat_id):
    """کف تاریخ اسکن از رکورد مهلت گروه؛ اگر رکوردی نیست ``None``.

    ``groups.json`` زمان فعال‌سازی نگه نمی‌دارد؛ تنها منبع موجود
    ``group_expiry.activated_at`` است و فقط برای گروه‌های دارای مهلت.
    نبودنش یعنی اسکن کل تاریخچهٔ محدودشده — تاریخ جعلی ساخته نمی‌شود.
    """
    try:
        from modules import group_expiry

        return group_expiry.activated_at(chat_id)
    except Exception:
        return None


def _before_since(message, since):
    """پیام قدیمی‌تر از کف تاریخ است؟ (iter از تازه به قدیمی می‌آید)."""
    if since is None:
        return False
    message_date = getattr(message, "date", None)
    if message_date is None:
        return False
    try:
        return message_date < since
    except TypeError:
        # aware/naive mismatch: مقایسهٔ نامطمئن را به حذف اشتباه ترجیح نده.
        return False


async def _resolve_sender_id(message, target_id):
    """sender_id همگام؛ فقط اگر نبود سراغ ``get_sender()`` (مثل ``_same_sender``)."""
    sender_id = message_sender_id(message)
    if sender_id is not None:
        return sender_id
    get_sender = getattr(message, "get_sender", None)
    if callable(get_sender):
        try:
            sender = await get_sender()
        except Exception:
            return None
        return getattr(sender, "id", None)
    return None


def _make_history_iterator(iterator_factory, target, *, limit, from_user):
    """ساخت ایتریتور تاریخچه با سازگاری امضای کلاینت‌های مختلف.

    SPlusthon واقعی ``from_user`` و ``wait_time`` را دارد؛ کلاینت‌های
    جعلی تست‌ها معمولاً فقط ``(target, limit)`` — با ``TypeError`` پله‌پله
    به ساده‌ترین امضا برمی‌گردیم. خطای RPC سرور اینجا رخ نمی‌دهد و در
    حلقهٔ ``async for`` مدیریت می‌شود.
    """
    attempts = []
    if from_user is not None:
        attempts.append({"limit": limit, "from_user": from_user, "wait_time": 1})
        attempts.append({"limit": limit, "from_user": from_user})
    attempts.append({"limit": limit, "wait_time": 1})
    attempts.append({"limit": limit})

    last_error = None
    for kwargs in attempts:
        try:
            return iterator_factory(target, **kwargs)
        except TypeError as error:
            last_error = error
            continue
    raise last_error if last_error is not None else TypeError("iter_messages")


async def collect_user_message_ids(
    bot,
    chat_id,
    target_id,
    *,
    seed_ids=(),
    exclude_ids=(),
    target_user=None,
    rpc_peer=None,
    limit=None,
    since=None,
):
    """همهٔ message_id های کاربر هدف در گروه: ردیاب حافظه‌ای ∪ اسکن تاریخچه.

    برمی‌گرداند: ``(ids: set[int], scanned: int)``. اسکن ابتدا با فیلتر
    سروری ``from_user`` تلاش می‌شود (سقف اسکن روی پیام‌های خود کاربر
    اعمال می‌شود) و فرستندهٔ هر پیام باز هم دستی راستی‌آزمایی می‌شود؛
    اگر سرور/کلاینت آن فیلتر را نپذیرد، اسکن سادهٔ تاریخچه با فیلتر
    دستی — همان الگوی ``collect_accessible_spam_ids`` — انجام می‌شود.
    """
    ids = {
        message_id
        for message_id in (seed_ids or ())
        if isinstance(message_id, int) and message_id > 0
    }
    excluded = {
        message_id
        for message_id in (exclude_ids or ())
        if isinstance(message_id, int) and message_id > 0
    }
    # ردیاب حافظه‌ای: بدون RPC؛ پیام‌های ۳۰ دقیقهٔ اخیر همین کاربر/گروه.
    try:
        tracked = message_tracker.spam_snapshot(chat_id, target_id)
    except Exception:
        tracked = ()
    ids.update(
        message_id
        for message_id in tracked
        if isinstance(message_id, int) and message_id > 0
    )
    ids -= excluded

    bot_account_id = getattr(bot, "bot_account_id", None) or getattr(
        bot, "account_id", None
    )
    if bot_account_id is not None and str(target_id) == str(bot_account_id):
        _log_info(
            bot,
            "USER PURGE COLLECT SKIPPED BOT ITSELF "
            f"chat_id={chat_id} user_id={target_id}",
        )
        return set(), 0

    client = getattr(bot, "client", None)
    iterator_factory = getattr(client, "iter_messages", None)
    if not callable(iterator_factory):
        _log_error(
            bot,
            "USER PURGE HISTORY UNAVAILABLE "
            f"chat_id={chat_id} user_id={target_id} tracked_ids={len(ids)}",
        )
        return ids, 0

    if since is None:
        since = activation_since(chat_id)
    effective_limit = scan_limit() if limit is None else max(1, int(limit))
    target = rpc_peer if rpc_peer is not None else chat_id
    # موجودیت کاربر برای from_user بهتر از عدد خام است (resolve اضافی نمی‌خواهد).
    from_user_candidate = target_user if target_user is not None else target_id

    scanned = 0
    for use_from_user in (True, False):
        from_user = from_user_candidate if use_from_user else None
        mode_scanned = 0
        for attempt in range(2):
            mode_scanned = 0
            try:
                iterator = _make_history_iterator(
                    iterator_factory, target,
                    limit=effective_limit, from_user=from_user,
                )
                async for message in iterator:
                    scanned += 1
                    mode_scanned += 1
                    if _before_since(message, since):
                        # از تازه به قدیمی می‌آییم؛ بقیه قدیمی‌ترند.
                        return ids, scanned
                    if _is_service_message(message):
                        continue
                    sender_id = await _resolve_sender_id(message, target_id)
                    if sender_id is None or str(sender_id) != str(target_id):
                        continue
                    if (
                        bot_account_id is not None
                        and str(sender_id) == str(bot_account_id)
                    ):
                        continue
                    message_id = getattr(message, "id", None)
                    if (
                        isinstance(message_id, int)
                        and message_id > 0
                        and message_id not in excluded
                    ):
                        ids.add(message_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                flood_wait = _flood_wait_seconds(error)
                if flood_wait is not None and attempt == 0:
                    delay = min(flood_wait, _FLOOD_WAIT_CAP_SECONDS)
                    _log_error(
                        bot,
                        "USER PURGE HISTORY FLOODWAIT "
                        f"chat_id={chat_id} user_id={target_id} "
                        f"wait_s={delay:.1f} from_user={use_from_user}",
                    )
                    await asyncio.sleep(delay)
                    continue
                _log_error(
                    bot,
                    "USER PURGE HISTORY FAILED "
                    f"chat_id={chat_id} user_id={target_id} "
                    f"scanned={scanned} matched={len(ids)} "
                    f"from_user={use_from_user} error={error!r}",
                )
                # فیلتر سروری پذیرفته نشد و هنوز چیزی اسکن نشده: اسکن ساده.
                if use_from_user and mode_scanned == 0:
                    break
                return ids, scanned
            else:
                _log_info(
                    bot,
                    "USER PURGE COLLECT COMPLETE "
                    f"chat_id={chat_id} user_id={target_id} "
                    f"scanned={scanned} matched={len(ids)} "
                    f"from_user={use_from_user} limit={effective_limit}",
                )
                return ids, scanned
    return ids, scanned


async def _delete_batch_revoke(client, chat_id, batch):
    """یک دسته حذف با ``revoke=True`` صریح (حذف برای همه).

    SPlusthon واقعی: ``delete_messages(entity, ids, *, revoke=True)``.
    اگر فورک/کلاینت جعلی این کلیدواژه را نداشت، یک بار بدون آن تلاش
    می‌شود (پیش‌فرض خود SPlusthon هم ``revoke=True`` است).
    """
    try:
        await client.delete_messages(chat_id, batch, revoke=True)
    except TypeError as error:
        if "revoke" not in str(error).lower() and "unexpected" not in str(
            error
        ).lower() and "keyword" not in str(error).lower():
            raise
        await client.delete_messages(chat_id, batch)


async def _delete_direct_in_batches(bot, chat_id, pending, *, batch_size):
    """مسیر جایگزین بدون صف: دسته‌های bounded با retry روی FloodWait/شبکه."""
    deleted = 0
    remaining = []
    client = getattr(bot, "client", None)
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        await asyncio.sleep(0)
        succeeded = False
        last_error = None
        for attempt in range(1, _MAX_BATCH_ATTEMPTS + 1):
            try:
                await _delete_batch_revoke(client, chat_id, batch)
                succeeded = True
                deleted += len(batch)
                break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                last_error = error
                _log_error(
                    bot,
                    "USER PURGE DELETE BATCH FAILED "
                    f"chat_id={chat_id} count={len(batch)} "
                    f"attempt={attempt} error={error!r}",
                )
                if attempt >= _MAX_BATCH_ATTEMPTS:
                    break
                flood_wait = _flood_wait_seconds(error)
                if flood_wait is not None:
                    if flood_wait > _FLOOD_WAIT_CAP_SECONDS:
                        # انتظار خیلی طولانی: این دسته و بقیه را نگه دار و
                        # با همان تعداد واقعیِ حذف‌شده گزارش بده.
                        remaining.extend(pending[start:])
                        _log_error(
                            bot,
                            "USER PURGE DELETE ABORTED FLOODWAIT "
                            f"chat_id={chat_id} wait_s={flood_wait:.1f} "
                            f"deleted={deleted} remaining={len(remaining)}",
                        )
                        return deleted, remaining
                    await asyncio.sleep(flood_wait)
                else:
                    await asyncio.sleep(0.5 * attempt)
        if succeeded:
            continue
        # فقط خطای خاصِ یک ID بد جداسازی تکی را توجیه می‌کند؛ مثل صف حذف.
        if last_error is not None and _id_specific_error(last_error):
            for message_id in batch:
                try:
                    await _delete_batch_revoke(client, chat_id, [message_id])
                    deleted += 1
                except asyncio.CancelledError:
                    raise
                except Exception as item_error:
                    remaining.append(message_id)
                    _log_error(
                        bot,
                        "USER PURGE DELETE ITEM FAILED "
                        f"chat_id={chat_id} message_id={message_id} "
                        f"error={item_error!r}",
                    )
        else:
            remaining.extend(batch)
    return deleted, remaining


async def delete_ids_in_batches(
    bot, chat_id, message_ids, *, batch_size=DELETE_BATCH_SIZE
):
    """حذف دسته‌ای با اولویت دستی؛ صف موجود پروژه اول، حذف مستقیم بعد.

    برمی‌گرداند ``(deleted_count, remaining_ids)`` با تعداد واقعی موفق —
    همان قراردادی که «پاک + عدد» برای گزارش نهایی استفاده می‌کند.
    """
    pending = sorted({
        message_id
        for message_id in (message_ids or ())
        if isinstance(message_id, int) and message_id > 0
    })
    if not pending:
        return 0, []

    queue = getattr(bot, "message_delete_queue", None)
    if queue is not None:
        try:
            result = queue.enqueue(chat_id, pending, priority=0)
        except TypeError:
            result = queue.enqueue(chat_id, pending)
        if inspect.isawaitable(result):
            result = await result
        deleted, remaining = result
        return int(deleted), list(remaining or ())

    return await _delete_direct_in_batches(
        bot, chat_id, pending, batch_size=max(1, int(batch_size))
    )


async def purge_user_messages(
    bot,
    chat_id,
    target_id,
    *,
    seed_ids=(),
    exclude_ids=(),
    target_user=None,
    rpc_peer=None,
    limit=None,
    since=None,
):
    """جمع‌آوری + حذف کامل؛ برمی‌گرداند ``(deleted_count, remaining_ids)``."""
    ids, scanned = await collect_user_message_ids(
        bot,
        chat_id,
        target_id,
        seed_ids=seed_ids,
        exclude_ids=exclude_ids,
        target_user=target_user,
        rpc_peer=rpc_peer,
        limit=limit,
        since=since,
    )
    deleted, remaining = await delete_ids_in_batches(bot, chat_id, ids)
    if deleted:
        # شناسه‌های واقعاً حذف‌شده از ردیاب حافظه‌ای هم فراموش شوند.
        deleted_ids = set(ids) - set(remaining or ())
        try:
            message_tracker.remove_message_ids(chat_id, target_id, deleted_ids)
        except Exception:
            pass
    _log_info(
        bot,
        "USER PURGE FINISHED "
        f"chat_id={chat_id} user_id={target_id} scanned={scanned} "
        f"selected={len(ids)} deleted={deleted} remaining={len(remaining)}",
    )
    return deleted, remaining
