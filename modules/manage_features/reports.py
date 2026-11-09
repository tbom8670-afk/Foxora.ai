# -*- coding: utf-8 -*-
"""⚠️ «گزارش» — پروندهٔ تخلف + تأیید/لغو مالک با دو اخطار.

جریان:

1. کاربر روی پیام متخلف ریپلای می‌کند و می‌نویسد «گزارش».
2. ربات دلیل را می‌پرسد (وضعیت «در انتظار دلیل» **در دیسک** ذخیره می‌شود، پس
   ری‌استارت آن را از بین نمی‌برد).
3. دلیل می‌آید ⇒ پرونده با شناسه‌های عددی ساخته می‌شود، مالک ثبت‌شده در گروه
   مطلع می‌شود و در وضعیت «در انتظار تأیید» می‌ماند.
4. مالک «تایید»/«لغو» می‌زند ⇒ در تأیید **دقیقاً دو اخطار** با همان سیستم
   اخطار پروژه (``bot.tracker.increment`` + ``user_history.add_warn`` +
   ``admin_tools.log_action``) ثبت می‌شود و پرونده مهر می‌خورد تا هرگز دوباره
   اخطار ندهد.

محدودیت ۷ ثانیه‌ای به‌ازای (گروه، کاربر) در دیسک نگه داشته می‌شود؛ تطبیق با
``REPORT_INTERVAL = 7.0`` و ساعت یکنواخت در حافظه برای مسیر گرم.
"""
from __future__ import annotations

import time
import uuid

from modules.manage_features import storage
from modules.manage_features.queue import PerGroupQueue

try:
    from modules import user_history
except ImportError:                                  # pragma: no cover
    user_history = None

try:
    from modules import admin_tools
except ImportError:                                  # pragma: no cover
    admin_tools = None

REPORT_INTERVAL = 7.0

#: صف مستقلِ گزارش‌ها؛ عملیات سنگینِ حذف هرگز اینجا اجرا نمی‌شود.
QUEUE = PerGroupQueue("user_report", max_pending_per_key=32,
                      max_pending_total=512, task_timeout=30.0)

#: کش محدودیت (فقط شتاب‌دهنده؛ منبع حقیقت دیسک است).
_RATE_CACHE: dict = {}
#: انتظار برای دلیل: (chat_key, user_key) → {"message_id", "target_id", "ts"}
_PENDING: dict = {}
PENDING_TTL = 300.0


class RateLimited(RuntimeError):
    """کمتر از ۷ ثانیه از گزارش قبلی همین کاربر در همین گروه گذشته."""


class ReportError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
#  محدودیت ۷ ثانیه (پایدار)
# ---------------------------------------------------------------------------
def _rate_key(chat_id, user_id):
    return f"{storage.group_key(chat_id)}:{storage.user_key(user_id)}"


def seconds_until_allowed(chat_id, user_id) -> float:
    key = _rate_key(chat_id, user_id)
    now = storage.monotonic()
    cached = _RATE_CACHE.get(key)
    if cached is not None:
        elapsed = now - cached
        if elapsed < REPORT_INTERVAL:
            return REPORT_INTERVAL - elapsed
        return 0.0
    # کش سرد (ری‌استارت): از دیسک بخوان تا محدودیت دور زده نشود.
    data = storage.REPORT_STORAGE.load()
    last = (data.get("limits") or {}).get(key)
    if not last:
        return 0.0
    elapsed = storage.now() - float(last)
    if elapsed < REPORT_INTERVAL:
        return REPORT_INTERVAL - elapsed
    return 0.0


def _stamp_rate(chat_id, user_id):
    key = _rate_key(chat_id, user_id)
    _RATE_CACHE[key] = storage.monotonic()

    def mutate(data):
        limits = data.setdefault("limits", {})
        limits[key] = storage.now()

    storage.REPORT_STORAGE.update(mutate)


def reset_rate_limits():
    _RATE_CACHE.clear()

    def mutate(data):
        data["limits"] = {}

    storage.REPORT_STORAGE.update(mutate)


# ---------------------------------------------------------------------------
#  انتظار برای دلیل
# ---------------------------------------------------------------------------
#: «در انتظار دلیل» در **دیسک** ذخیره می‌شود تا ری‌استارت آن را از بین نبرد.
_PENDING_FILE_KEY = "waiting_reason"
#: کشِ فقط-خواندنی برای مسیر گرم: مقدارش با هر تغییر از دیسک بازسازی می‌شود.
_PENDING_LOADED = {"ts": None, "items": {}}


def _pending_all():
    """نگاشتِ پایدارِ (گروه، کاربر) → وضعیت انتظار دلیل."""
    data = storage.REPORT_STORAGE.load()
    items = data.setdefault(_PENDING_FILE_KEY, {})
    return items if isinstance(items, dict) else {}


def remember_pending(chat_id, user_id, *, message_id=None, target_id=None,
                     target_name="", target_username=""):
    entry = {
        "message_id": message_id,
        "target_id": target_id,
        "target_name": target_name,
        "target_username": target_username,
        "ts": storage.now(),
        "created_at_tehran": storage.iso_tehran(),
    }
    key = f"{storage.group_key(chat_id)}:{storage.user_key(user_id)}"

    def mutate(data):
        items = data.setdefault(_PENDING_FILE_KEY, {})
        if not isinstance(items, dict):
            items = {}
            data[_PENDING_FILE_KEY] = items
        items[key] = entry
        # پاک‌سازی ورودی‌های منقضی (بدون محدودیت رشد)
        cutoff = storage.now() - PENDING_TTL
        for other, value in list(items.items()):
            if float((value or {}).get("ts", 0)) < cutoff:
                items.pop(other, None)
        return entry

    storage.REPORT_STORAGE.update(mutate)
    _PENDING_LOADED["ts"] = None
    _PENDING[(storage.group_key(chat_id), storage.user_key(user_id))] = entry
    return entry


def pending_reason(chat_id, user_id):
    key = (storage.group_key(chat_id), storage.user_key(user_id))
    entry = _pending_all().get(f"{key[0]}:{key[1]}")
    if not entry:
        _PENDING.pop(key, None)
        return None
    if storage.now() - float(entry.get("ts", 0)) > PENDING_TTL:
        forget_pending(chat_id, user_id)
        return None
    _PENDING[key] = dict(entry)
    return dict(entry)


def forget_pending(chat_id, user_id):
    key = f"{storage.group_key(chat_id)}:{storage.user_key(user_id)}"

    def mutate(data):
        items = data.get(_PENDING_FILE_KEY)
        if isinstance(items, dict):
            items.pop(key, None)
        return True

    try:
        storage.REPORT_STORAGE.update(mutate)
    except Exception:                                # noqa: BLE001
        pass
    _PENDING.pop((storage.group_key(chat_id), storage.user_key(user_id)), None)
    _PENDING_LOADED["ts"] = None


def pending_waiting_count():
    """تعداد وضعیت‌های «در انتظار دلیل» (برای بازیابی/لاگ)."""
    return len(_pending_all())


def clear_pending_memory():
    _PENDING.clear()
    _PENDING_LOADED["ts"] = None


# ---------------------------------------------------------------------------
#  ساخت پرونده
# ---------------------------------------------------------------------------
def _cases(chat_id, data):
    groups = data.setdefault("groups", {})
    record = groups.setdefault(storage.group_key(chat_id), {"cases": {}})
    record.setdefault("cases", {})
    return record


def create_case(chat_id, reporter_id, target_id, message_id, reason, *,
                reporter_name="", reporter_username="", target_name="",
                target_username="", owner_id=None):
    """پروندهٔ تازه؛ گزارش تکراریِ همان (گزارش‌دهنده، پیام) رد می‌شود."""
    reason = " ".join(str(reason or "").split())[:1000]
    if not reason:
        raise ReportError("empty reason")
    if str(reporter_id) == str(target_id):
        raise ReportError("self report")

    def mutate(data):
        record = _cases(chat_id, data)
        for case in record["cases"].values():
            if str(case.get("reporter_id")) == str(reporter_id) and \
                    str(case.get("message_id")) == str(message_id):
                raise ReportError("duplicate report")
        case_id = uuid.uuid4().hex[:12]
        case = {
            "case_id": case_id,
            "chat_id": storage.group_key(chat_id),
            "reporter_id": storage.user_key(reporter_id),
            "reporter_name": reporter_name or "",
            "reporter_username": (reporter_username or "").lstrip("@"),
            "target_id": storage.user_key(target_id),
            "target_name": target_name or "",
            "target_username": (target_username or "").lstrip("@"),
            "message_id": int(message_id) if message_id is not None else None,
            "reason": reason,
            "created_ts": storage.now(),
            "created_at_tehran": storage.iso_tehran(),
            "state": "pending",          # pending | approved | rejected
            "owner_id": storage.user_key(owner_id) if owner_id else None,
            "warning_count": 0,
            "decided_ts": None,
            "decided_at_tehran": "",
            "decided_by": None,
        }
        record["cases"][case_id] = case
        return dict(case)

    return storage.REPORT_STORAGE.update(mutate)


def get_case(chat_id, case_id):
    data = storage.REPORT_STORAGE.load()
    record = (data.get("groups") or {}).get(storage.group_key(chat_id)) or {}
    case = (record.get("cases") or {}).get(case_id)
    return dict(case) if case else None


def pending_cases(chat_id):
    data = storage.REPORT_STORAGE.load()
    record = (data.get("groups") or {}).get(storage.group_key(chat_id)) or {}
    return [dict(case) for case in (record.get("cases") or {}).values()
            if case.get("state") == "pending"]


def cases_of(chat_id):
    data = storage.REPORT_STORAGE.load()
    record = (data.get("groups") or {}).get(storage.group_key(chat_id)) or {}
    return [dict(case) for case in (record.get("cases") or {}).values()]


def _set_state(chat_id, case_id, state, **extra):
    def mutate(data):
        record = _cases(chat_id, data)
        case = record["cases"].get(case_id)
        if case is None:
            return None
        case["state"] = state
        case.update(extra)
        case["decided_ts"] = storage.now()
        case["decided_at_tehran"] = storage.iso_tehran()
        return dict(case)

    return storage.REPORT_STORAGE.update(mutate)


# ---------------------------------------------------------------------------
#  تأیید / لغو توسط مالک
# ---------------------------------------------------------------------------
class CaseNotFound(RuntimeError):
    pass


class CaseAlreadyDecided(RuntimeError):
    pass


class SelfDecision(RuntimeError):
    """گزارش‌دهنده نمی‌تواند پرونده را تأیید/لغو کند."""


class NotOwnerDecision(RuntimeError):
    """تصمیم‌گیری فقط در اختیار مالکِ ثبت‌شدهٔ گروه است."""


def approve(bot, chat_id, case_id, *, actor_id, actor=None, threshold=None,
            logger=None):
    """تأیید مالک: **دقیقاً دو اخطار** + سابقه + لاگ مدیریتی (idempotent)."""
    case = get_case(chat_id, case_id)
    if not case:
        raise CaseNotFound(case_id)
    if case.get("state") != "pending":
        raise CaseAlreadyDecided(case["state"])
    if str(actor_id) == str(case.get("reporter_id")):
        raise SelfDecision("reporter cannot decide its own case")
    # فقط مالکِ ثبت‌شدهٔ همین گروه تصمیم می‌گیرد (ادمین عادی و مالک سراسریِ
    # بی‌ربط نه) — همان کسی که کارت «مالک محترم» را دریافت کرده است.
    owner_id = case.get("owner_id")
    if owner_id is not None and str(actor_id) != str(owner_id):
        raise NotOwnerDecision("only the registered owner can decide")

    # مهر زدن قبل از اعمال: اگر دو کلیک هم‌زمان برسد، فقط یکی اخطار می‌دهد.
    def mutate(data):
        record = _cases(chat_id, data)
        target = record["cases"].get(case_id)
        if target is None:
            raise CaseNotFound(case_id)
        if target.get("state") != "pending":
            raise CaseAlreadyDecided(str(target.get("state")))
        target["state"] = "approving"
        return dict(target)

    storage.REPORT_STORAGE.update(mutate)

    count = 0
    target_user = case.get("target_id")
    try:
        for _ in range(2):
            count = bot.tracker.increment(chat_id, int(target_user))
    except Exception as error:                       # noqa: BLE001
        # اگر سیستم اخطار خطا داد، پرونده به pending برمی‌گردد تا گم نشود.
        _set_state(chat_id, case_id, "pending", last_error=repr(error))
        raise

    try:
        if threshold is None:
            threshold = bot.config_manager.get("spam_threshold", 5)
    except Exception:                                # noqa: BLE001
        threshold = 5

    if user_history is not None:
        try:
            for index in range(2):
                user_history.add_warn(
                    chat_id,
                    {"id": int(target_user),
                     "first_name": case.get("target_name") or "",
                     "username": case.get("target_username") or ""},
                    "گزارش تأییدشده توسط مالک گروه "
                    f"(اخطار شمارهٔ {max(0, count - 1 + index)})",
                )
        except Exception as error:                   # noqa: BLE001
            if logger is not None:
                try:
                    logger.log_error(
                        "REPORT HISTORY WARN FAILED "
                        f"chat_id={chat_id} case={case_id} error={error!r}")
                except Exception:                    # noqa: BLE001
                    pass

    if admin_tools is not None and actor is not None:
        try:
            admin_tools.log_action(
                chat_id, actor, "تأیید گزارش",
                target={"id": int(target_user),
                        "first_name": case.get("target_name") or "",
                        "username": case.get("target_username") or ""},
                note=f"پرونده {case_id} — دو اخطار (تعداد {count})")
        except Exception:                            # noqa: BLE001
            pass

    return _set_state(chat_id, case_id, "approved",
                      warning_count=2, decided_by=storage.user_key(actor_id),
                      warnings_total=count, threshold=threshold)


def reject(chat_id, case_id, *, actor_id, actor=None, logger=None):
    """لغو مالک: هیچ اخطاری ثبت نمی‌شود؛ پرونده در سوابق می‌ماند."""
    case = get_case(chat_id, case_id)
    if not case:
        raise CaseNotFound(case_id)
    if case.get("state") != "pending":
        raise CaseAlreadyDecided(case["state"])
    if str(actor_id) == str(case.get("reporter_id")):
        raise SelfDecision("reporter cannot decide its own case")
    owner_id = case.get("owner_id")
    if owner_id is not None and str(actor_id) != str(owner_id):
        raise NotOwnerDecision("only the registered owner can decide")

    if admin_tools is not None and actor is not None:
        try:
            admin_tools.log_action(
                chat_id, actor, "لغو گزارش",
                target={"id": int(case.get("target_id")) if
                        str(case.get("target_id") or "").isdigit() else None,
                        "first_name": case.get("target_name") or "",
                        "username": case.get("target_username") or ""},
                note=f"پرونده {case_id} لغو شد")
        except Exception:                            # noqa: BLE001
            pass
    return _set_state(chat_id, case_id, "rejected",
                      warning_count=0,
                      decided_by=storage.user_key(actor_id))


def reset_all():
    _PENDING.clear()
    _RATE_CACHE.clear()
