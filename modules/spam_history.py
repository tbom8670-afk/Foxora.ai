from collections import defaultdict, deque
import re
import time


MESSAGE_HISTORY = defaultdict(lambda: deque(maxlen=2000))
REPEAT_WINDOW_SECONDS = 30
# 🧹 نگه‌داری تاریخچه هر کاربر حداکثر ۲ دقیقه بعد از آخرین پیامش؛
# حلقهٔ پاکسازی core هر ۶۰ ثانیه آن را هرس می‌کند، پس RAM هر گروه
# عملاً هر ۱-۲ دقیقه خالی می‌شود. این پنجره برای همهٔ مصرف‌کننده‌ها
# کافی است: is_repeat فقط ۳۰ ثانیه نگاه می‌کند و بررسی/حذف موج اسپم
# در همان ثانیه‌های موج انجام می‌شود. «حذف کامل پیام‌های اسپمر بعد
# از بن» به این تاریخچه وابسته نیست (از message_tracker با نگه‌داری
# ۳۰ دقیقه استفاده می‌کند). بدون این هرس، در گروه‌های پرترافیک
# حافظه ساعت‌به‌ساعت رشد می‌کرد و ربات به‌تدریج کند می‌شد.
RETENTION_SECONDS = 2 * 60


def normalize(text):
    """فرم مقایسه: حذف ایموجی/علائم/فاصله تا نسخه‌های تزئین‌شدهٔ همان
    پیام («بیو چک🐥»، «بیوچک🌐»، «بیو  چک») یکی حساب شوند."""
    text = str(text or "").lower()
    text = re.sub(r'[^\wآ-ی]+', '', text)
    return text


def save_history_message(chat_id, user_id, message_id, text):
    key = (chat_id, user_id)
    if message_id is None:
        return False

    normalized_text = normalize(text)
    if any(item["message_id"] == message_id for item in MESSAGE_HISTORY[key]):
        return False

    MESSAGE_HISTORY[key].append({
        "message_id": message_id,
        "normalized_text": normalized_text,
        "timestamp": time.monotonic(),
    })
    return True


def get_user_history(chat_id, user_id):
    key = (chat_id, user_id)
    if key not in MESSAGE_HISTORY:
        return None
    return list(MESSAGE_HISTORY[key])


def is_repeat(chat_id, user_id, text, limit=3):
    current = normalize(text)
    if not current:
        return False

    # 🌡 «سپر محافظتی»: پنجرهٔ ضدتکرار کوتاه‌تر می‌شود تا موجِ تکراری زودتر
    # گرفته شود. در نبودِ سپر دقیقاً رفتارِ قبلی می‌ماند.
    window = REPEAT_WINDOW_SECONDS
    try:
        from modules import emergency_shield
        if emergency_shield.is_active(chat_id):
            window = emergency_shield.repeat_window(chat_id)
            limit = emergency_shield.repeat_limit(chat_id)
    except Exception:
        pass

    now = time.monotonic()
    recent_count = sum(
        1
        for item in MESSAGE_HISTORY.get((chat_id, user_id), [])
        if (
            item["normalized_text"] == current
            and now - item["timestamp"] <= window
        )
    )
    return recent_count >= limit


def get_message_ids(chat_id, user_id):
    return [
        item["message_id"]
        for item in MESSAGE_HISTORY.get((chat_id, user_id), [])
    ]


def clear_user(chat_id, user_id):
    MESSAGE_HISTORY.pop((chat_id, user_id), None)


def cleanup_expired(now=None, retention=RETENTION_SECONDS):
    """هرس دوره‌ای تاریخچه‌های راکد؛ داده‌های تازهٔ تشخیص دست نمی‌خورد.

    از حلقهٔ پاکسازی ۶۰ ثانیه‌ای core صدا زده می‌شود (مثل
    message_tracker.cleanup_expired). کاربری که در ۳۰ دقیقهٔ اخیر پیام
    داده، رکوردهای تازه‌اش می‌ماند؛ رکوردهای قدیمی‌ترش و کاربران راکد
    کامل آزاد می‌شوند تا RAM در گروه‌های پرترافیک رشد بی‌پایان نکند.
    """
    now = time.monotonic() if now is None else now
    removed_rows = 0
    for key in list(MESSAGE_HISTORY.keys()):
        rows = MESSAGE_HISTORY.get(key)
        if not rows:
            MESSAGE_HISTORY.pop(key, None)
            continue
        fresh = [
            item for item in rows
            if now - item.get("timestamp", now) <= retention
        ]
        removed_rows += len(rows) - len(fresh)
        if fresh:
            if len(fresh) != len(rows):
                MESSAGE_HISTORY[key] = deque(fresh, maxlen=2000)
        else:
            MESSAGE_HISTORY.pop(key, None)
    return removed_rows
