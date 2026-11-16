"""ردیابیِ پاسخ‌های خودِ ربات برای پاکسازیِ همراه با پیامِ ویرایش‌شده.

سناریوی هدف
------------
کاربر پیامی مثل «سلام» می‌فرستد و ربات پاسخ می‌دهد («سلام 👋»). سپس همان
کاربر (که در عمل اغلب یک رباتِ اسپمر است) پیامش را خیلی سریع به یک متنِ
فیلترشده/هرزنامه ویرایش می‌کند. خطِ لولهٔ مدیریت، پیامِ ویرایش‌شده را حذف
می‌کند، اما پاسخِ قبلیِ ربات سر جایش می‌ماند. نتیجه این است که گروه شلوغ و
«ربات‌گونه» به نظر می‌رسد؛ انگار خودِ ربات دارد «سلام» یا پیامِ خالی
می‌فرستد.

این ماژول نگاشتِ ``(chat_id, user_message_id) -> [bot_reply_message_id, ...]``
را در حافظه نگه می‌دارد تا مسیرِ حذف بتواند پاسخِ(های) ربات را هم‌زمان با
خودِ پیامِ ویرایش‌شده پاک کند. چون ویرایش در سروش/تلگرام شناسهٔ پیام را
تغییر نمی‌دهد، همان کلید در هر دو رویداد (پیامِ جدید و پیامِ ویرایش‌شده)
یکسان می‌ماند و نیازی به هیچ RPC اضافه‌ای برای «پیدا کردنِ» پاسخ نیست.

طراحی
-----
* فقط حافظه، بدونِ I/O و بدونِ وابستگی به splusthon → مسیرِ گرمِ پیام را کند
  نمی‌کند و به‌راحتی به‌صورت مستقل تست می‌شود.
* تک‌رشته‌ای (حلقهٔ asyncio): هیچ ``await``ی داخل توابعِ این ماژول نیست، پس
  عملیاتِ ``OrderedDict`` هرگز وسطِ کار قطع نمی‌شود و نیازی به قفل نیست.
* TTL و سقفِ تعداد: تا روی گروه‌های شلوغ بی‌رویه رشد نکند.
* «بهترین تلاش»: هیچ خطایی از این ماژول نباید مسیرِ پیام را بشکند؛ فراخوان
  مسئولِ بلعیدنِ استثناها است، ولی توابعِ این‌جا هم تدافعی نوشته شده‌اند.
"""

import time
from collections import OrderedDict

try:
    # یکسان‌سازیِ شناسهٔ گروه تا شکلِ کوتاه (event_chat.id) و شکلِ علامت‌دار
    # (event.chat_id = -100...) هر دو به یک کلیدِ یکسان نگاشت شوند.
    from modules.group_id import normalize_group_id as _normalize_chat_id
except Exception:  # pragma: no cover - چیدمانِ ماژول عوض شود
    def _normalize_chat_id(value):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)


# پیامی که به متنِ فیلترشده ویرایش می‌شود تقریباً همیشه در عرضِ چند ثانیه
# ویرایش می‌شود؛ کمی بیشتر نگه می‌داریم تا مطمئن باشیم، بعد منقضی می‌کنیم.
_TTL_SECONDS = 10 * 60

# سقفِ کلِ ورودی‌ها تا حافظه روی گروه‌های پرحجم بی‌رویه رشد نکند.
_MAX_ENTRIES = 4000

# اگر ربات به یک پیام چند بار پاسخ داد (مثلاً «سلام» و بعد ویرایش به «خوبی»)
# همهٔ پاسخ‌ها را نگه می‌داریم تا همه‌شان پاک شوند، ولی فقط چند تایِ آخر.
_MAX_REPLIES_PER_MESSAGE = 5

# key: (normalized_chat_id, user_message_id) -> ([bot_reply_id, ...], monotonic_ts)
_store = OrderedDict()


def _key(chat_id, user_message_id):
    """کلیدِ پایدارِ نگاشت؛ در صورتِ ورودیِ نامعتبر ``None`` برمی‌گرداند."""
    try:
        message_id = int(user_message_id)
    except (TypeError, ValueError):
        return None
    if message_id <= 0:
        return None
    return (_normalize_chat_id(chat_id), message_id)


def register(chat_id, user_message_id, bot_reply_id):
    """ثبتِ شناسهٔ پاسخِ ربات برای یک پیامِ کاربر (بهترین تلاش، بدونِ خطا)."""
    try:
        reply_id = int(bot_reply_id)
    except (TypeError, ValueError):
        return
    if reply_id <= 0:
        return
    key = _key(chat_id, user_message_id)
    if key is None:
        return

    entry = _store.get(key)
    ids = list(entry[0]) if entry is not None else []
    if reply_id not in ids:
        ids.append(reply_id)
        if len(ids) > _MAX_REPLIES_PER_MESSAGE:
            ids = ids[-_MAX_REPLIES_PER_MESSAGE:]
    _store[key] = (ids, time.monotonic())
    # تازه‌ترین ورودی همیشه آخرِ صف است تا _prune از سرِ (قدیمی‌ترین) بخواند.
    _store.move_to_end(key)
    _prune()


def pop_reply_ids(chat_id, user_message_id):
    """شناسه‌های پاسخِ ربات برای این پیام را برمی‌گرداند و از حافظه پاک می‌کند.

    پس از فراخوانی، ورودی حذف می‌شود تا دوباره پردازشِ همان پیام (مثلاً یک
    ویرایشِ دیگر) باعثِ حذفِ تکراری یا نگاه‌داشتنِ دادهٔ کهنه نشود. اگر ورودی
    از TTL گذشته باشد، فهرستِ خالی برمی‌گردد.
    """
    key = _key(chat_id, user_message_id)
    if key is None:
        return []
    entry = _store.pop(key, None)
    if entry is None:
        return []
    ids, ts = entry
    if (time.monotonic() - ts) > _TTL_SECONDS:
        return []
    return [reply_id for reply_id in ids
            if isinstance(reply_id, int) and reply_id > 0]


def peek_reply_ids(chat_id, user_message_id):
    """مثلِ :func:`pop_reply_ids` اما بدونِ حذفِ ورودی (برای تست/لاگ)."""
    key = _key(chat_id, user_message_id)
    if key is None:
        return []
    entry = _store.get(key)
    if entry is None:
        return []
    ids, ts = entry
    if (time.monotonic() - ts) > _TTL_SECONDS:
        return []
    return [reply_id for reply_id in ids
            if isinstance(reply_id, int) and reply_id > 0]


def _prune():
    now = time.monotonic()
    # ورودی‌های منقضی از سرِ صف (قدیمی‌ترین) حذف می‌شوند؛ چون ثبت همیشه با
    # زمانِ یکنواختِ روبه‌رشد انجام می‌شود، سرِ صف کمترین ts را دارد.
    while _store:
        _oldest_key, (_ids, ts) = next(iter(_store.items()))
        if (now - ts) > _TTL_SECONDS:
            _store.popitem(last=False)
        else:
            break
    # سقفِ سختِ تعداد: قدیمی‌ترین‌ها اول بیرون می‌روند.
    while len(_store) > _MAX_ENTRIES:
        _store.popitem(last=False)


def clear():
    """فراموش کردنِ همهٔ ورودی‌ها (برای تست یا راه‌اندازیِ مجددِ ربات)."""
    _store.clear()
