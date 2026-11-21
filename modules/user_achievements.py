# -*- coding: utf-8 -*-
"""🏅 دستاورد کاربران — کارت آماری هر کاربر در هر گروه.

دستورها (در ``handlers/message_handler.py`` سیم‌کشی شده‌اند)::

    دستاورد            ← کارت خودِ فرستنده
    دستاورد @username  ← کارت کاربر موردنظر در همان گروه

منبع هر ردیف کارت (هیچ مقداری حدس زده نمی‌شود):

===================  =====================================================
ردیف                 منبع واقعی
===================  =====================================================
تعداد پیام           ``modules.group_stats`` (شمارندهٔ ماندگار per-group)
نشان عضویت           قدیمی‌ترین ``first_seen`` شناخته‌شده در همین گروه
                     (``user_activity`` + رکورد همین ماژول)
استاد بازی           بردهای per-game که هنگام پرداخت جایزهٔ واقعی در
                     ``economy.coins.accounts.add(win=True)`` ثبت می‌شوند؛
                     برای سابقهٔ پیش از این قابلیت، از دفتر تراکنش‌های
                     همان گروه/کاربر (برچسب بازیِ تراکنش‌های جایزه) خوانده
                     می‌شود. اگر هیچ بازی‌ای به حد نصاب نرسید: ``ـــــ``
نشان فعالیت          آخرین فعالیت شناخته‌شده در همین گروه
تعداد تخلف           شمارندهٔ **تجمعی و ماندگار** همین ماژول؛ با هر ثبتِ
                     تخلف در ``modules.user_history.add_record`` (اخطار/
                     سکوت/اخراج) یکی زیاد می‌شود و با ریست ۲۴ساعتهٔ سابقهٔ
                     موقت یا صفرشدن چرخهٔ اخطار (``bot.tracker``) هرگز کم
                     یا پاک نمی‌شود.
روزهای فعالیت        مجموعهٔ روزهای متمایزی (به وقت تهران) که کاربر در
                     این گروه پیام داشته است؛ ماندگار ذخیره می‌شود و هر
                     روز فقط یک بار شمرده می‌شود. برای روزهای پیش از
                     نصب این قابلیت، فقط از دادهٔ واقعی موجود در
                     ``economy.activity`` (پیام‌های روزانه) بذرگیری
                     می‌شود — روزی ساخته یا حدس زده نمی‌شود.
===================  =====================================================

فرمول امتیاز (از ۱۰) — قابل‌تنظیم با متغیرهای محیطی ``BOT_ACHIEVEMENT_*``::

    امتیاز پیام   = min(6, تعداد پیام / 100)        # هر ۱۰۰ پیام = ۱ امتیاز
    امتیاز روزها  = min(4, روزهای فعالیت / 4)       # هر ۴ روز = ۱ امتیاز
    جریمهٔ تخلف  = 1 × تعداد تخلف
    امتیاز نهایی = clamp(round(پیام + روزها − جریمه), 0, 10)

نشان دستاورد فقط از روی همین امتیاز تعیین می‌شود:
``>= 8`` طلایی، ``>= 5`` نقره‌ای، وگرنه برنزی.

ذخیره‌سازی: هیچ دیتابیس موازی‌ای ساخته نمی‌شود. در حالت SQLite داده داخل
همان ``runtime_db`` فعلی (namespace ``user_achievements`` در ``runtime_kv``)
می‌نشیند و در حالت JSON داخل ``user_achievements.json`` کنار بقیهٔ فایل‌های
runtime — دقیقاً با همان الگوی کشِ درون‌حافظه + ``flush()`` دوره‌ای که
``modules.user_activity`` استفاده می‌کند. کلید همیشه
``(group_id, user_id)`` است؛ آمار گروه‌ها هرگز با هم ترکیب نمی‌شوند.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import runtime_db
from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        _TEHRAN = ZoneInfo("Asia/Tehran")
    except ZoneInfoNotFoundError:  # pragma: no cover
        _TEHRAN = timezone(timedelta(hours=3, minutes=30))
except ImportError:  # pragma: no cover
    _TEHRAN = timezone(timedelta(hours=3, minutes=30))

FILE = runtime_config_file("user_achievements.json")
_NAMESPACE = "user_achievements"
_USE_SQLITE = runtime_db.SQLITE_ENABLED

COMMAND = "دستاورد"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return int(default)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


#: چند روز سابقهٔ عضویت در گروه لازم است تا «عضو قدیمی» محسوب شود.
OLD_MEMBER_DAYS = max(1, _env_int("BOT_ACHIEVEMENT_OLD_MEMBER_DAYS", 30))
#: اگر آخرین فعالیت کاربر حداکثر این‌قدر روز پیش باشد «کاربر فعال» است.
ACTIVE_WITHIN_DAYS = max(1, _env_int("BOT_ACHIEVEMENT_ACTIVE_DAYS", 7))
#: حداقل برد در یک بازی تا آن بازی «استاد بازی» محسوب شود.
GAME_MASTER_MIN_WINS = max(1, _env_int("BOT_ACHIEVEMENT_GAME_MIN_WINS", 3))
#: سقف امتیاز بخش پیام و گام آن (هر چند پیام = یک امتیاز).
MESSAGE_POINTS_MAX = _env_float("BOT_ACHIEVEMENT_MESSAGE_POINTS_MAX", 6)
MESSAGE_POINTS_PER = max(1.0, _env_float("BOT_ACHIEVEMENT_MESSAGE_POINTS_PER", 100))
#: سقف امتیاز بخش روزهای فعالیت و گام آن (هر چند روز = یک امتیاز).
DAY_POINTS_MAX = _env_float("BOT_ACHIEVEMENT_DAY_POINTS_MAX", 4)
DAY_POINTS_PER = max(1.0, _env_float("BOT_ACHIEVEMENT_DAY_POINTS_PER", 4))
#: جریمهٔ هر تخلف از امتیاز.
VIOLATION_PENALTY = _env_float("BOT_ACHIEVEMENT_VIOLATION_PENALTY", 1)
#: آستانه‌های نشان دستاورد.
GOLD_MIN_SCORE = _env_int("BOT_ACHIEVEMENT_GOLD_MIN_SCORE", 8)
SILVER_MIN_SCORE = _env_int("BOT_ACHIEVEMENT_SILVER_MIN_SCORE", 5)

MEMBER_NEW = "عضو جدید"
MEMBER_OLD = "عضو قدیمی"
ACTIVE = "کاربر فعال"
INACTIVE = "کاربر غیرفعال"
TIER_BRONZE = "کاربر برنزی"
TIER_SILVER = "کاربر نقره‌ای"
TIER_GOLD = "کاربر طلایی"
NO_GAME = "ـــــ"

_MATH_DIGITS = str.maketrans("0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵")


def fa(value):
    """عدد با ارقام ریاضی (𝟬…𝟵)، دقیقاً مثل بقیهٔ کارت‌های ربات."""
    return str(value).translate(_MATH_DIGITS)


# ---------------------------------------------------------------------------
# ذخیره‌سازی (همان الگوی modules.user_activity: کش + dirty + flush دوره‌ای)
# ---------------------------------------------------------------------------
_LOCK = threading.RLock()
_CACHE = {}          # (group_id, user_id) -> record dict
_DIRTY = {}          # key -> mutation generation
_JSON_LOADED = False
_JSON_DATA = {}      # {group_id: {user_id: record}}


def _key(chat_id, user_id):
    return normalize_group_id(chat_id), str(user_id)


def _default_record():
    return {
        "days": [], "violations": 0, "games": {},
        "first": 0.0, "last": 0.0, "username": "", "name": "",
    }


def _normalize_record(raw):
    record = _default_record()
    if not isinstance(raw, dict):
        return record
    days = raw.get("days")
    if isinstance(days, (list, tuple, set)):
        record["days"] = sorted({str(day) for day in days if str(day or "")})
    try:
        record["violations"] = max(0, int(raw.get("violations", 0) or 0))
    except (TypeError, ValueError):
        pass
    games = raw.get("games")
    if isinstance(games, dict):
        for label, count in games.items():
            try:
                count = int(count or 0)
            except (TypeError, ValueError):
                continue
            if label and count > 0:
                record["games"][str(label)] = count
    for field in ("first", "last"):
        try:
            record[field] = max(0.0, float(raw.get(field, 0) or 0))
        except (TypeError, ValueError):
            pass
    record["username"] = str(raw.get("username") or "")
    record["name"] = str(raw.get("name") or "")
    return record


def _import_json_once():
    """مهاجرت یک‌بارهٔ فایل JSON قدیمی به SQLite (مثل user_activity)."""
    if not _USE_SQLITE:
        return
    marker = "user_achievements_json_import_v1"
    if runtime_db.meta_flag(marker):
        return
    if runtime_db.query_one(
        "SELECT COUNT(*) FROM runtime_kv WHERE namespace=?", (_NAMESPACE,)
    )[0]:
        runtime_db.meta_set(marker, "existing-db")
        return
    try:
        raw = json.loads(FILE.read_text(encoding="utf8")) if FILE.exists() else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    with runtime_db.transaction() as conn:
        for group_id, users in raw.items():
            if not isinstance(users, dict):
                continue
            gid = normalize_group_id(group_id)
            for user_id, info in users.items():
                record = _normalize_record(info)
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_kv("
                    "namespace,item_key,payload,updated_at) "
                    "VALUES(?,?,?,CURRENT_TIMESTAMP)",
                    (_NAMESPACE, f"{gid}:{user_id}",
                     json.dumps(record, ensure_ascii=False,
                                separators=(",", ":"))),
                )
        conn.execute(
            "INSERT OR REPLACE INTO storage_meta(key,value) VALUES(?,?)",
            (marker, "ok"),
        )


def _json_load():
    global _JSON_DATA, _JSON_LOADED
    if _JSON_LOADED:
        return _JSON_DATA
    if FILE.exists():
        try:
            data = json.loads(FILE.read_text(encoding="utf8"))
            _JSON_DATA = data if isinstance(data, dict) else {}
        except Exception:
            _JSON_DATA = {}
    else:
        _JSON_DATA = {}
    _JSON_LOADED = True
    return _JSON_DATA


def _entry(chat_id, user_id):
    """رکورد زندهٔ (قابل ویرایش) این کاربر در این گروه؛ در صورت نبود ساخته می‌شود."""
    key = _key(chat_id, user_id)
    if key in _CACHE:
        return _CACHE[key]
    if _USE_SQLITE:
        raw = runtime_db.kv_get(_NAMESPACE, f"{key[0]}:{key[1]}", None)
        record = _normalize_record(raw)
    else:
        data = _json_load()
        record = _normalize_record(data.get(key[0], {}).get(key[1]))
    _CACHE[key] = record
    return record


def _mark_dirty(chat_id, user_id):
    key = _key(chat_id, user_id)
    _DIRTY[key] = int(_DIRTY.get(key, 0)) + 1
    if not _USE_SQLITE:
        # در حالت JSON، کش و دادهٔ فایل یک مرجع مشترک دارند تا flush ارزان بماند.
        data = _json_load()
        data.setdefault(key[0], {})[key[1]] = _CACHE[key]


def flush():
    """نوشتن رکوردهای تغییرکرده روی ذخیرهٔ ماندگار. خروجی: آیا چیزی نوشته شد."""
    global _DIRTY
    with _LOCK:
        if not _DIRTY:
            return False
        generations = dict(_DIRTY)
        batch = {key: _CACHE[key] for key in generations if key in _CACHE}
    if _USE_SQLITE:
        with runtime_db.transaction() as conn:
            for (gid, uid), record in batch.items():
                conn.execute(
                    "INSERT INTO runtime_kv(namespace,item_key,payload,updated_at) "
                    "VALUES(?,?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(namespace,item_key) DO UPDATE SET "
                    "payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                    (_NAMESPACE, f"{gid}:{uid}",
                     json.dumps(record, ensure_ascii=False,
                                separators=(",", ":"))),
                )
    else:
        with _LOCK:
            payload = {
                str(group): {str(user): record for user, record in users.items()}
                for group, users in _json_load().items()
            }
        write_json(FILE, payload)
    with _LOCK:
        for key, generation in generations.items():
            if _DIRTY.get(key) == generation:
                _DIRTY.pop(key, None)
    return True


def _reset_state():
    """پاک‌کردن کش درون‌حافظه (فقط برای تست‌ها و ری‌استارت شبیه‌سازی‌شده)."""
    global _JSON_DATA, _JSON_LOADED
    with _LOCK:
        _CACHE.clear()
        _DIRTY.clear()
        _JSON_DATA = {}
        _JSON_LOADED = False


# ---------------------------------------------------------------------------
# ثبت رویدادها (مسیر داغ فقط حافظه را لمس می‌کند؛ نوشتن با flush دوره‌ای است)
# ---------------------------------------------------------------------------
def _today(now_ts=None):
    moment = (datetime.fromtimestamp(now_ts, tz=timezone.utc)
              if now_ts else datetime.now(timezone.utc))
    return moment.astimezone(_TEHRAN).date().isoformat()


def record_message(chat_id, user_id, username="", display_name="",
                   *, now=None, today=None):
    """ثبت یک پیام: روز فعالیت امروز (اگر تازه است)، first/last و نام کاربر."""
    if chat_id is None or user_id is None:
        return None
    ts = time.time() if now is None else float(now)
    day = str(today) if today else _today(ts)
    with _LOCK:
        record = _entry(chat_id, user_id)
        if day and day not in record["days"]:
            record["days"] = sorted(set(record["days"]) | {day})
        if not record["first"] or ts < record["first"]:
            record["first"] = ts
        if ts > record["last"]:
            record["last"] = ts
        if username:
            record["username"] = str(username).lstrip("@")
        if display_name:
            record["name"] = str(display_name)
        _mark_dirty(chat_id, user_id)
        return dict(record)


def record_violation(chat_id, user_id):
    """یک تخلف به شمارندهٔ **تجمعی و ماندگار** اضافه می‌کند و مجموع را برمی‌گرداند.

    این شمارنده عمداً هیچ مسیر کاهشی ندارد: ریست ۲۴ساعتهٔ
    ``user_history``، صفرشدن چرخهٔ اخطار در ``bot.tracker`` و «حذف اخطار»
    روی آن اثری ندارند.
    """
    if chat_id is None or user_id is None:
        return 0
    with _LOCK:
        record = _entry(chat_id, user_id)
        record["violations"] = int(record.get("violations", 0)) + 1
        _mark_dirty(chat_id, user_id)
        return record["violations"]


def _game_label(note):
    """برچسب بازی از روی یادداشت تراکنش جایزه؛ ``None`` یعنی «نامشخص، ثبت نکن»."""
    if not note:
        return None
    label = str(note).strip()
    for prefix in ("سایت بازی:", "سایت بازی："):
        if label.startswith(prefix):
            label = label[len(prefix):].strip()
            break
    if label == "جایزه مرکز فرماندهی روباه":
        label = "مرکز فرماندهی روباه"
    if not label or label.startswith("رتبه "):
        return None
    return label[:60]


def record_game_win(chat_id, user_id, note):
    """ثبت یک برد واقعی در بازیِ مشخص‌شده در ``note`` تراکنش جایزه.

    فقط از ``economy.coins.accounts.add(win=True)`` و فقط وقتی صدا زده
    می‌شود که جایزه واقعاً پرداخت شده باشد (مرجع تکراری زودتر برگشته
    است)؛ پس هر برد دقیقاً یک بار شمرده می‌شود. بدون نام بازی چیزی ثبت
    نمی‌شود — نام بازی هرگز حدس زده نمی‌شود.
    """
    label = _game_label(note)
    if label is None or chat_id is None or user_id is None:
        return 0
    with _LOCK:
        record = _entry(chat_id, user_id)
        record["games"][label] = int(record["games"].get(label, 0)) + 1
        _mark_dirty(chat_id, user_id)
        return record["games"][label]


def get_record(chat_id, user_id):
    """کپی فقط‌خواندنی از رکورد ماندگار این کاربر در این گروه."""
    if chat_id is None or user_id is None:
        return _default_record()
    with _LOCK:
        record = _entry(chat_id, user_id)
        return {
            "days": list(record["days"]),
            "violations": int(record["violations"]),
            "games": dict(record["games"]),
            "first": float(record["first"]),
            "last": float(record["last"]),
            "username": record["username"],
            "name": record["name"],
        }


# ---------------------------------------------------------------------------
# منطق خالص کارت (قابل تست، بدون I/O شبکه)
# ---------------------------------------------------------------------------
def compute_score(messages, active_days, violations):
    """امتیاز از ۱۰؛ فرمول کامل در docstring ماژول آمده است."""
    raw = (
        min(MESSAGE_POINTS_MAX, max(0, int(messages or 0)) / MESSAGE_POINTS_PER)
        + min(DAY_POINTS_MAX, max(0, int(active_days or 0)) / DAY_POINTS_PER)
        - VIOLATION_PENALTY * max(0, int(violations or 0))
    )
    return max(0, min(10, int(max(0.0, raw) + 0.5)))


def tier_for_score(score):
    """نشان دستاورد از روی امتیاز: طلایی / نقره‌ای / برنزی."""
    if score >= GOLD_MIN_SCORE:
        return TIER_GOLD
    if score >= SILVER_MIN_SCORE:
        return TIER_SILVER
    return TIER_BRONZE


def membership_badge(first_seen, *, now=None):
    """«عضو قدیمی» اگر سابقهٔ عضویت در گروه به حد نصاب رسیده باشد."""
    if not first_seen:
        return MEMBER_NEW
    ts = time.time() if now is None else float(now)
    tenure_days = max(0.0, (ts - float(first_seen)) / 86400)
    return MEMBER_OLD if tenure_days >= OLD_MEMBER_DAYS else MEMBER_NEW


def activity_badge(last_seen, *, now=None):
    """«کاربر فعال» اگر آخرین فعالیت در بازهٔ اخیر همین گروه بوده باشد."""
    if not last_seen:
        return INACTIVE
    ts = time.time() if now is None else float(now)
    if ts - float(last_seen) <= ACTIVE_WITHIN_DAYS * 86400:
        return ACTIVE
    return INACTIVE


def game_master(games):
    """نام بازیِ دارای بیشترین برد، اگر به حد نصاب رسیده باشد؛ وگرنه ``None``.

    تساوی به‌صورت قطعی با ترتیب الفباییِ برچسب شکسته می‌شود تا خروجی
    همیشه پایدار باشد.
    """
    if not games:
        return None
    label, wins = sorted(games.items(), key=lambda item: (-int(item[1]), item[0]))[0]
    return label if int(wins) >= GAME_MASTER_MIN_WINS else None


def parse_command(clean_text):
    """``دستاورد`` → ``("self", None)``؛ ``دستاورد @x`` → ``("user", "x")``.

    هر متن دیگری ``None`` برمی‌گرداند. یوزرنیم بدون ``@`` و lowercase
    برگردانده می‌شود.
    """
    text = " ".join(str(clean_text or "").split())
    if text == COMMAND:
        return ("self", None)
    if text.startswith(COMMAND + " @"):
        username = text[len(COMMAND) + 1:].strip().lstrip("@").strip().lower()
        if username and " " not in username:
            return ("user", username)
    return None


# ---------------------------------------------------------------------------
# جمع‌آوری دادهٔ واقعی کارت
# ---------------------------------------------------------------------------
def _seed_days_from_economy(chat_id, user_id):
    """روزهای فعالیتِ پیش از نصب این قابلیت، فقط از دادهٔ واقعی اقتصاد.

    ``economy.activity`` تعداد پیام روزانهٔ هر کاربر در هر گروه را نگه
    می‌دارد (پنجرهٔ ماندگار فعلی)؛ هر روزی که برای این کاربر در این
    گروه پیام ثبت شده باشد، یک روز فعالیت واقعی است.
    """
    try:
        from economy import storage as eco_storage
        from economy.coins import accounts as eco_accounts
        chat = eco_accounts.chat_key(chat_id)
        daily = eco_storage.read_path("daily_messages", default={}) or {}
        days = []
        for day, groups in daily.items():
            if not isinstance(groups, dict):
                continue
            users = groups.get(chat) or {}
            if isinstance(users, dict) and str(user_id) in users:
                days.append(str(day))
        return sorted(set(days))
    except Exception:
        return []


def _game_wins_from_ledger(chat_id, user_id):
    """بردهای per-game از دفتر تراکنش‌های جایزهٔ همین گروه/کاربر.

    فقط برای وقتی که هنوز برد per-game ثبت نشده است (سابقهٔ پیش از این
    قابلیت) استفاده می‌شود و فقط یادداشت‌هایی شمرده می‌شوند که دقیقاً
    برچسب یک بازی ثبت‌شده در ``economy.rewards`` یا برندهٔ «سایت بازی»
    باشند.
    """
    try:
        from economy import rewards as eco_rewards
        from economy.transactions import ledger
        known = {eco_rewards.label_for(game) for game in eco_rewards.games()}
        known.add("مرکز فرماندهی روباه")
        wins = {}
        for entry in ledger.history(chat_id, user_id, limit=0,
                                    kind=ledger.KIND_REWARD):
            label = _game_label(entry.get("note"))
            if label is not None and label in known:
                wins[label] = wins.get(label, 0) + 1
        return wins
    except Exception:
        return {}


def _group_user_stats(chat_id, user_id):
    try:
        from modules import group_stats
        group = group_stats.get_stats(chat_id)
        users = group.get("users", {}) if isinstance(group, dict) else {}
        info = users.get(str(user_id), {}) if isinstance(users, dict) else {}
        return info if isinstance(info, dict) else {}
    except Exception:
        return {}


def _activity(chat_id, user_id):
    try:
        from modules import user_activity
        return user_activity.get(chat_id, user_id) or {}
    except Exception:
        return {}


def resolve_target(chat_id, username):
    """یوزرنیم → (user_id یا None, برچسب نمایشی) فقط با دادهٔ همین گروه.

    اول آمار پیام همین گروه (``group_stats``) و بعد دفترچهٔ یوزرنیم
    اقتصادِ همین گروه (``economy.directory``) بررسی می‌شود. اگر کاربر
    پیدا نشود ``user_id`` مقدار ``None`` می‌گیرد تا کارت با مقادیر
    پیش‌فرض و همان ``@username`` خواسته‌شده نمایش داده شود.
    """
    name = str(username or "").strip().lstrip("@").strip().lower()
    if not name:
        return None, ""
    display = f"@{name}"
    try:
        from modules import group_stats
        group = group_stats.get_stats(chat_id)
        users = group.get("users", {}) if isinstance(group, dict) else {}
        for user_id, info in users.items():
            stored = str((info or {}).get("username") or "").lstrip("@").lower()
            if stored and stored == name:
                return str(user_id), f"@{(info or {}).get('username')}"
    except Exception:
        pass
    try:
        from economy import directory as username_directory
        found = username_directory.lookup(chat_id, name)
        if found is not None:
            return str(found), display
    except Exception:
        pass
    return None, display


def collect_card_data(chat_id, user_id, *, display=None, now=None):
    """همهٔ مقادیر کارت را از منابع واقعی همین گروه جمع می‌کند."""
    ts = time.time() if now is None else float(now)
    data = {
        "display": display or "",
        "member_badge": MEMBER_NEW,
        "messages": 0,
        "game_master": None,
        "activity_badge": INACTIVE,
        "violations": 0,
        "score": 0,
        "tier": TIER_BRONZE,
        "active_days": 0,
    }
    if user_id is None:
        data["score"] = compute_score(0, 0, 0)
        data["tier"] = tier_for_score(data["score"])
        return data

    user_stats = _group_user_stats(chat_id, user_id)
    try:
        data["messages"] = max(0, int(user_stats.get("messages", 0) or 0))
    except (TypeError, ValueError):
        data["messages"] = 0

    with _LOCK:
        record = _entry(chat_id, user_id)
        days = list(record["days"])
        violations = int(record["violations"])
        games = dict(record["games"])
        first = float(record["first"])
        last = float(record["last"])
        record_username = record["username"]
        record_name = record["name"]
        if not days:
            seeded = _seed_days_from_economy(chat_id, user_id)
            if seeded:
                record["days"] = sorted(set(days) | set(seeded))
                days = list(record["days"])
                _mark_dirty(chat_id, user_id)

    # user_activity برای کاربرِ هرگزدیده‌نشده در حالت SQLite مقدار «الان»
    # برمی‌گرداند؛ پس فقط وقتی به آن اعتماد می‌کنیم که کاربر در همین گروه
    # سابقهٔ شناخته‌شده‌ای (آمار پیام یا رکورد دستاورد) داشته باشد.
    activity_first = activity_last = 0.0
    if first or user_stats or data["messages"]:
        activity = _activity(chat_id, user_id)
        activity_first = float(activity.get("first", 0) or 0)
        activity_last = float(activity.get("last", 0) or 0)
    first_candidates = [value for value in (first, activity_first) if value]
    last_candidates = [value for value in (last, activity_last) if value]
    first_seen = min(first_candidates) if first_candidates else 0.0
    last_seen = max(last_candidates) if last_candidates else 0.0

    if not games:
        games = _game_wins_from_ledger(chat_id, user_id)

    if not data["display"]:
        username = user_stats.get("username") or record_username
        name = user_stats.get("name") or record_name
        if username and str(username) != "unknown":
            data["display"] = "@" + str(username).lstrip("@")
        elif name:
            data["display"] = str(name)
        else:
            data["display"] = "کاربر ناشناس"

    data["member_badge"] = membership_badge(first_seen, now=ts)
    data["game_master"] = game_master(games)
    data["activity_badge"] = activity_badge(last_seen, now=ts)
    data["violations"] = violations
    data["active_days"] = len(days)
    data["score"] = compute_score(data["messages"], data["active_days"],
                                  violations)
    data["tier"] = tier_for_score(data["score"])
    return data


def format_card(data):
    """متن کارت، دقیقاً با قالب مصوب — بدون هیچ ردیف یا متن اضافه."""
    game = data.get("game_master") or NO_GAME
    lines = [
        f"🪽کاربر : {data.get('display') or 'کاربر ناشناس'}  ⇾",
        f"◩ نـشان عضویت : {data.get('member_badge') or MEMBER_NEW}",
        f"◩ تـعداد پیام : {fa(data.get('messages', 0))}",
        f"◩ اسـتاد بازی : {game}",
        f"◩ نـشان فعالیت : {data.get('activity_badge') or INACTIVE}",
        f"◩ تـعداد تخلف : {fa(data.get('violations', 0))}",
        f"◩ امـتیاز دریافتی : {fa(10)} / {fa(data.get('score', 0))}",
        f"◩ نـشان دستاورد : {data.get('tier') or TIER_BRONZE}",
        f"◩ روز هـای فعالیت : {fa(data.get('active_days', 0))} روز",
    ]
    return "\n\n".join(lines)


def build_card(chat_id, user_id, *, display=None, now=None):
    """کارت دستاورد آمادهٔ ارسال برای یک کاربر در یک گروه."""
    return format_card(collect_card_data(
        chat_id, user_id, display=display, now=now))


_import_json_once()
