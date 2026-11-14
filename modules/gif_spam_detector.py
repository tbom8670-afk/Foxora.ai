"""ضداسپم GIF — کاملاً مستقل از سایر فیلترها.

این ماژول مسیر، حالت و صف حذف مخصوص خودش را دارد و هیچ ساختار داده‌ای با
ضداسپم متن، فوروارد یا پیام تکراری به اشتراک نمی‌گذارد.

مشکل قبلی
---------
شمارنده پس از هر بار حذف صفر می‌شد و کاربر «محکوم‌شده» دوباره از صفر شمرده
می‌شد. بنابراین GIFهایی که بعد از دستهٔ اول می‌رسیدند تا وقتی به آستانه
نمی‌رسیدند در گروه باقی می‌ماندند:

    ۱۰ گیف → ۶ حذف، ۴ باقی
    ۲۵ گیف → ۲۴ حذف، ۱ باقی

راه‌حل
------
پس از رسیدن به آستانه، کاربر وارد حالت «flagged» می‌شود و از آن پس **هر** GIF
او بلافاصله برای حذف صف می‌شود. حذف‌ها در یک صف اختصاصی جمع و به‌صورت دسته‌ای
با تلاش مجدد ارسال می‌شوند تا هیچ GIFی جا نماند.
"""
import asyncio
import time
from collections import defaultdict, deque

# آستانهٔ تشخیص: با رسیدن به این تعداد GIF پیاپی، کاربر flagged می‌شود.
GIF_THRESHOLD = 6

# مدت زمانی که کاربر پس از تشخیص، در حالت flagged می‌ماند (ثانیه).
FLAG_DURATION = 3600

# فاصلهٔ جمع‌آوری صف پیش از ارسال دسته‌ای حذف (ثانیه).
FLUSH_DELAY = 0.35

# حداکثر تلاش برای حذف هر دسته.
MAX_DELETE_ATTEMPTS = 3

# شمارندهٔ GIFهای پیاپی هر کاربر — فقط برای همین ماژول.
GIF_COUNTER = defaultdict(lambda: deque(maxlen=GIF_THRESHOLD))

# آخرین GIF هر کلید؛ بدون این، شمارنده تا ری‌استارت می‌ماند.
_COUNTER_TOUCHED = {}

# کاربرانی که آستانه را رد کرده‌اند: (chat_id, user_id) -> زمان انقضا.
_FLAGGED = {}

# صف حذف اختصاصی GIF: chat_id -> مجموعهٔ message_id.
_DELETE_QUEUE = defaultdict(set)
_FLUSH_TASKS = {}

# آمار برای تست و لاگ.
_STATS = defaultdict(int)


def _document_from_message(message):
    return (
        getattr(message, "document", None)
        or getattr(getattr(message, "media", None), "document", None)
    )


def is_gif_message(message):
    document = _document_from_message(message)
    if not document:
        return False

    mime_type = (getattr(document, "mime_type", None) or "").lower()
    attributes = getattr(document, "attributes", None) or []
    animated = any("Animated" in attr.__class__.__name__ for attr in attributes)
    return (
        bool(getattr(message, "gif", False))
        or bool(getattr(message, "animation", None))
        or mime_type == "image/gif"
        or animated
    )


def _now():
    return time.monotonic()


def is_flagged(chat_id, user_id):
    """آیا این کاربر در حالت اسپم GIF است."""
    expiry = _FLAGGED.get((chat_id, user_id))
    if expiry is None:
        return False
    if expiry <= _now():
        _FLAGGED.pop((chat_id, user_id), None)
        GIF_COUNTER.pop((chat_id, user_id), None)
        _COUNTER_TOUCHED.pop((chat_id, user_id), None)
        return False
    return True


def flag_user(chat_id, user_id, duration=FLAG_DURATION):
    _FLAGGED[(chat_id, user_id)] = _now() + duration


def reset_gif_history(chat_id, user_id):
    """فقط شمارنده را صفر می‌کند؛ حالت flagged دست‌نخورده می‌ماند.

    این تفکیک عمدی است: پاک‌کردن شمارنده نباید کاربری را که قبلاً تشخیص داده
    شده دوباره آزاد کند، وگرنه همان باگِ «چند گیف باقی می‌ماند» برمی‌گردد.
    """
    GIF_COUNTER.pop((chat_id, user_id), None)
    _COUNTER_TOUCHED.pop((chat_id, user_id), None)


def clear_user(chat_id, user_id):
    """پاک‌سازی کامل: هم شمارنده و هم حالت flagged."""
    GIF_COUNTER.pop((chat_id, user_id), None)
    _COUNTER_TOUCHED.pop((chat_id, user_id), None)
    _FLAGGED.pop((chat_id, user_id), None)


def track_gif(chat_id, user_id, message_id):
    """یک GIF را ثبت می‌کند و می‌گوید چه پیام‌هایی باید حذف شوند.

    خروجی: ``(message_ids, newly_flagged)``

    * تا پیش از آستانه: ``([], False)``
    * دقیقاً در آستانه: کل دسته + ``True``
    * پس از آن تا پایان مهلت: همان یک پیام + ``False``
    """
    key = (chat_id, user_id)

    # کاربر قبلاً تشخیص داده شده: هر GIF تازه بلافاصله حذف می‌شود.
    if is_flagged(chat_id, user_id):
        _STATS["flagged_hits"] += 1
        return [message_id], False

    history = GIF_COUNTER[key]
    history.append(message_id)
    _COUNTER_TOUCHED[key] = _now()

    if len(history) >= GIF_THRESHOLD:
        batch = list(history)
        GIF_COUNTER.pop(key, None)
        _COUNTER_TOUCHED.pop(key, None)
        flag_user(chat_id, user_id)
        _STATS["threshold_hits"] += 1
        # Keep the first GIF as the representative; only duplicates are
        # scheduled for deletion.
        return batch[1:], True

    return [], False


def queue_delete(chat_id, message_ids):
    """پیام‌ها را به صف حذف اختصاصی GIF اضافه می‌کند."""
    if not message_ids:
        return 0
    before = len(_DELETE_QUEUE[chat_id])
    _DELETE_QUEUE[chat_id].update(message_ids)
    return len(_DELETE_QUEUE[chat_id]) - before


def pending_count(chat_id=None):
    if chat_id is None:
        return sum(len(v) for v in _DELETE_QUEUE.values())
    return len(_DELETE_QUEUE.get(chat_id, ()))


async def flush_deletes(client, chat_id, logger=None):
    """صف حذف یک چت را با تلاش مجدد خالی می‌کند.

    پیام‌هایی که حذف نشوند به صف برمی‌گردند تا در نوبت بعد دوباره تلاش شود؛
    بنابراین هیچ GIFی به‌خاطر یک خطای گذرا جا نمی‌ماند.
    """
    pending = _DELETE_QUEUE.pop(chat_id, set())
    if not pending:
        return 0

    message_ids = sorted(pending)
    deleted = 0
    # The API accepts at most 100 ids per request. Process every chunk
    # independently so one oversized/failed request cannot strand the tail.
    for start in range(0, len(message_ids), 100):
        batch = message_ids[start:start + 100]
        remaining = list(batch)
        for attempt in range(1, MAX_DELETE_ATTEMPTS + 1):
            if not remaining:
                break
            try:
                await client.delete_messages(chat_id, remaining)
                deleted += len(remaining)
                _STATS["deleted"] += len(remaining)
                remaining = []
            except Exception as error:
                if logger is not None:
                    logger.log_error(
                        f"GIF DELETE ATTEMPT {attempt} FAILED chat_id={chat_id} "
                        f"count={len(remaining)} error={error!r}"
                    )
                if attempt < MAX_DELETE_ATTEMPTS:
                    await asyncio.sleep(0.2 * attempt)
        if remaining:
            # Isolate a bad id after batch retries; survivors stay queued for
            # the next flush rather than being silently discarded.
            survivors = []
            for message_id in remaining:
                try:
                    await client.delete_messages(chat_id, [message_id])
                    deleted += 1
                    _STATS["deleted"] += 1
                except Exception:
                    survivors.append(message_id)
            if survivors:
                _DELETE_QUEUE[chat_id].update(survivors)
                _STATS["failed"] += len(survivors)
    return deleted


def schedule_flush(client, chat_id, logger=None, delay=FLUSH_DELAY):
    """یک بار flush زمان‌بندی می‌کند و GIFهای پشت‌سرهم را دسته می‌کند."""
    task = _FLUSH_TASKS.get(chat_id)
    if task is not None and not task.done():
        return task

    async def runner():
        try:
            await asyncio.sleep(delay)
            await flush_deletes(client, chat_id, logger)
        finally:
            _FLUSH_TASKS.pop(chat_id, None)

    try:
        new_task = asyncio.get_running_loop().create_task(runner())
    except RuntimeError:
        return None
    _FLUSH_TASKS[chat_id] = new_task
    return new_task


def handle_gif(chat_id, user_id, message_id, client=None, logger=None,
               delete_queue=None):
    """مسیر کامل و مستقل GIF: ثبت، صف‌بندی و زمان‌بندی حذف.

    خروجی ``(queued_ids, newly_flagged)``.

    If ``delete_queue`` is provided (the per-chat MessageDeleteQueue), GIF
    IDs go there so a GIF flood cannot spawn a second global RPC loop.
    Tests that pass only ``client`` keep the original schedule_flush path.
    """
    message_ids, newly_flagged = track_gif(chat_id, user_id, message_id)
    if message_ids:
        queue_delete(chat_id, message_ids)
        if delete_queue is not None:
            delete_queue.enqueue(chat_id, message_ids, priority=1)
            _DELETE_QUEUE.pop(chat_id, None)
        elif client is not None:
            schedule_flush(client, chat_id, logger)
    return message_ids, newly_flagged


def stats():
    return dict(_STATS)


def cleanup_expired(now=None):
    """Drop idle GIF counters and expired flags. Owner is the 60s loop.

    Keys used to live until the next GIF or a process restart. In busy
    groups that is one deque per unique sender for the whole uptime.
    """
    now = _now() if now is None else now
    removed = 0
    for key, expiry in list(_FLAGGED.items()):
        if expiry <= now:
            _FLAGGED.pop(key, None)
            GIF_COUNTER.pop(key, None)
            _COUNTER_TOUCHED.pop(key, None)
            removed += 1
    for key, seen in list(_COUNTER_TOUCHED.items()):
        if key in _FLAGGED:
            continue
        if now - seen >= FLAG_DURATION:
            GIF_COUNTER.pop(key, None)
            _COUNTER_TOUCHED.pop(key, None)
            removed += 1
    for key in list(GIF_COUNTER):
        if key not in _COUNTER_TOUCHED and key not in _FLAGGED:
            GIF_COUNTER.pop(key, None)
            removed += 1
    for chat_id, task in list(_FLUSH_TASKS.items()):
        done = getattr(task, "done", None)
        try:
            is_done = bool(done()) if callable(done) else True
        except Exception:
            is_done = True
        if is_done:
            _FLUSH_TASKS.pop(chat_id, None)
    return removed


def reset_all():
    """پاک‌سازی کامل وضعیت — برای تست."""
    GIF_COUNTER.clear()
    _COUNTER_TOUCHED.clear()
    _FLAGGED.clear()
    _DELETE_QUEUE.clear()
    _FLUSH_TASKS.clear()
    _STATS.clear()
