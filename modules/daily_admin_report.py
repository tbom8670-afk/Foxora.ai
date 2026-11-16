"""گزارش فعالیت کمترین ادمین در ساعت ۱۱ شب (۲۳:۰۰) به وقت تهران.

بررسی روزانهٔ فعالیت ادمین‌ها در هر گروه:
- ادمین‌های با ۰ پیام کلاً نادیده گرفته می‌شوند.
- ادمینی که از بین ادمین‌های فعال کمترین پیام را داده باشد انتخاب می‌شود.
- قالب پیام با نقل قول سروش پلاس ارسال می‌گردد.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file
from modules.time_utils import TEHRAN, now_local
from economy import activity, storage
from modules import group_expiry, group_storage

try:
    from splusthon.tl.types import MessageEntityBlockquote
except ImportError:
    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

FILE = runtime_config_file("daily_admin_report_state.json")

_cache = None
_cache_mtime = None


def _load_state():
    global _cache, _cache_mtime
    try:
        mtime = FILE.stat().st_mtime_ns
    except OSError:
        mtime = None
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    if mtime is None:
        _cache = {}
    else:
        try:
            _cache = json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    _cache_mtime = mtime
    return _cache


def _save_state(data):
    global _cache, _cache_mtime
    write_json(FILE, data, indent=2)
    _cache = data
    _cache_mtime = FILE.stat().st_mtime_ns


def is_report_done_for_group(group_id, date_str):
    state = _load_state()
    gid = str(normalize_group_id(group_id))
    return state.get(gid) == date_str


def mark_report_done_for_group(group_id, date_str):
    state = _load_state().copy()
    gid = str(normalize_group_id(group_id))
    state[gid] = date_str
    _save_state(state)


async def get_least_active_admin_for_group(bot, chat_id, now=None):
    """پیدا کردن ادمینی که در طول روز کمترین تعداد پیام (> 0) را داشته است.

    ادمین‌هایی که صفر پیام داده‌اند محاسبه نمی‌شوند.
    اگر هیچ ادمینی پیامی نداده باشد، None برمی‌گرداند.
    """
    today_key = activity._today(now)
    chat_key = str(chat_id)
    day_data = storage.read_path("daily_messages", today_key, chat_key, default={})

    # شناسایی شناسهٔ خود ربات جهت عدم قرارگیری در لیست ادمین‌ها
    me_id = None
    try:
        if hasattr(bot, "client") and hasattr(bot.client, "get_me"):
            me = await bot.client.get_me()
            me_id = getattr(me, "id", None)
    except Exception:
        pass

    # ۱. ادمین‌های ثبت‌شده ربات
    from modules.admin_storage import load_admins
    admins_map = load_admins()
    norm_gid = normalize_group_id(chat_id)
    registered_entries = admins_map.get(norm_gid, []) or admins_map.get(str(norm_gid), []) or []

    # ۲. مالک گروه
    from modules.group_storage import get_group_owner
    owner_id = get_group_owner(chat_id)

    # گردآوری تمام ادمین‌ها
    admins = {}  # user_id -> {"user_id": int/str, "username": str, "display_name": str}

    if owner_id:
        try:
            oid = int(owner_id)
            admins[oid] = {"user_id": oid, "username": None, "display_name": None}
        except (TypeError, ValueError):
            admins[str(owner_id)] = {"user_id": str(owner_id), "username": None, "display_name": None}

    for entry in registered_entries:
        if isinstance(entry, dict):
            uid = entry.get("user_id")
            uname = entry.get("username")
            dname = entry.get("display_name")
            if uid:
                try:
                    uid = int(uid)
                except (TypeError, ValueError):
                    pass
                admins[uid] = {
                    "user_id": uid,
                    "username": uname,
                    "display_name": dname,
                }
        else:
            try:
                uid = int(entry)
            except (TypeError, ValueError):
                uid = str(entry)
            admins[uid] = {"user_id": uid, "username": None, "display_name": None}

    # ۳. ادمین‌های بومی گروه (سروش پلاس / تلگرام)
    try:
        from splusthon.tl import types
        if hasattr(types, "ChannelParticipantsAdmins") and hasattr(bot, "client") and hasattr(bot.client, "iter_participants"):
            async for participant in bot.client.iter_participants(
                chat_id, filter=types.ChannelParticipantsAdmins()
            ):
                pid = getattr(participant, "id", None)
                if pid:
                    p_user = getattr(participant, "username", None)
                    first = getattr(participant, "first_name", "") or ""
                    last = getattr(participant, "last_name", "") or ""
                    p_name = f"{first} {last}".strip() or None
                    admins[pid] = {
                        "user_id": pid,
                        "username": p_user,
                        "display_name": p_name,
                    }
    except Exception as e:
        if hasattr(bot, "logger") and hasattr(bot.logger, "log_error"):
            bot.logger.log_error(f"DAILY ADMIN REPORT: Error getting native admins for {chat_id}: {e}")

    # حذف خود ربات
    if me_id:
        admins.pop(me_id, None)
        admins.pop(str(me_id), None)

    active_admins = []
    for uid, meta in admins.items():
        stats = day_data.get(str(uid), {})
        msg_count = int(stats.get("messages", 0))

        # ادمین‌هایی که صفر پیام داده‌اند نباید در نظر گرفته شوند
        if msg_count <= 0:
            continue

        dname = meta.get("display_name") or stats.get("name")
        uname = meta.get("username")

        # در صورت نبود اطلاعات، تلاش برای استخراج موجودیت از کلاینت
        if (not dname or not uname) and hasattr(bot, "client") and hasattr(bot.client, "get_entity"):
            try:
                entity = await bot.client.get_entity(uid)
                if entity:
                    if not uname and getattr(entity, "username", None):
                        uname = entity.username
                    if not dname:
                        first = getattr(entity, "first_name", "") or ""
                        last = getattr(entity, "last_name", "") or ""
                        dname = f"{first} {last}".strip() or None
            except Exception:
                pass

        active_admins.append({
            "user_id": uid,
            "username": uname,
            "display_name": dname,
            "count": msg_count,
        })

    if not active_admins:
        return None

    # انتخاب کمترین پیام در میان ادمین‌های فعال
    least_admin = min(active_admins, key=lambda a: a["count"])

    # آماده‌سازی نام نمایشی و نام کاربری
    dname = least_admin.get("display_name")
    uname = least_admin.get("username")
    uid = least_admin.get("user_id")

    if uname:
        clean_uname = uname.lstrip("@").strip()
        if dname:
            user_label = f"{dname} @{clean_uname}"
        else:
            user_label = f"@{clean_uname}"
    else:
        if dname:
            user_label = f"{dname} ({uid})"
        else:
            user_label = str(uid)

    persian_count = str(least_admin["count"]).translate(_PERSIAN_DIGITS)
    header = f"ادمین جان : ﴿ {user_label} ﴾"
    body = f"امروز فقط « {persian_count} » ارسال کردی دسخوشش فکر نمیکنی یکم کم حرف زدی ! سعی کن از فردا بیشتر فعال باشی تا یه گروه شلوغ داشته باشیم دور هم"

    header_len = len(header.encode("utf-16-le")) // 2
    entities = [MessageEntityBlockquote(offset=0, length=header_len)]
    full_text = f"{header}\n\n\n\n{body}"

    return {
        "admin": least_admin,
        "user_label": user_label,
        "count": least_admin["count"],
        "full_text": full_text,
        "entities": entities,
    }


async def send_daily_admin_report_for_group(bot, chat_id, now=None, force=False):
    """ارسال گزارش روزانه کم‌فعالیت‌ترین ادمین برای یک گروه مشخص."""
    today_key = activity._today(now)
    if not force and is_report_done_for_group(chat_id, today_key):
        return False

    report = await get_least_active_admin_for_group(bot, chat_id, now=now)
    if not report:
        if not force:
            # ثبت برای امروز تا در این ساعت دیگر بررسی مجدد تکرار نشود
            mark_report_done_for_group(chat_id, today_key)
        return False

    try:
        await bot.client.send_message(
            chat_id,
            report["full_text"],
            formatting_entities=report["entities"],
        )
        if hasattr(bot, "logger") and hasattr(bot.logger, "log_info"):
            bot.logger.log_info(
                f"DAILY ADMIN REPORT SENT chat_id={chat_id} "
                f"user={report['user_label']} count={report['count']}"
            )
        mark_report_done_for_group(chat_id, today_key)
        return True
    except Exception as error:
        if hasattr(bot, "logger") and hasattr(bot.logger, "log_error"):
            bot.logger.log_error(
                f"DAILY ADMIN REPORT FAILED chat_id={chat_id} error={error}"
            )
        return False


async def check_and_send_all_groups(bot, now=None, force=False):
    """بررسی و ارسال برای تمام گروه‌های فعال و منقضی‌نشده."""
    groups = group_storage.load_groups()
    for gid in list(groups.keys()):
        try:
            chat_id = int(gid)
        except (TypeError, ValueError):
            chat_id = gid

        if not group_storage.is_active(chat_id):
            continue
        if group_expiry.is_expired(chat_id):
            continue

        await send_daily_admin_report_for_group(bot, chat_id, now=now, force=force)


async def run_daily_admin_report_watcher(bot, interval=30, logger=None):
    """حلقه پس‌زمینه ناظر ساعت ۲۳:۰۰ به وقت تهران."""
    if logger:
        logger.log_info("DAILY ADMIN REPORT WATCHER STARTED")

    while True:
        try:
            now = now_local()
            # ساعت ۱۱ شب (۲۳:۰۰ به بعد)
            if now.hour == 23:
                # 🪶 کاهش‌بارِ خودکار: گزارشِ روزانه کم‌اولویت است؛ وقتی
                # اتصال کند یا صف‌ها سنگین شوند این راند عقب می‌افتد و در
                # تیکِ بعدی (۳۰ ثانیه دیگر، هنوز داخل همان ساعتِ ۲۳) دوباره
                # تلاش می‌شود — ارسالِ دستی با force از این مسیر نیست.
                from modules import load_shedding

                if not load_shedding.should_defer(bot, "daily_admin_report"):
                    await check_and_send_all_groups(bot, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if logger:
                logger.log_error(f"DAILY ADMIN REPORT WATCHER ERROR: {error}")
        await asyncio.sleep(interval)
