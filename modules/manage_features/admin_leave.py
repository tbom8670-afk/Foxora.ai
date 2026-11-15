# -*- coding: utf-8 -*-
"""🎒 موتورِ «مرخصی ادمین» — مستقل، گروه‌محور، ادمین‌محور و قابل بازیابی.

ذخیره‌سازی: ``admin_leave.json`` (همان موتورِ اتمیکِ ``storage.Storage``)::

    {"groups": {<group_key>: {
        "chat_id": …, "peer": {…},
        "leaves": {<admin_id>: {user_id, username, display, start_ts, end_ts,
                                 duration, reminder_ts, reminder_sent, token}},
        "snapshot": {"taken_at": …, "games_before": bool,
                     "features": {<name>: {"before": bool, "changed": bool,
                                           "manual": bool}}}}}}

قواعد:

* هر ادمین یک رکوردِ مستقل دارد؛ کلید = (گروه، شناسهٔ عددیِ ادمین).
* «حالت مدیریت پیشرفته» تا وقتی حداقل یک مرخصیِ معتبر در گروه هست روشن است.
* Snapshot فقط با **اولین** مرخصیِ گروه گرفته و فقط با **آخرین** پایان/لغو
  بازیابی می‌شود. فقط تنظیمی برمی‌گردد که خودِ همین سیستم عوض کرده و در این
  مدت کسی دستی به آن دست نزده باشد (``manual``).
* بازی‌ها «روکش» می‌شوند: فایلِ تنظیمِ سرگرمی دست نمی‌خورد، پس وضعیتِ قبلی
  خودبه‌خود و دقیق برمی‌گردد و هیچ امتیازی پاک نمی‌شود.
* زمان‌بندی بر پایهٔ timestampِ ذخیره‌شده است؛ ری‌استارت چیزی را گم نمی‌کند.
"""
from __future__ import annotations

import asyncio
import threading
import uuid

from modules.manage_features import storage
from modules.manage_features.queue import PerGroupQueue

REMINDER_AFTER_SECONDS = 17 * 60
MIN_SECONDS = 60
MAX_SECONDS = 24 * 3600
#: حداکثر خوابِ حلقهٔ زمان‌بند؛ ساعتِ دیوار هر بار دوباره خوانده می‌شود.
MAX_SLEEP_SECONDS = 20.0
RPC_TIMEOUT = 15.0

STORAGE = storage.Storage("admin_leave.json", {"groups": {}})
#: صفِ مستقلِ همین قابلیت (به صفِ بازی/حذف/ارسالِ گروه‌های دیگر وابسته نیست).
QUEUE = PerGroupQueue("admin_leave", task_timeout=60.0)

_LOCK = threading.RLock()
#: ایندکسِ حافظه‌ای برای مسیرِ گرم: group_key → بزرگ‌ترین end_ts
_ACTIVE: dict = {}
_INDEX_READY = False
_APPLYING = threading.local()
_WAKE = None          # asyncio.Event حلقهٔ زمان‌بند
_WATCHER = None


class InvalidDuration(ValueError):
    pass


# ---------------------------------------------------------------------------
#  قابلیت‌های حفاظتی
# ---------------------------------------------------------------------------
def _feature_modules():
    from modules import group_banned_words_control
    from modules.ad_spam_guard import storage as ad_spam_storage
    from modules.profile_guard import storage as profile_guard_storage
    return {
        "anti_channel": profile_guard_storage,
        "banned_words": group_banned_words_control,
        "heavy_spam": ad_spam_storage,
    }


FEATURES = ("anti_channel", "banned_words", "heavy_spam")


def _key(chat_id):
    return storage.group_key(chat_id)


def _user(user_id):
    return str(int(user_id))


# ---------------------------------------------------------------------------
#  پردازشِ مدت
# ---------------------------------------------------------------------------
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_WORD_NUMBERS = {"یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6,
                 "هفت": 7, "هشت": 8, "نه": 9, "ده": 10}
_HOUR_UNITS = ("ساعت", "ساعته")
_MINUTE_UNITS = ("دقیقه", "دقیقه ای", "دقیقهای", "دقه", "دیقه")


def normalize(text) -> str:
    value = str(text or "").translate(_DIGITS)
    value = value.replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    for ch in ("\u200f", "\u200e", "\ufeff", "\u0640"):
        value = value.replace(ch, "")
    return " ".join(value.split())


def parse_duration(text):
    """ثانیه، یا ``None`` برای ورودیِ نامعتبر.

    ``30 دقیقه`` · ``2 ساعت`` · ``1 ساعت و 30 دقیقه`` · ``نیم ساعت`` ·
    ``یک ساعت و نیم``. هر چیزِ اضافه/ناشناخته ⇒ نامعتبر (حدس زده نمی‌شود).
    """
    value = normalize(text)
    if not value:
        return None
    tokens = [token for token in value.split(" ") if token != "و"]
    hours = minutes = None
    half = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "نیم":
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt in _HOUR_UNITS and hours is None and minutes is None:
                minutes = 30
                index += 2
                continue
            if nxt is None and hours is not None and minutes is None:
                half = True
                index += 1
                continue
            return None
        number = None
        if token.isdigit() and len(token) <= 4:
            number = int(token)
        elif token in _WORD_NUMBERS:
            number = _WORD_NUMBERS[token]
        if number is None or index + 1 >= len(tokens):
            return None
        unit = tokens[index + 1]
        if unit in _HOUR_UNITS:
            if hours is not None or minutes is not None:
                return None
            hours = number
        elif unit in _MINUTE_UNITS:
            if minutes is not None:
                return None
            minutes = number
        else:
            return None
        index += 2
    if hours is None and minutes is None:
        return None
    total = (hours or 0) * 3600 + (minutes or 0) * 60 + (1800 if half else 0)
    if total < MIN_SECONDS or total > MAX_SECONDS:
        return None
    return total


def duration_label(seconds) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    parts = []
    if hours:
        parts.append(f"{hours} ساعت")
    if minutes:
        parts.append(f"{minutes} دقیقه")
    return " و ".join(parts) or "0 دقیقه"


# ---------------------------------------------------------------------------
#  ایندکسِ مسیرِ گرم
# ---------------------------------------------------------------------------
def _rebuild_index(data=None):
    global _INDEX_READY
    if data is None:
        data = STORAGE.load()
    fresh = {}
    for key, group in (data.get("groups") or {}).items():
        ends = [float(leave.get("end_ts") or 0)
                for leave in (group.get("leaves") or {}).values()]
        if ends:
            fresh[key] = max(ends)
    with _LOCK:
        _ACTIVE.clear()
        _ACTIVE.update(fresh)
        _INDEX_READY = True


def is_active(chat_id, now_ts=None) -> bool:
    """آیا گروه در «حالت مدیریت پیشرفته» است؟ O(1)، بدون I/O.

    حتی اگر زمان‌بند دیر برسد، بعد از ``end_ts`` دیگر فعال حساب نمی‌شود.
    """
    if not _INDEX_READY:
        try:
            _rebuild_index()
        except Exception:                            # noqa: BLE001
            return False
    if not _ACTIVE:
        return False
    try:
        end = _ACTIVE.get(_key(chat_id))
    except Exception:                                # noqa: BLE001
        return False
    if end is None:
        return False
    return end > (storage.now() if now_ts is None else now_ts)


def games_blocked(chat_id) -> bool:
    return is_active(chat_id)


# ---------------------------------------------------------------------------
#  خواندن
# ---------------------------------------------------------------------------
def group_record(chat_id):
    data = STORAGE.load()
    return (data.get("groups") or {}).get(_key(chat_id))


def get_leave(chat_id, user_id):
    group = group_record(chat_id) or {}
    try:
        return (group.get("leaves") or {}).get(_user(user_id))
    except (TypeError, ValueError):
        return None


def active_leaves(chat_id):
    group = group_record(chat_id) or {}
    return list((group.get("leaves") or {}).values())


def remaining_seconds(chat_id, user_id, now_ts=None):
    leave = get_leave(chat_id, user_id)
    if not leave:
        return 0
    now_ts = storage.now() if now_ts is None else now_ts
    return max(0, int(round(float(leave["end_ts"]) - now_ts)))


# ---------------------------------------------------------------------------
#  Snapshot / بازیابی
# ---------------------------------------------------------------------------
def _take_snapshot(chat_id, now_ts):
    """وضعیتِ قبلی را ثبت می‌کند و فقط خاموش‌ها را (بی‌صدا) روشن می‌کند."""
    features = {}
    modules = _feature_modules()
    _APPLYING.on = True
    try:
        for name in FEATURES:
            module = modules[name]
            try:
                before = bool(module.is_enabled(chat_id))
            except Exception:                        # noqa: BLE001
                continue                 # نامشخص ⇒ دست نمی‌زنیم
            changed = False
            if not before:
                try:
                    module.enable(chat_id)
                    changed = True
                except Exception:                    # noqa: BLE001
                    changed = False
            features[name] = {"before": before, "changed": changed,
                              "manual": False}
    finally:
        _APPLYING.on = False
    games_before = True
    try:
        from modules import entertainment_control
        games_before = bool(entertainment_control.is_enabled(chat_id))
    except Exception:                                # noqa: BLE001
        pass
    return {"taken_at": now_ts, "games_before": games_before,
            "features": features}


def _restore_snapshot(chat_id, snapshot):
    """فقط چیزی که خودمان روشن کردیم و کسی دستی عوضش نکرده خاموش می‌شود."""
    restored = []
    if not isinstance(snapshot, dict):
        return restored
    modules = _feature_modules()
    _APPLYING.on = True
    try:
        for name, entry in (snapshot.get("features") or {}).items():
            module = modules.get(name)
            if module is None or not isinstance(entry, dict):
                continue
            if not entry.get("changed") or entry.get("manual"):
                continue
            if entry.get("before"):
                continue
            try:
                if module.is_enabled(chat_id):
                    module.disable(chat_id)
                    restored.append(name)
            except Exception:                        # noqa: BLE001
                continue
    finally:
        _APPLYING.on = False
    return restored


def note_manual_change(feature, chat_id):
    """قلابِ «منشأ تغییر»: مالک/مدیر در مدتِ مرخصی تنظیمی را دستی عوض کرد.

    از داخلِ ``enable``/``disable``ِ خودِ هر قابلیت صدا زده می‌شود؛ تغییراتِ
    خودِ سیستمِ مرخصی (``_APPLYING``) نادیده گرفته می‌شوند. بعد از این،
    بازیابیِ خودکار به آن تنظیم دست نمی‌زند.
    """
    if getattr(_APPLYING, "on", False):
        return False
    if not _INDEX_READY or not _ACTIVE:
        return False
    try:
        key = _key(chat_id)
    except Exception:                                # noqa: BLE001
        return False
    if key not in _ACTIVE:
        return False

    def mutate(data):
        group = (data.get("groups") or {}).get(key)
        if not group:
            return False
        entry = ((group.get("snapshot") or {}).get("features") or {}).get(
            feature)
        if not isinstance(entry, dict) or entry.get("manual"):
            return False
        entry["manual"] = True
        return True

    try:
        return bool(STORAGE.update(mutate))
    except Exception:                                # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
#  ثبت / پایان
# ---------------------------------------------------------------------------
def start(chat_id, user_id, seconds, *, username="", display="", peer=None,
          now_ts=None):
    """ثبتِ اتمیکِ مرخصی. خروجی: ``(leave, first_in_group)``.

    ثبتِ دوبارهٔ همان ادمین، رکوردِ خودش را جایگزین می‌کند (هرگز دو رکورد).
    """
    seconds = int(seconds)
    if seconds < MIN_SECONDS or seconds > MAX_SECONDS:
        raise InvalidDuration(seconds)
    now_ts = storage.now() if now_ts is None else float(now_ts)
    key = _key(chat_id)
    uid = _user(user_id)
    leave = {
        "user_id": int(user_id),
        "username": str(username or "").lstrip("@"),
        "display": str(display or ""),
        "start_ts": now_ts,
        "end_ts": now_ts + seconds,
        "duration": seconds,
        "reminder_ts": (now_ts + REMINDER_AFTER_SECONDS
                        if seconds > REMINDER_AFTER_SECONDS else None),
        "reminder_sent": False,
        "token": uuid.uuid4().hex,
    }

    def mutate(data):
        groups = data.setdefault("groups", {})
        group = groups.get(key)
        first = not group or not group.get("leaves")
        if first:
            # اگر snapshotِ بازیابی‌نشده‌ای مانده (کرش وسطِ پایان)، همان معتبر است.
            snapshot = (group or {}).get("snapshot") or _take_snapshot(
                chat_id, now_ts)
            group = {"chat_id": chat_id, "leaves": {}, "snapshot": snapshot}
            groups[key] = group
        if peer is not None:
            group["peer"] = peer
        group["leaves"][uid] = leave
        return first

    with _LOCK:
        first = STORAGE.update(mutate)
        _rebuild_index()
    wake()
    return dict(leave), bool(first)


def end(chat_id, user_id, *, token=None):
    """پایان/لغوِ مرخصیِ همین ادمین. خروجی: ``(leave|None, last, restored)``.

    اگر مرخصیِ فعالِ دیگری نمانده باشد، snapshot بازیابی و رکوردِ گروه پاک
    می‌شود. ``token`` جلوی پایان‌دادنِ اشتباهِ یک مرخصیِ تازه‌تر را می‌گیرد.
    """
    key = _key(chat_id)
    try:
        uid = _user(user_id)
    except (TypeError, ValueError):
        return None, False, []
    box = {}

    def mutate(data):
        groups = data.setdefault("groups", {})
        group = groups.get(key)
        if not group:
            return None
        leaves = group.get("leaves") or {}
        leave = leaves.get(uid)
        if not leave or (token is not None and leave.get("token") != token):
            return None
        del leaves[uid]
        box["last"] = not leaves
        if not leaves:
            # بازیابی «قبل از» پاک‌شدنِ رکورد: اگر پروسه همین‌جا بمیرد، رکورد
            # هنوز روی دیسک است و بعد از ری‌استارت همین کار (بی‌خطر) تکرار
            # می‌شود؛ snapshot هیچ‌وقت گم نمی‌شود.
            box["restored"] = _restore_snapshot(
                group.get("chat_id", chat_id), group.get("snapshot"))
            del groups[key]
        return leave

    with _LOCK:
        leave = STORAGE.update(mutate)
        restored = box.get("restored") or []
        _rebuild_index()
    if leave:
        wake()
    return leave, bool(box.get("last")), restored


def claim_reminder(chat_id, user_id, token, now_ts=None):
    """یادآوری را «قبل از ارسال» مصرف‌شده علامت می‌زند (هرگز دو بار)."""
    now_ts = storage.now() if now_ts is None else float(now_ts)
    key = _key(chat_id)
    uid = _user(user_id)

    def mutate(data):
        leave = (((data.get("groups") or {}).get(key) or {}).get("leaves")
                 or {}).get(uid)
        if not leave or leave.get("token") != token:
            return None
        if leave.get("reminder_sent") or not leave.get("reminder_ts"):
            return None
        if now_ts < float(leave["reminder_ts"]) or \
                now_ts >= float(leave["end_ts"]):
            return None
        leave["reminder_sent"] = True
        return dict(leave)

    with _LOCK:
        return STORAGE.update(mutate)


# ---------------------------------------------------------------------------
#  زمان‌بند
# ---------------------------------------------------------------------------
def due_actions(now_ts=None):
    """``(reminders, endings, next_due_ts)`` از روی timestampهای ذخیره‌شده."""
    now_ts = storage.now() if now_ts is None else float(now_ts)
    reminders, endings = [], []
    next_due = None
    data = STORAGE.load()
    for key, group in (data.get("groups") or {}).items():
        chat_id = group.get("chat_id", key)
        for leave in (group.get("leaves") or {}).values():
            end_ts = float(leave.get("end_ts") or 0)
            if end_ts <= now_ts:
                endings.append((chat_id, dict(leave), group.get("peer")))
                continue
            candidate = end_ts
            reminder_ts = leave.get("reminder_ts")
            if reminder_ts and not leave.get("reminder_sent"):
                if float(reminder_ts) <= now_ts:
                    reminders.append((chat_id, dict(leave), group.get("peer")))
                else:
                    candidate = min(candidate, float(reminder_ts))
            if next_due is None or candidate < next_due:
                next_due = candidate
    return reminders, endings, next_due


def wake():
    event = _WAKE
    if event is not None:
        try:
            event.set()
        except Exception:                            # noqa: BLE001
            pass


def _log(logger, message, error=False):
    if logger is None:
        return
    try:
        (logger.log_error if error else logger.log_info)(message)
    except Exception:                                # noqa: BLE001
        pass


async def tick(on_reminder, on_end, logger=None, now_ts=None):
    """یک دور: یادآوری‌ها و پایان‌های سررسیده. خروجی: زمانِ سررسیدِ بعدی."""
    reminders, endings, next_due = due_actions(now_ts)
    for chat_id, leave, peer in endings:
        ended, last, restored = end(chat_id, leave["user_id"],
                                    token=leave.get("token"))
        if not ended:
            continue
        _log(logger, f"ADMIN LEAVE ENDED chat_id={chat_id} "
                     f"admin_id={leave['user_id']} last={last} "
                     f"restored={restored}")
        await _dispatch(chat_id, on_end, (chat_id, ended, peer, last), logger)
    for chat_id, leave, peer in reminders:
        claimed = claim_reminder(chat_id, leave["user_id"],
                                 leave.get("token"), now_ts)
        if not claimed:
            continue
        _log(logger, f"ADMIN LEAVE REMINDER chat_id={chat_id} "
                     f"admin_id={leave['user_id']}")
        await _dispatch(chat_id, on_reminder, (chat_id, claimed, peer), logger)
    return next_due


async def _dispatch(chat_id, callback, args, logger):
    if callback is None:
        return

    async def job():
        await callback(*args)

    try:
        await QUEUE.submit(_key(chat_id), job, logger=logger)
    except Exception as error:                       # noqa: BLE001
        _log(logger, f"ADMIN LEAVE DISPATCH FAILED chat_id={chat_id} "
                     f"error={error!r}", error=True)


async def watcher(on_reminder, on_end, logger=None):
    """حلقهٔ زمان‌بند — فقط می‌خوابد؛ هیچ کارِ سنگینی روی event loop ندارد."""
    global _WAKE
    _WAKE = asyncio.Event()
    try:
        from modules.outgoing_sender import detach_from_dispatch
        detach_from_dispatch()
    except Exception:                                # noqa: BLE001
        pass
    while True:
        # پیش از محاسبه پاک می‌شود تا ثبتِ هم‌زمانِ یک مرخصی هرگز گم نشود.
        _WAKE.clear()
        try:
            next_due = await tick(on_reminder, on_end, logger)
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            _log(logger, f"ADMIN LEAVE TICK FAILED error={error!r}",
                 error=True)
            next_due = None
        if next_due is None:
            delay = None if not _ACTIVE else MAX_SLEEP_SECONDS
        else:
            delay = min(MAX_SLEEP_SECONDS,
                        max(0.05, next_due - storage.now()))
        try:
            await asyncio.wait_for(_WAKE.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def ensure_watcher(on_reminder, on_end, logger=None):
    """حلقه را (فقط یک بار) بالا می‌آورد؛ اجرای چندباره تکراری نمی‌سازد."""
    global _WATCHER
    if _WATCHER is not None and not _WATCHER.done():
        wake()
        return _WATCHER
    _rebuild_index()
    _WATCHER = asyncio.ensure_future(watcher(on_reminder, on_end, logger))
    return _WATCHER


def reset_all():
    """فقط برای تست."""
    global _WATCHER, _WAKE, _INDEX_READY
    if _WATCHER is not None:
        try:
            _WATCHER.cancel()
        except Exception:                            # noqa: BLE001
            pass
    _WATCHER = None
    _WAKE = None
    STORAGE.reset()
    with _LOCK:
        _ACTIVE.clear()
        _INDEX_READY = False
