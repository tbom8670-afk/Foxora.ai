# -*- coding: utf-8 -*-
"""⏳ «پیام زماندار» — قفل گروه در پایان بازه + پاکسازی دقیقاً همان بازه.

ساختار:

* یک رکورد پایدار به‌ازای هر گروه در ``timed_message.json`` شامل: زمان شروع/
  پایان (UTC epoch + رشتهٔ ISO تهران)، شناسهٔ فعال‌کننده، وضعیت، و فهرست
  شناسهٔ پیام‌های همان بازه.
* زمان‌سنج با ``asyncio`` به‌ازای هر گروه (نه یک حلقهٔ سراسری) و بر اساس
  **زمان پایانِ ذخیره‌شده** — پس ری‌استارت، زمان را از ابتدا شروع نمی‌کند.
* ثبت پیام‌ها فقط از مسیر واقعی پیام جدید (``track_message``) و فقط اگر
  رکورد در وضعیت ``active`` باشد و شناسهٔ پیام داخل بازه باشد.
* در پایان: (۱) وضعیت گروه از سرور، (۲) قفل با همان سیستم موجود پروژه،
  (۳) حذف دسته‌ای ۱۰۰تایی (سقف واقعی سرور) از طریق **صف اختصاصی** همین
  قابلیت. خطای یک دسته بقیه را متوقف نمی‌کند و برای تلاش دوباره ذخیره می‌شود.
* هیچ پیامی خارج از بازهٔ همین اجرا حذف نمی‌شود (فهرست شناسه‌ها و بازهٔ زمانی
  هر دو بررسی می‌شوند).
"""
from __future__ import annotations

import asyncio
import time

from modules.group_id import normalize_group_id
from modules.manage_features import storage
from modules.manage_features.queue import PerGroupQueue
from modules.manage_features.rpc import call, log_info

#: بازه‌های مجاز (دقیقه) — مطابق قالب کارفرما
#: برچسب‌ها عیناً مثل قالب تصویر کارفرما (۱ تا ۵).
DURATIONS = {30: "30 دقیقه", 120: "1 ساعته", 300: "5 ساعته",
             720: "12 ساعته", 1440: "24 ساعته"}

#: سقف شناسه‌های ذخیره‌شده به‌ازای هر اجرا (جلوگیری از رشد بی‌پایان حافظه).
MAX_TRACKED_IDS = 200_000
#: سقف واقعی سرور برای یک RPC حذف (بررسی‌شده روی splusthon 1.1.4)
DELETE_BATCH = 100
#: تحملِ فاصلهٔ زمانی: پیام‌هایی که چند ثانیه قبل/بعد از مرزِ بازه رسیده‌اند
#: (صف، تأخیر شبکه) نباید بیرون از بازه حساب شوند — و هرگز پیامِ قدیمی‌تر از
#: شروع بازه پاک نمی‌شود.
WINDOW_SLACK = 5.0
#: حداکثر دورهٔ تلاش دوباره برای دسته‌های ناموفق
MAX_RETRY_ROUNDS = 3

QUEUE = PerGroupQueue("timed_message", max_pending_per_key=64,
                      max_pending_total=1024, task_timeout=300.0)

_TIMERS: dict = {}

#: قلابِ نمایش پیشرفت (``timed_commands`` آن را ثبت می‌کند تا همان پیام
#: وضعیت ویرایش شود). تهی بودنش هیچ اثری روی موتور ندارد.
PROGRESS_HOOK = None
#: قلابِ ساختِ پیام وضعیت در لحظهٔ پایان (``timed_commands`` ثبتش می‌کند).
STATUS_HOOK = None
#: کمینهٔ فاصلهٔ بین دو ویرایش (ویرایش بیش از حد = FloodWait)
PROGRESS_MIN_INTERVAL = 2.0


def set_status_hook(hook):
    global STATUS_HOOK
    STATUS_HOOK = hook
    return hook


def set_progress_hook(hook):
    global PROGRESS_HOOK
    PROGRESS_HOOK = hook
    return hook


async def _emit(bot, chat_id, *, deleted, failed, elapsed_seconds, locked,
                state, logger=None):
    """اطلاع به لایهٔ نمایش؛ خطای آن هرگز موتور را متوقف نمی‌کند."""
    hook = PROGRESS_HOOK
    if hook is None:
        return
    try:
        await hook(bot, chat_id, deleted=int(deleted), failed=int(failed),
                   elapsed_seconds=int(elapsed_seconds), locked=bool(locked),
                   state=state, logger=logger)
    except Exception as error:                       # noqa: BLE001
        log_info(logger, f"TIMED PROGRESS HOOK FAILED chat_id={chat_id} "
                         f"error={error!r}")


def _key(chat_id):
    return storage.group_key(chat_id)


# ---------------------------------------------------------------------------
#  خواندن/نوشتن وضعیت
# ---------------------------------------------------------------------------
def get_record(chat_id):
    data = storage.TIMED_STORAGE.load()
    return dict((data.get("groups") or {}).get(_key(chat_id)) or {}) or None


def _write(chat_id, record):
    def mutate(data):
        groups = data.setdefault("groups", {})
        groups[_key(chat_id)] = record
        return record

    return storage.TIMED_STORAGE.update(mutate)


def active(chat_id) -> bool:
    record = get_record(chat_id)
    return bool(record) and record.get("state") in {"active", "locking",
                                                   "deleting"}


def remaining_seconds(chat_id):
    record = get_record(chat_id)
    if not record or record.get("state") not in {"active", "locking"}:
        return 0
    return max(0, int(round(float(record.get("end_ts", 0)) - storage.now())))


# ---------------------------------------------------------------------------
#  ثبت پیام‌های بازه
# ---------------------------------------------------------------------------
def track_message(chat_id, message_id, *, user_id=None, sent_ts=None):
    """ثبت شناسهٔ پیام اگر داخل بازهٔ فعال همین گروه باشد.

    خروجی ``True`` یعنی ثبت شد. این تابع عمداً ارزان است (بدون I/O در حالت
    غیرفعال) و در مسیر گرم پیام صدا زده می‌شود.
    """
    if message_id is None:
        return False
    record = get_record(chat_id)
    if not record or record.get("state") != "active":
        return False
    stamp = storage.now() if sent_ts is None else float(sent_ts)
    start = float(record.get("start_ts", 0))
    end = float(record.get("end_ts", 0))
    if not (start - WINDOW_SLACK <= stamp <= end + WINDOW_SLACK):
        return False

    def mutate(data):
        groups = data.setdefault("groups", {})
        target = groups.get(_key(chat_id))
        if not target or target.get("state") != "active":
            return False
        ids = target.setdefault("message_ids", [])
        if len(ids) >= MAX_TRACKED_IDS:
            return False
        ids.append(int(message_id))
        target["tracked"] = len(ids)
        if user_id is not None:
            senders = target.setdefault("senders", {})
            key = storage.user_key(user_id)
            senders[key] = int(senders.get(key, 0)) + 1
        return True

    return storage.TIMED_STORAGE.update(mutate)


def tracked_ids(chat_id, record=None):
    record = record or get_record(chat_id) or {}
    return [int(value) for value in record.get("message_ids", [])]


# ---------------------------------------------------------------------------
#  فعال‌سازی / لغو
# ---------------------------------------------------------------------------
class AlreadyActive(RuntimeError):
    pass


class NotActive(RuntimeError):
    pass


class InvalidDuration(RuntimeError):
    pass


def start(chat_id, minutes, *, actor_id=None, name="", username="",
          status_message_id=None):
    """شروع زمان‌سنج. فقط یک اجرای هم‌زمان برای هر گروه."""
    if int(minutes) not in DURATIONS:
        raise InvalidDuration(f"duration {minutes} is not allowed")
    if active(chat_id):
        raise AlreadyActive(f"timed messages already active in {chat_id}")
    now = storage.now()
    record = {
        "chat_id": _key(chat_id),
        "state": "active",
        "minutes": int(minutes),
        "start_ts": now,
        "end_ts": now + int(minutes) * 60,
        "started_at_tehran": storage.iso_tehran(now),
        "end_at_tehran": storage.iso_tehran(now + int(minutes) * 60),
        "actor_id": storage.user_key(actor_id) if actor_id else None,
        "actor_name": name or "",
        "actor_username": (username or "").lstrip("@"),
        "status_message_id": status_message_id,
        "message_ids": [],
        "tracked": 0,
        "senders": {},
        "deleted": 0,
        "failed": 0,
        "locked_at_tehran": "",
        "finished_at_tehran": "",
        "last_error": "",
        "retry_rounds": 0,
    }
    _write(chat_id, record)
    log_info(None, f"TIMED START chat_id={chat_id} minutes={minutes}")
    return record


def register_status(chat_id, message_id):
    """شناسهٔ پیامِ وضعیت را روی اجرای فعلی ثبت می‌کند.

    این تابع به‌جای ``start`` دوباره صدا زده می‌شود تا هرگز یک اجرای دوم
    (با زمان‌سنج تازه) ساخته نشود؛ همان رکورد فقط یک فیلد می‌گیرد.
    """
    if message_id is None:
        return None

    def mutate(data):
        record = (data.setdefault("groups", {}) or {}).get(_key(chat_id))
        if not isinstance(record, dict):
            return None
        record["status_message_id"] = int(message_id)
        record["updated_at"] = storage.now()
        return dict(record)

    return storage.TIMED_STORAGE.update(mutate)


def cancel(chat_id, *, reason="manual"):
    """توقف زمان‌سنج؛ حذف آغاز نمی‌شود و رکورد غیرفعال می‌شود."""
    record = get_record(chat_id)
    if not record or record.get("state") not in {"active", "locking",
                                                 "deleting"}:
        raise NotActive(f"no active timed run in {chat_id}")
    record = dict(record)
    record["state"] = "cancelled"
    record["cancelled_at_tehran"] = storage.iso_tehran()
    record["cancel_reason"] = reason
    _write(chat_id, record)
    _drop_timer(chat_id)
    log_info(None, f"TIMED CANCELLED chat_id={chat_id} reason={reason}")
    return record


# ---------------------------------------------------------------------------
#  زمان‌سنج‌ها
# ---------------------------------------------------------------------------
def _drop_timer(chat_id):
    task = _TIMERS.pop(_key(chat_id), None)
    if task is not None and not task.done():
        task.cancel()


def schedule(bot, chat_id, *, logger=None):
    """زمان‌سنجِ باقی‌مانده را برای همین گروه بالا می‌آورد (idempotent)."""
    record = get_record(chat_id)
    if not record or record.get("state") not in {
            "active", "locking", "deleting"}:
        return None
    key = _key(chat_id)
    existing = _TIMERS.get(key)
    if existing is not None and not existing.done():
        return existing

    delay = max(0.0, float(record.get("end_ts", 0)) - storage.now())
    # ⚠️ نباید از ``asyncio.create_task`` با ارجاعِ ضعیف استفاده کرد: حلقه
    # فقط ارجاعِ ضعیف نگه می‌دارد و ممکن است Task جمع‌آوری شود. ارجاع قوی
    # داخل ``_TIMERS`` نگه داشته می‌شود و در ``finally`` آزاد می‌شود.
    task = asyncio.ensure_future(_timer(bot, chat_id, max(1.0, delay),
                                        logger))
    try:
        task.set_name(f"timed:{key}")
    except Exception:                                # noqa: BLE001
        pass
    _TIMERS[key] = task
    task.add_done_callback(lambda _done, k=key: _TIMERS.pop(k, None))
    return task


async def _timer(bot, chat_id, delay, logger):
    try:
        if delay > 0:
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        raise
    try:
        await QUEUE.submit(key=_key(chat_id), job=lambda: finish(
            bot, chat_id, logger=logger), dedupe_key="finish", logger=logger)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    "TIMED FINISH SUBMIT FAILED "
                    f"chat_id={chat_id} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
    finally:
        _TIMERS.pop(_key(chat_id), None)


# ---------------------------------------------------------------------------
#  پایان بازه: قفل + حذف
# ---------------------------------------------------------------------------
async def lock_group(bot, chat_id, logger=None):
    """قفل گروه با همان سیستم موجود پروژه (بدون تغییر در آن ماژول)."""
    actions = getattr(bot, "group_actions", None)
    if actions is None:
        from modules.group_actions import GroupActions

        actions = GroupActions(bot.client, bot.logger)
    locked = await actions.lock_group(chat_id)
    log_info(logger, f"TIMED LOCK chat_id={chat_id} result={locked}")
    return locked


async def delete_batch(bot, chat_id, ids, logger=None):
    """حذف یک دسته (حداکثر ۱۰۰ شناسه) با تلاش مجدد؛ شمارشِ واقعی."""
    ids = [int(value) for value in ids if value]
    if not ids:
        return 0
    try:
        result = await call(
            bot.client.delete_messages(chat_id, ids),
            logger=logger,
            what=f"TIMED DELETE chat={chat_id} count={len(ids)}")
        # تلگرام/سروش برای هر RPC یک AffectedMessages برمی‌گرداند که تعدادِ
        # pts_count را می‌دهد؛ اگر نبود، فرض می‌کنیم همه حذف شدند (رفتار خودِ
        # کتابخانه هم همین است و خطا raise نمی‌شود).
        counted = getattr(result, "pts_count", None)
        if isinstance(counted, int) and 0 <= counted < len(ids):
            return counted
        return len(ids)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    "TIMED DELETE BATCH FAILED "
                    f"chat_id={chat_id} count={len(ids)} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return 0


async def finish(bot, chat_id, *, logger=None, now_ts=None):
    """پایان بازه: بررسی وضعیت، قفل، حذف بازه، ثبت نتیجه. همه‌چیز idempotent."""
    record = get_record(chat_id)
    if not record:
        return {"reason": "no_record"}
    if record.get("state") in {"cancelled", "finished"}:
        return {"reason": record.get("state")}

    started = time.monotonic()
    record = dict(record)
    now_ts = storage.now() if now_ts is None else float(now_ts)
    if now_ts + 1 < float(record.get("end_ts", 0)) and \
            record.get("state") == "active":
        # هنوز وقت نرسیده؛ دوباره زمان‌بندی می‌شود (مثلاً بعد از ری‌استارت).
        schedule(bot, chat_id, logger=logger)
        return {"reason": "not_due"}

    record["state"] = "locking"
    record["locking_at_tehran"] = storage.iso_tehran(now_ts)
    _write(chat_id, record)

    locked = False
    try:
        locked = await lock_group(bot, chat_id, logger)
        record["locked_at_tehran"] = storage.iso_tehran()
        record["lock_ok"] = bool(locked)
    except Exception as error:                       # noqa: BLE001
        record["lock_ok"] = False
        record["last_error"] = repr(error)
        if logger is not None:
            try:
                logger.log_error(
                    f"TIMED LOCK FAILED chat_id={chat_id} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass

    # پیامِ وضعیتِ زنده فقط از این‌جا (لحظهٔ پایان بازه) ساخته می‌شود.
    ensure_status = STATUS_HOOK
    if ensure_status is not None:
        try:
            await ensure_status(bot, chat_id, record=record, logger=logger)
        except Exception as error:                   # noqa: BLE001
            log_info(logger, f"TIMED STATUS HOOK FAILED chat_id={chat_id} "
                             f"error={error!r}")

    ids = tracked_ids(chat_id, record)
    record["state"] = "deleting"
    _write(chat_id, record)

    deleted = 0
    failed_ids = []
    last_emit = 0.0

    def _elapsed():
        return int(max(0.0, time.monotonic() - started))

    await _emit(bot, chat_id, deleted=0, failed=0,
                elapsed_seconds=_elapsed(), locked=locked, state="deleting",
                logger=logger)
    last_emit = time.monotonic()

    for start in range(0, len(ids), DELETE_BATCH):
        batch = ids[start:start + DELETE_BATCH]
        removed = await delete_batch(bot, chat_id, batch, logger)
        deleted += removed
        if removed < len(batch):
            failed_ids.extend(batch[removed:])
        # همان پیام وضعیت، به‌صورت زنده و با فاصلهٔ حداقل ویرایش می‌شود.
        if time.monotonic() - last_emit >= PROGRESS_MIN_INTERVAL:
            await _emit(bot, chat_id, deleted=deleted,
                        failed=len(failed_ids), elapsed_seconds=_elapsed(),
                        locked=locked, state="deleting", logger=logger)
            last_emit = time.monotonic()

    # تلاش دوبارهٔ کنترل‌شده برای دسته‌های ناموفق
    rounds = 0
    while failed_ids and rounds < MAX_RETRY_ROUNDS:
        rounds += 1
        retry = failed_ids[:]
        failed_ids = []
        for start in range(0, len(retry), DELETE_BATCH):
            batch = retry[start:start + DELETE_BATCH]
            removed = await delete_batch(bot, chat_id, batch, logger)
            deleted += removed
            if removed < len(batch):
                failed_ids.extend(batch[removed:])

    record["state"] = "finished"
    record["deleted"] = int(deleted)
    record["failed"] = len(failed_ids)
    record["failed_ids"] = failed_ids[:500]
    record["tracked"] = len(ids)
    record["finished_at_tehran"] = storage.iso_tehran()
    record["retry_rounds"] = rounds
    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    _write(chat_id, record)
    await _emit(bot, chat_id, deleted=deleted, failed=len(failed_ids),
                elapsed_seconds=_elapsed(), locked=locked, state="done",
                logger=logger)
    log_info(logger,
             "TIMED FINISHED "
             f"chat_id={chat_id} tracked={len(ids)} deleted={deleted} "
             f"failed={len(failed_ids)} locked={locked} "
             f"elapsed_ms={record['elapsed_ms']}")
    return {"reason": "finished", "tracked": len(ids), "deleted": deleted,
            "failed": len(failed_ids), "locked": locked,
            "elapsed_ms": record["elapsed_ms"]}


# ---------------------------------------------------------------------------
#  بازیابی پس از ری‌استارت / قطعی
# ---------------------------------------------------------------------------
def recover(bot, logger=None):
    """زمان‌سنج‌های همهٔ گروه‌ها را با زمان پایانِ ذخیره‌شده برمی‌گرداند.

    خروجی: فهرست ``(chat_id, remaining_seconds)``.
    """
    data = storage.TIMED_STORAGE.load()
    restored = []
    for key, record in (data.get("groups") or {}).items():
        if not isinstance(record, dict):
            continue
        if record.get("state") not in {"active", "locking", "deleting"}:
            continue
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            continue
        remaining = max(0, int(float(record.get("end_ts", 0)) - storage.now()))
        schedule(bot, chat_id, logger=logger)
        restored.append((chat_id, remaining))
    if restored:
        log_info(logger, f"TIMED RECOVERED count={len(restored)} "
                         f"runs={restored[:8]}")
    return restored


async def shutdown():
    for task in list(_TIMERS.values()):
        task.cancel()
    _TIMERS.clear()


def reset_all(chat_id=None):
    """پاک‌سازی وضعیت درون‌حافظه (تست)."""
    if chat_id is None:
        for task in list(_TIMERS.values()):
            task.cancel()
        _TIMERS.clear()
        return
    _drop_timer(chat_id)
