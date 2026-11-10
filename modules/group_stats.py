import json
import os

from modules.runtime_paths import runtime_log_file

from modules.group_id import normalize_group_id
from datetime import datetime

FILE = str(runtime_log_file("group_stats.json", migrate=True))
# 🛟 نسخهٔ پشتیبانِ «آخرین وضعیت سالم». اگر فایل اصلی خراب شود
# (kill وسط نوشتن، تداخل git stash/pop و...) از این بازیابی می‌شود
# تا شمارش پیام‌ها — و در نتیجه «سطح گروه» — هرگز صفر نشود.
BACKUP_FILE = FILE + ".bak"

_stats_cache = None
_stats_cache_mtime = None
_stats_dirty = False

def _file_mtime():
    try:
        return os.stat(FILE).st_mtime_ns
    except OSError:
        return None


def _read_json(path):
    """خواندن امن؛ None اگر فایل نبود یا JSON خراب بود."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def load_stats():
    global _stats_cache, _stats_cache_mtime, _stats_dirty
    mtime = _file_mtime()
    if _stats_cache is not None and mtime == _stats_cache_mtime:
        return _stats_cache

    data = _read_json(FILE)
    if data is None:
        # ⚠️ فایل اصلی گم/خراب است. قبلاً اینجا بی‌صدا {} برمی‌گشت و
        # flush بعدی همان {} را روی دیسک می‌نوشت — یعنی کل شمارش
        # پیام‌ها و سطح گروه‌ها یک‌شبه صفر می‌شد. حالا از آخرین نسخهٔ
        # سالم بازیابی می‌کنیم و dirty می‌شود تا فایل اصلی ترمیم شود.
        backup = _read_json(BACKUP_FILE)
        if backup is not None:
            _stats_cache = backup
            _stats_dirty = True
        else:
            _stats_cache = {}
    else:
        _stats_cache = data

    _stats_cache_mtime = mtime
    return _stats_cache


def save_stats(data):
    """به‌روزرسانی حافظه؛ flush دوره‌ای از نوشتن برای هر پیام جلوگیری می‌کند."""
    global _stats_cache, _stats_dirty
    _stats_cache = data
    _stats_dirty = True


def flush():
    global _stats_cache_mtime, _stats_dirty
    if not _stats_dirty:
        return False
    os.makedirs(os.path.dirname(FILE) or ".", exist_ok=True)
    payload = json.dumps(_stats_cache or {}, ensure_ascii=False,
                         separators=(",", ":"))
    # ✍️ نوشتن اتمیک (temp + replace): اگر پروسه وسط نوشتن kill شود
    # (pkill ری‌استارت روزانه، OOM، خاموشی)، فایل نیمه‌کاره و خراب
    # باقی نمی‌ماند — قبلاً همین باعث صفر شدن سطح گروه‌ها می‌شد.
    temp_path = FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(temp_path, FILE)
    # نسخهٔ پشتیبان همیشه از همین دادهٔ سالمِ در حافظه نوشته می‌شود
    # (نه با جابه‌جایی فایل قبلی که ممکن است خراب باشد).
    try:
        backup_temp = BACKUP_FILE + ".tmp"
        with open(backup_temp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(backup_temp, BACKUP_FILE)
    except OSError:
        pass
    _stats_cache_mtime = _file_mtime()
    _stats_dirty = False
    return True


def ensure_group(data, chat_id):
    chat_id = normalize_group_id(chat_id)

    if chat_id not in data:
        data[chat_id] = {
            "messages": 0,
            "deleted": 0,
            "kicked": 0,
            "muted": 0,
            "gifs": 0,
            "members": 0,
            "violator_deleted": 0,
            "users": {}
        }

    group = data[chat_id]
    # گروه‌هایی که پیش از این نسخه ساخته شده‌اند این کلیدها را ندارند.
    if not isinstance(group.get("gifs"), int):
        group["gifs"] = 0
    if not isinstance(group.get("members"), int):
        group["members"] = 0
    if not isinstance(group.get("users"), dict):
        group["users"] = {}
    # شمارندهٔ جدأ حذف‌های قابلیت «متخلف» (کلیدهای قدیمی را هم پوشش می‌دهد).
    if not isinstance(group.get("violator_deleted"), int):
        group["violator_deleted"] = 0

    return group


def ensure_user(group, user_id, username="", display_name=""):
    user_id = str(user_id)

    if user_id not in group["users"]:
        group["users"][user_id] = {
            "username": username or "unknown",
            "name": display_name or "",
            "messages": 0,
            "deleted": 0
        }

    else:
        record = group["users"][user_id]
        if username:
            record["username"] = username
        if display_name:
            record["name"] = display_name

    return group["users"][user_id]


def add_message(chat_id, user_id, username="", display_name=""):
    data = load_stats()

    group = ensure_group(data, chat_id)
    user = ensure_user(group, user_id, username, display_name)

    group["messages"] += 1
    user["messages"] += 1

    save_stats(data)


def add_gif(chat_id, user_id=None, username="", display_name=""):
    """شمارش «تعداد کل گیف های ارسالی» گروه — یک شمارندهٔ واقعی، نه تخمین.

    برای هر گیفِ دیده‌شده در گروه یک واحد اضافه می‌شود؛ همان مسیری که بقیهٔ
    آمار ثبت می‌شود (حافظه + فلاش دوره‌ای). اگر شناسهٔ کاربر معلوم باشد،
    شمارندهٔ همان کاربر هم بالا می‌رود.
    """
    data = load_stats()

    group = ensure_group(data, chat_id)
    group["gifs"] = int(group.get("gifs", 0)) + 1
    if user_id:
        user = ensure_user(group, user_id, username, display_name)
        user["gifs"] = int(user.get("gifs", 0)) + 1

    save_stats(data)


def record_members(chat_id, member_count):
    """ذخیرهٔ آخرین تعداد اعضای *واقعی* گروه (برای وقتی سرور پاسخ نمی‌دهد)."""
    try:
        member_count = int(member_count)
    except (TypeError, ValueError):
        return None
    if member_count <= 0:
        return None
    data = load_stats()
    group = ensure_group(data, chat_id)
    group["members"] = member_count
    save_stats(data)
    return member_count


def gif_count(chat_id):
    return int(get_stats(chat_id).get("gifs", 0) or 0)


def member_count(chat_id):
    return int(get_stats(chat_id).get("members", 0) or 0)


def add_deleted(chat_id, user_id, username=""):
    add_deleted_count(chat_id, user_id, username)


def add_deleted_count(chat_id, user_id, username="", count=1):
    data = load_stats()

    group = ensure_group(data, chat_id)
    user = ensure_user(group, user_id, username)
    count = max(0, int(count))

    group["deleted"] += count
    user["deleted"] += count

    save_stats(data)


def add_kick(chat_id):
    data = load_stats()

    group = ensure_group(data, chat_id)
    group["kicked"] += 1

    save_stats(data)


def add_mute(chat_id):
    data = load_stats()

    group = ensure_group(data, chat_id)
    group["muted"] += 1

    save_stats(data)


def add_violator_deleted(chat_id, user_id=None, username="", count=1):
    """شمارندهٔ **جدا** برای حذف‌های قابلیت «متخلف».

    این شمارنده عمداً از ``deleted`` (حذف دستی/خودکار) و از هر مسیرِ
    اسپم/اخطار جدا است تا حذف‌های خودکارِ متخلفان با آمار مدیریتی قاطی
    نشوند. مقدارش از طریق ``get_stats(chat_id)["violator_deleted"]``
    خواندنی است. (افزودنِ این کلید روی ساختار موجودِ آمار اثری ندارد؛
    ``make_report`` و ``group_stats_report`` فقط کلیدهای خودشان را
    می‌خوانند.)
    """
    count = max(0, int(count or 0))
    if count <= 0:
        return 0
    data = load_stats()

    group = ensure_group(data, chat_id)
    group["violator_deleted"] = int(group.get("violator_deleted", 0)) + count

    save_stats(data)
    return group["violator_deleted"]


def get_stats(chat_id):
    data = load_stats()
    return data.get(normalize_group_id(chat_id), {
        "messages": 0,
        "deleted": 0,
        "kicked": 0,
        "muted": 0,
        "members": 0,
        "violator_deleted": 0,
        "users": {}
    })


def top_users(chat_id, limit=10):
    """کاربران بر اساس تعداد پیام ارسالی، از بیشترین به کمترین.

    مرتب‌سازی کاملاً قطعی است: تعداد پیام نزولی و در صورت تساوی شناسهٔ کاربر
    (رشته‌ای) صعودی — پس یک ورودی همیشه یک خروجی می‌دهد و هیچ‌وقت دو اجرا
    ترتیب متفاوت نشان نمی‌دهند.
    """
    group = get_stats(chat_id)

    users = group.get("users", {})

    def rank_key(item):
        user_id, info = item
        count = info.get("messages", 0)
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            count = 0
        return (-count, str(user_id))

    result = sorted(users.items(), key=rank_key)

    return result[:limit]


def make_report(chat_id, member_count=0):
    group = get_stats(chat_id)

    text = (
        "📊 **آمار گروه**\n\n"
        "💬 **کل پیام‌ها:** "
        f"{group['messages']}\n\n"

        "🗑 **پیام حذف شده:** "
        f"{group['deleted']}\n\n"

        "🚪 **اخراج شده:** "
        f"{group['kicked']}\n\n"

        "🔇 **سکوت شده:** "
        f"{group['muted']}\n\n"

        "👥 **تعداد اعضا:** "
        f"{member_count}\n\n"

        "🏆 **کاربران فعال:**\n\n"
    )

    for i, (_, user) in enumerate(top_users(chat_id), 1):
        name = user.get("username", "unknown")
        count = user.get("messages", 0)
        text += f"{i}️⃣ @{name} - {count} پیام\n\n"

    return text

