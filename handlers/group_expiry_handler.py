"""⏳ هندلر مستقل «تاریخ انقضای گروه».

تنها نقطهٔ اتصال این قابلیت به ربات. هیچ state ای اینجا نگه داشته
نمی‌شود و هیچ ماژول بازی/حافظه/قفلی import نمی‌گردد.

مسیر پردازش کاملاً جداست و تطبیق دستور «دقیق» است، پس هیچ
``startswith`` عمومی‌ای نمی‌تواند این سه دستور را با چیز دیگری اشتباه
بگیرد.
"""
try:
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:
    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

from modules.group_expiry import (
    EXPIRED_MESSAGE,
    add_notice,
    build_confirmation,
    build_expired_message,
    due_groups,
    is_expired,
    mark_notified,
    match_command,
    set_expiry,
    take_notices,
)
from modules.owner_check import is_global_owner

# پیامی که وقتی گروه منقضی است به غیرمالک نشان داده می‌شود.
EXPIRED_NOTICE = (
    "⛔ مدت زمان فعال بودن این گروه به پایان رسیده است.\n\n"
    "برای فعال‌سازی دوباره، مالک اصلی باید دستور انقضا "
    "(از «۱ روز» تا «۴۰ روز») را ارسال کند."
)

CHECK_INTERVAL_SECONDS = 5



# ---------------------------------------------------------------------------
# پاک‌سازی خودکار پیام‌های «اعلام مهلت»
# ---------------------------------------------------------------------------
# ریشهٔ باگ قبلی: پیام تأیید/اعلام مهلت هیچ‌جا برای حذف ثبت نمی‌شد، پس پس از
# پایان مهلت (یا تمدید بعدی) تا ابد در گروه می‌ماند. حالا شناسهٔ پیام در همان
# رکورد مهلت ذخیره می‌شود، با TTL «زمان باقی‌مانده» به موتور notice_cleanup
# سپرده می‌شود و در لحظهٔ انقضا هم صریحاً به صف حذف می‌رود؛ بنابراین حتی اگر
# پروسه ری‌استارت شود یا ناظر از کار بیفتد، پیام در پایان مهلت پاک می‌شود.
NOTICE_TTL_GRACE_SECONDS = 5.0
NOTICE_TTL_MAX_SECONDS = 60 * 24 * 3600


def _notice_ttl(expires_in_seconds):
    """TTL حذف پیام اعلام = زمان باقی‌مانده تا پایان مهلت (+ مهلت کوچک)."""
    try:
        remaining = float(expires_in_seconds)
    except (TypeError, ValueError):
        return NOTICE_TTL_GRACE_SECONDS
    if remaining < 0:
        remaining = 0.0
    return max(1.0, min(NOTICE_TTL_MAX_SECONDS, remaining + NOTICE_TTL_GRACE_SECONDS))


def _sent_message_id(sent):
    """شناسهٔ پیامی که همین حالا ارسال شد (هر شکل خروجی SPlusthon)."""
    if sent is None:
        return None
    try:
        from modules.notice_cleanup import extract_sent_id
        mid = extract_sent_id(sent)
    except Exception:
        mid = None
    if mid is not None:
        return mid
    for attr in ("id", "message_id"):
        candidate = getattr(sent, attr, None)
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return None


def track_notice(bot, chat_id, sent, *, ttl=None):
    """یک پیام «اعلام مهلت» را برای حذف در پایان مهلت ثبت می‌کند.

    ``ttl`` همان زمان باقی‌مانده تا انقضاست. اگر مقدار داده نشود، هر دو لایه
    (رکورد مهلت + موتور TTL) فقط ثبت می‌شوند و حذف در لحظهٔ انقضا انجام
    می‌گیرد.
    """
    message_id = _sent_message_id(sent)
    if message_id is None:
        return False
    stored = False
    try:
        stored = add_notice(chat_id, message_id)
    except Exception:
        stored = False
    cleanup = getattr(bot, "notice_cleanup", None)
    scheduled = False
    if cleanup is not None and ttl is not None:
        try:
            scheduled = bool(cleanup.schedule(chat_id, sent, ttl=ttl))
        except Exception:
            scheduled = False
    return bool(stored or scheduled)


async def delete_notices(bot, chat_id, message_ids):
    """پیام‌های اعلام‌شده را حذف می‌کند (هرگز استثنا پرتاب نمی‌کند).

    مسیر اصلی همان صف حذف پروژه است؛ اگر نبود، حذف مستقیم انجام می‌شود.
    خروجی: تعداد شناسه‌هایی که به صف/کلاینت سپرده شد.
    """
    ids = [int(mid) for mid in (message_ids or []) if isinstance(mid, int) and mid > 0]
    if not ids:
        return 0
    queue = getattr(bot, "message_delete_queue", None)
    if queue is not None:
        try:
            queue.enqueue(chat_id, ids, priority=1)
            return len(ids)
        except Exception as error:
            _log_error(getattr(bot, "logger", None),
                       f"EXPIRY NOTICE DELETE QUEUE FAILED chat_id={chat_id} "
                       f"error={error!r}")
    client = getattr(bot, "client", None)
    delete = getattr(client, "delete_messages", None)
    if callable(delete):
        try:
            await delete(chat_id, ids)
            return len(ids)
        except Exception as error:
            _log_error(getattr(bot, "logger", None),
                       f"EXPIRY NOTICE DELETE FAILED chat_id={chat_id} "
                       f"message_ids={ids!r} error={error!r}")
            return 0
    return 0


def _expires_in(result):
    """ثانیه‌های باقی‌مانده تا انقضا از خروجی ``set_expiry``."""
    if not result:
        return None
    try:
        return max(0.0, (result["expires_at"] - _utcnow()).total_seconds())
    except Exception:
        return None


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)

def _entities(spans):
    """تبدیل span های خنثی به entity واقعی splusthon."""
    built = []
    for kind, offset, length in spans:
        if kind == "blockquote":
            built.append(MessageEntityBlockquote(offset=offset, length=length))
        elif kind == "bold":
            built.append(MessageEntityBold(offset=offset, length=length))
    return built


def _log(logger, message):
    if logger is not None:
        try:
            logger.log_info(message)
        except Exception:
            pass


def _log_error(logger, message):
    if logger is not None:
        try:
            logger.log_error(message)
        except Exception:
            pass


async def handle(bot, event, chat_id, sender, text, logger=None):
    """اگر پیام یکی از سه دستور انقضا باشد آن را پردازش می‌کند.

    ``True`` یعنی پیام مصرف شد و هندلر اصلی نباید ادامه دهد.
    """
    command = match_command(text)
    if command is None:
        return False

    # فقط مالک اصلی. برای بقیه هیچ پاسخی داده نمی‌شود تا این دستورها
    # برای کاربران عادی اصلاً وجود نداشته باشند.
    if not is_global_owner(sender):
        _log(logger, "GROUP EXPIRY DENIED "
                     f"chat_id={chat_id} user_id={getattr(sender, 'id', None)} "
                     f"command={command!r} reason=not_global_owner")
        return True

    title = getattr(await _safe_chat(event), "title", "") or ""
    # پیام‌های «اعلام مهلت» قبلی همین گروه: با تعیین مهلت تازه کهنه می‌شوند.
    stale_ids = []
    try:
        stale_ids = take_notices(chat_id)
    except Exception:
        stale_ids = []
    result = set_expiry(chat_id, command, title=title)
    if result is None:
        _log_error(logger, f"GROUP EXPIRY SET FAILED chat_id={chat_id} "
                           f"command={command!r}")
        return True

    text_out, spans = build_confirmation(
        result["activated_at"], result["expires_at"])
    sent = await event.reply(text_out, formatting_entities=_entities(spans))
    if stale_ids:
        await delete_notices(bot, chat_id, stale_ids)
    if track_notice(bot, chat_id, sent, ttl=_notice_ttl(_expires_in(result))):
        _log(logger, f"EXPIRY NOTICE TRACKED chat_id={chat_id} "
                     f"expires_at={result['expires_at'].isoformat()}")
    else:
        _log_error(logger, "EXPIRY NOTICE TRACK FAILED "
                           f"chat_id={chat_id} (message_id نامعلوم)")
    _log(logger, "GROUP EXPIRY SET "
                 f"chat_id={chat_id} command={command!r} days={result['days']} "
                 f"activated_at={result['activated_at'].isoformat()} "
                 f"expires_at={result['expires_at'].isoformat()}")
    return True


async def _safe_chat(event):
    try:
        return await event.get_chat()
    except Exception:
        return None


def blocks_message(chat_id, sender):
    """آیا این پیام باید به دلیل انقضای گروه متوقف شود.

    مالک اصلی همیشه عبور می‌کند تا بتواند گروه را دوباره فعال کند.
    """
    if not is_expired(chat_id):
        return False
    return not is_global_owner(sender)


async def run_expiry_watcher(bot, deactivate, interval=None,
                             logger=None, iterations=None):
    """حلقهٔ پس‌زمینه: گروه‌های منقضی را بدون نیاز به هیچ پیامی می‌بندد.

    ``deactivate(chat_id, title)`` توسط فراخوان داده می‌شود تا این ماژول
    به storage گروه‌ها وابسته نشود.
    """
    import asyncio

    delay = CHECK_INTERVAL_SECONDS if interval is None else interval
    rounds = 0
    while True:
        try:
            await check_once(bot, deactivate, logger=logger)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_error(logger, f"GROUP EXPIRY WATCHER FAILED error={error!r}")
        rounds += 1
        if iterations is not None and rounds >= iterations:
            return rounds
        await asyncio.sleep(delay)


async def check_once(bot, deactivate, logger=None):
    """یک بار بررسی می‌کند و هر گروه منقضی را می‌بندد.

    تعداد گروه‌های بسته‌شده را برمی‌گرداند.
    """
    closed = 0
    due = due_groups()
    _log(logger, f"EXPIRY CHECK due_count={len(due)}")
    for key, record in due:
        title = record.get("title", "") or ""
        _log(logger, "EXPIRY FOUND "
                     f"group_id={key} expires_at={record.get('expires_at')} "
                     f"title={title!r}")
        try:
            short_key = int(key)
        except (TypeError, ValueError):
            short_key = None
        if short_key is not None:
            # شکل واقعی peer گروه؛ + شکل کوتاه به‌عنوان fallback.
            chat_id = -(_channel_offset() + short_key)
        else:
            chat_id = key

        _log(logger, "EXPIRY ACTION START "
                     f"group_id={chat_id} title={title!r}")
        # 📣 پیام‌های «اعلام مهلت» همین حالا بی‌اعتبار می‌شوند: پیش از هر
        # کاری پاک می‌شوند تا گروه پیام مهلتِ گذشته را نگه ندارد.
        try:
            pending_ids = take_notices(key)
        except Exception:
            pending_ids = []
        if pending_ids:
            removed = await delete_notices(bot, chat_id, pending_ids)
            _log(logger, "EXPIRY NOTICE CLEARED "
                         f"group_id={key} count={removed} ids={pending_ids!r}")
        try:
            deactivate(chat_id, title)
            _log(logger, f"GROUP EXPIRY DEACTIVATED chat_id={chat_id} "
                         f"title={title!r} expires_at={record.get('expires_at')}")
        except Exception as error:
            _log_error(logger, f"GROUP EXPIRY DEACTIVATE FAILED "
                               f"chat_id={chat_id} error={error!r}")
            continue

        message, spans = build_expired_message()
        sent_any = False
        for target in _targets(chat_id, key):
            try:
                sent = await bot.client.send_message(
                    target, message, formatting_entities=_entities(spans))
                cleanup = getattr(bot, "notice_cleanup", None)
                if cleanup is not None:
                    cleanup.schedule(target, sent)
                if not mark_notified(key):
                    _log_error(logger, "EXPIRY NOTIFICATION STATE FAILED "
                                       f"group_id={key}")
                    continue
                _log(logger, f"EXPIRY NOTIFICATION SENT group_id={key} chat_id={target}")
                _log(logger, f"GROUP EXPIRY NOTICE SENT chat_id={target}")
                sent_any = True
                break
            except Exception as error:
                _log_error(logger, f"GROUP EXPIRY NOTICE FAILED "
                                   f"chat_id={target} error={error!r}")
                error_text = f"{error!r}".upper()
                if "404" in error_text or "NOT_FOUND" in error_text:
                    mark_notified(key)
                    sent_any = True
                    _log_error(logger, f"EXPIRY TARGET INVALID REMOVED chat_id={target}")
                    break
        if not sent_any:
            mark_notified(key)
        closed += 1
    return closed


def _channel_offset():
    try:
        from modules.group_id import CHANNEL_ID_OFFSET
        return int(CHANNEL_ID_OFFSET)
    except Exception:
        return 1_000_000_000_000


def _targets(chat_id, key):
    """شکل‌های درست شناسهٔ گروه برای ارسال/حذف پیام.

    ⚠️ ریشهٔ باگ «پیام مهلت پاک نمی‌شود»: کلید رکورد انقضا **نرمال‌شده** است
    (مثبت، بدون پیشوند -۱۰۰). شکل مثبت در سروش یعنی «کاربر خصوصی»؛ پس پیام
    اعلام یا به گروه نمی‌رفت یا به کاربر اشتباهی می‌رفت و در نتیجه حذف هم
    روی همان peer غلط انجام می‌شد. حالا فقط شکل‌های منفیِ گروه (اول
    استاندارد -۱۰۰...، بعد شکل کوتاه) استفاده می‌شود.
    """
    targets = []

    def add(value):
        if isinstance(value, int) and value < 0 and value not in targets:
            targets.append(value)

    try:
        short = int(key)
    except (TypeError, ValueError):
        return [chat_id] if isinstance(chat_id, int) and chat_id < 0 else [chat_id]
    # اگر فراخوان خودش شناسهٔ منفی واقعی داده باشد، همان اولویت دارد.
    add(chat_id if isinstance(chat_id, int) else None)
    add(-(_channel_offset() + short))
    add(-short)
    return targets or [chat_id]
