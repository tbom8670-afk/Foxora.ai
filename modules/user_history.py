"""🗂 سیستم سابقه‌ها — پروندهٔ تخلفِ کاربران به تفکیکِ گروه.

ماژولِ کاملاً مستقل. هیچ‌کدام از سیستم‌هایِ اخراج/سکوت/اخطار/فیلتر را
تغییر نمی‌دهد؛ فقط یک «ثبتِ اضافه» کنارِ آن‌ها انجام می‌شود و خواندنِ
گزارش هم از فایلِ جداگانهٔ خودش (``config/user_history.json``) است.

ساختارِ داده::

    {
      "<chat_id>": {
        "<user_id>": {
          "display": "@user",
          "records": [
            {"kind": "kick" | "mute" | "warn", "reason": "...", "_ts": 1.2}
          ]
        }
      }
    }

قانونِ ریست: هر رکورد دقیقاً ۲۴ ساعت پس از ثبت منقضی می‌شود و در اولین
خواندن/نوشتنِ بعدی از فایل پاک می‌گردد (ریستِ واقعی، نه فقط پنهان‌کردن).

قانونِ حریم خصوصی: هرگز شناسهٔ عددی نمایش داده نمی‌شود؛ نامِ نمایشی از
``modules.admin_tools.display_name`` گرفته می‌شود.
"""
import json
import time
from pathlib import Path

from modules.runtime_paths import CONFIG_DIR
from modules.atomic_write import write_json

from modules.admin_tools import display_name
from modules.group_id import normalize_group_id

_BASE = CONFIG_DIR
_FILE = _BASE / "user_history.json"

COMMAND = "سابقه ها"

# ریستِ واقعی هر ۲۴ ساعت.
TTL_SECONDS = 24 * 60 * 60
# سقفِ رکوردِ نگه‌داشته‌شده برای هر کاربر در هر گروه.
MAX_RECORDS_PER_USER = 50
# سقفِ کاربرانِ نمایش‌داده‌شده در گزارش.
MAX_USERS_IN_REPORT = 25

KICK = "kick"
MUTE = "mute"
WARN = "warn"

_LABELS = {
    KICK: "🚫 اخراج شده",
    MUTE: "🔇 سکوت شده",
    WARN: "⚠️ اخطار گرفته",
}
_ORDER = (KICK, MUTE, WARN)

NO_HISTORY = (
    "📭 در ۲۴ ساعت گذشته سابقه‌ای برای این گروه ثبت نشده است."
)
NO_PERMISSION = "⛔️ این دستور فقط برای مالک و ادمین‌های گروه است."


# ---------------------------------------------------------------------------
#  ذخیره‌سازی
# ---------------------------------------------------------------------------
def _chat_key(chat_id):
    """Return the canonical group key used by active SPlusthon storage."""
    return normalize_group_id(chat_id)


def _merge_legacy_chat_keys(data, chat_id):
    """Merge raw legacy ``-100…``/short-id variants into one group bucket.

    Older history versions used ``str(chat_id)`` directly.  SPlusthon may
    expose the same channel as either a short positive id or a ``-100…`` id,
    so direct string keys made a group's history appear missing.  This helper
    migrates only aliases of the requested group and preserves all records.
    """
    canonical = _chat_key(chat_id)
    aliases = [
        key for key in list(data)
        if _chat_key(key) == canonical and key != canonical
    ]
    if not aliases:
        return False

    target = data.setdefault(canonical, {})
    if not isinstance(target, dict):
        target = {}
        data[canonical] = target
    for alias in aliases:
        users = data.pop(alias, {})
        if not isinstance(users, dict):
            continue
        for user_key, entry in users.items():
            if not isinstance(entry, dict):
                continue
            existing = target.get(user_key)
            if not isinstance(existing, dict):
                target[user_key] = entry
                continue
            # Same user may exist under both old key forms.  Keep every valid
            # record, newest first on read, while retaining the normal cap.
            merged = list(existing.get("records") or []) + list(entry.get("records") or [])
            merged.sort(key=lambda record: record.get("_ts", 0)
                          if isinstance(record, dict) else 0)
            existing["records"] = merged[-MAX_RECORDS_PER_USER:]
            if not existing.get("display") and entry.get("display"):
                existing["display"] = entry["display"]
    return True


def _load():
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _save(data):
    try:
        _BASE.mkdir(parents=True, exist_ok=True)
        write_json(_FILE, data, indent=2)
    except OSError:
        pass


def _prune(data):
    """رکوردهایِ قدیمی‌تر از ۲۴ ساعت را واقعاً حذف می‌کند."""
    now = time.time()
    changed = False
    for chat_key in list(data.keys()):
        users = data.get(chat_key)
        if not isinstance(users, dict):
            data.pop(chat_key, None)
            changed = True
            continue
        for user_key in list(users.keys()):
            entry = users.get(user_key)
            records = entry.get("records") if isinstance(entry, dict) else None
            if not isinstance(records, list):
                users.pop(user_key, None)
                changed = True
                continue
            kept = []
            for record in records:
                if not isinstance(record, dict):
                    changed = True
                    continue
                ts = record.get("_ts", 0)
                if ts and (now - ts) < TTL_SECONDS:
                    kept.append(record)
                else:
                    changed = True
            if kept:
                entry["records"] = kept[-MAX_RECORDS_PER_USER:]
            else:
                users.pop(user_key, None)
                changed = True
        if not users:
            data.pop(chat_key, None)
            changed = True
    return changed


# ---------------------------------------------------------------------------
#  ثبت
# ---------------------------------------------------------------------------
def add_record(chat_id, user, kind, reason="", user_id=None):
    """یک تخلف را برای کاربر ثبت می‌کند.

    ``user`` شیءِ کاربر یا dict یا رشتهٔ آماده؛ ``kind`` یکی از
    ``kick``/``mute``/``warn``؛ ``reason`` دلیلِ تخلف.
    """
    if kind not in _LABELS:
        return None
    if user_id is None:
        if isinstance(user, dict):
            user_id = user.get("id", user.get("user_id"))
        elif user is not None:
            user_id = getattr(user, "id", getattr(user, "user_id", None))
    if user_id is None:
        return None

    if isinstance(user, str):
        display = user.strip() or "کاربر ناشناس"
    else:
        display = display_name(user)

    data = _load()
    _merge_legacy_chat_keys(data, chat_id)
    _prune(data)
    users = data.setdefault(_chat_key(chat_id), {})
    entry = users.setdefault(str(user_id), {"display": display, "records": []})
    if not isinstance(entry, dict):
        entry = {"display": display, "records": []}
        users[str(user_id)] = entry
    # نامِ نمایشی همیشه با جدیدترین مقدار به‌روز می‌شود.
    if display and display != "کاربر ناشناس":
        entry["display"] = display
    records = entry.setdefault("records", [])
    record = {
        "kind": kind,
        "reason": (reason or "").strip(),
        "_ts": time.time(),
    }
    records.append(record)
    if len(records) > MAX_RECORDS_PER_USER:
        del records[:-MAX_RECORDS_PER_USER]
    # 🏅 شمارندهٔ تجمعی تخلف برای «دستاورد کاربران»: جدا از این سابقهٔ
    # ۲۴ساعته زیاد می‌شود تا با ریست خودکار این فایل از بین نرود.
    try:
        from modules import user_achievements
        user_achievements.record_violation(chat_id, user_id)
    except Exception:
        pass
    # Save even when only the key form changed, so future reads are stable.
    _save(data)
    return record


def add_kick(chat_id, user, reason="", user_id=None):
    return add_record(chat_id, user, KICK, reason, user_id)


def add_mute(chat_id, user, reason="", user_id=None):
    return add_record(chat_id, user, MUTE, reason, user_id)


def add_warn(chat_id, user, reason="", user_id=None):
    return add_record(chat_id, user, WARN, reason, user_id)


# ---------------------------------------------------------------------------
#  خواندن
# ---------------------------------------------------------------------------
def get_history(chat_id, user_id=None):
    """سابقهٔ ۲۴ ساعتِ اخیر.

    بدونِ ``user_id`` فهرستی از ``(display, records)`` برایِ همهٔ کاربران
    برمی‌گرداند (تازه‌ترین تخلف اول)؛ با ``user_id`` فقط رکوردهایِ همان کاربر.
    """
    data = _load()
    migrated = _merge_legacy_chat_keys(data, chat_id)
    if _prune(data) or migrated:
        _save(data)
    users = data.get(_chat_key(chat_id), {})
    if user_id is not None:
        entry = users.get(str(user_id))
        if not isinstance(entry, dict):
            return []
        return list(entry.get("records", []))

    result = []
    for entry in users.values():
        if not isinstance(entry, dict):
            continue
        records = entry.get("records") or []
        if not records:
            continue
        result.append((entry.get("display") or "کاربر ناشناس", records))
    result.sort(key=lambda item: max(r.get("_ts", 0) for r in item[1]),
                reverse=True)
    return result


def reset(chat_id=None):
    """پاک‌سازیِ دستی (برای تست/ری‌استارت)."""
    if chat_id is None:
        _save({})
        return True
    data = _load()
    _merge_legacy_chat_keys(data, chat_id)
    if data.pop(_chat_key(chat_id), None) is None:
        return False
    _save(data)
    return True


def _u16_len(text):
    return len(text.encode("utf-16-le")) // 2


def format_history(chat_id, limit=MAX_USERS_IN_REPORT):
    """گزارشِ سابقه‌ها → ``(text, entities)``.

    قالب::

        「 @User 」
        🚫 اخراج شده
        📝 دلیل: ...
    """
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold

    users = get_history(chat_id)
    if not users:
        return NO_HISTORY, []

    lines = ["🗂 سابقهٔ کاربران این گروه:\n\n"]
    entities = []
    for display, records in users[:limit]:
        header = f"「 {display} 」\n"
        start = _u16_len("".join(lines))
        lines.append(header)
        entities.append(MessageEntityBold(
            offset=start, length=_u16_len(header.rstrip("\n"))))
        # تخلف‌ها به ترتیبِ اخراج → سکوت → اخطار، هرکدام با دلیل.
        buckets = {kind: [] for kind in _ORDER}
        for record in records:
            kind = record.get("kind")
            if kind in buckets:
                buckets[kind].append((record.get("reason") or "").strip())
        for kind in _ORDER:
            reasons = buckets[kind]
            if not reasons:
                continue
            count = len(reasons)
            label = _LABELS[kind]
            if count > 1:
                label = f"{label} ({_fa(count)} بار)"
            lines.append(f"{label}\n")
            for reason in reasons:
                lines.append(f"📝 دلیل: {reason or 'ثبت نشده'}\n")
        lines.append("\n")

    footer = "⏳ سابقه‌ها هر ۲۴ ساعت به‌صورت خودکار ریست می‌شوند."
    footer_start = _u16_len("".join(lines))
    lines.append(footer)
    full_text = "".join(lines)
    # کل گزارش داخل نقل قول شیشه‌ای (Blockquote) و با newline واقعی
    entities = [MessageEntityBlockquote(
        offset=0, length=_u16_len(full_text))]
    # Bold برای عنوان اصلی و پاورقی
    entities.append(MessageEntityBold(
        offset=0, length=_u16_len("🗂 سابقهٔ کاربران این گروه:")))
    footer_pos = full_text.find(footer)
    if footer_pos != -1:
        entities.append(MessageEntityBold(
            offset=_u16_len(full_text[:footer_pos]), length=_u16_len(footer)))
    return full_text, entities


_PERSIAN_DIGITS = "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵"


def _fa(value):
    return "".join(_PERSIAN_DIGITS[int(ch)] if ch.isdigit() else ch
                   for ch in str(value))
