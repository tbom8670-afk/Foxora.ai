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
from modules.manage_features import peers, storage
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

#: سقفِ زمانیِ کلِ عملیاتِ پایان (قفل + حذف). بعد از این، وضعیت صریحاً
#: «failed_timeout» می‌شود و پیامِ وضعیت هم گزارشِ همان را نشان می‌دهد؛
#: عملیاتِ نیمه‌کاره بی‌صدا رها نمی‌شود.
FINISH_BUDGET_SECONDS = 30 * 60.0
#: سقفِ سختِ هر تماسِ شبکه‌ای در مسیر پایان (قفل/حذف/ویرایش).
RPC_TIMEOUT = 15.0
#: تیکِ زمان‌سنج. خوابِ یک‌جا (مثلاً ۲۴ ساعت) در برابر تغییرِ ساعتِ دیوار و
#: بیدارشدنِ دیرهنگامِ حلقه آسیب‌پذیر است؛ پس هر ۳۰ ثانیه بیدار می‌شویم و
#: زمان را از **ساعت واقعی/زمانِ پایانِ ذخیره‌شده** می‌سنجیم.
TICK_SECONDS = 30.0
#: اگر بیشتر از این مقدار از زمانِ پایان گذشته باشد، زمان‌سنج «تاخیر» خورده
#: و در پیام وضعیت گزارش می‌شود (کلید تشخیص باگ).
LATE_WARN_SECONDS = 60.0


def group_peer(bot, chat_id):
    """InputPeer گروه (پیاده‌سازی مشترک در ``modules.manage_features.peers``)."""
    return peers.group_peer(bot, chat_id)


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


def _serialize_peer(peer):
    """InputPeer را به dictِ قابل‌ذخیره در JSON تبدیل می‌کند."""
    if peer is None:
        return None
    channel_id = getattr(peer, "channel_id", None)
    access_hash = getattr(peer, "access_hash", None)
    if channel_id is None or access_hash is None:
        return None
    return {"channel_id": int(channel_id), "access_hash": int(access_hash),
            "type": type(peer).__name__}


def peer_from_record(record):
    """InputPeer ذخیره‌شده در رکورد (اگر بود)."""
    data = (record or {}).get("peer")
    if not isinstance(data, dict):
        return None
    try:
        from splusthon.tl import types

        return types.InputPeerChannel(int(data["channel_id"]),
                                      int(data["access_hash"]))
    except Exception:                                # noqa: BLE001
        return None


def resolve_peer(bot, chat_id, record=None):
    """peer گروه به «بهترین شکلِ ممکن»: رکورد ← کش ← شناسهٔ منفی."""
    peer = peer_from_record(record if record is not None
                            else get_record(chat_id))
    if peer is not None:
        return peer
    return group_peer(bot, chat_id)


def _write(chat_id, record):
    """نوشتن رکورد با ادغامِ امنِ شناسه‌های پیام.

    اگر رکورد ورودی از نسخهٔ قدیمی‌تر (stale) ساخته شده باشد، نباید
    شناسه‌های ثبت‌شده روی دیسک را پاک کند — وگرنه تعدادی از پیام‌های بازه
    بی‌سر و صدا از فهرست حذف بیرون می‌مانند (باگِ واقعی). ادغام فقط وقتی
    انجام می‌شود که «همان اجرا» باشد (``start_ts`` یکسان).
    """
    def mutate(data):
        groups = data.setdefault("groups", {})
        key = _key(chat_id)
        previous = groups.get(key)
        target = dict(record)
        if isinstance(previous, dict) and \
                float(previous.get("start_ts", -1)) == \
                float(target.get("start_ts", -2)):
            merged = list(dict.fromkeys(
                [int(value) for value in (previous.get("message_ids") or [])] +
                [int(value) for value in (target.get("message_ids") or [])]))
            target["message_ids"] = merged
            target["tracked"] = len(merged)
        groups[key] = target
        return target

    return storage.TIMED_STORAGE.update(mutate)


def active(chat_id) -> bool:
    record = get_record(chat_id)
    return bool(record) and record.get("state") in {"active", "locking",
                                                   "deleting"}


def remaining_seconds(chat_id):
    record = get_record(chat_id)
    if not record or record.get("state") not in {"active", "locking",
                                                 "deleting"}:
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


def record_message_ids(chat_id, record, message_ids):
    """شناسه‌های ضبط‌شده در حافظه را به رکوردِ پایدار اضافه می‌کند.

    چرا لازم است: ممکن است موتور زمان‌دار زنده شود ولی رکورد هنوز
    ``active`` باشد (مثلاً تسکی که تازه بالا آمده). اگر پیام‌ها فقط در حافظه
    ثبت شوند، پایانِ بازه آن‌ها را نمی‌بیند و حذف نمی‌شوند (باگِ واقعی).
    """
    if not message_ids:
        return int(record.get("tracked", 0) or 0)
    existing = {int(value) for value in (record.get("message_ids") or [])}
    fresh_ids = [int(value) for value in message_ids if int(value) not in existing]
    if not fresh_ids:
        return len(existing)
    record.setdefault("message_ids", [])
    record["message_ids"].extend(fresh_ids)
    record["tracked"] = len(record["message_ids"])
    record["updated_at"] = storage.now()
    _write(chat_id, record)
    return len(record["message_ids"])


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
          status_message_id=None, peer=None):
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
        # InputPeer گروه در همان لحظهٔ راه‌اندازی ذخیره می‌شود؛ بعد از
        # ری‌استارت (که کشِ حافظه خالی است) بدون resolve و بدون شکست
        # قابل استفاده است.
        "peer": _serialize_peer(peer),
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


def _same_loop(task) -> bool:
    """آیا این تسک در حلقهٔ asyncioِ همین حالا زندگی می‌کند؟"""
    try:
        return task.get_loop() is asyncio.get_running_loop()
    except RuntimeError:
        return False


def _timer_alive(chat_id) -> bool:
    task = _TIMERS.get(_key(chat_id))
    return task is not None and not task.done() and _same_loop(task)


def schedule(bot, chat_id, *, logger=None):
    """زمان‌سنجِ باقی‌مانده را برای همین گروه بالا می‌آورد (idempotent)."""
    record = get_record(chat_id)
    if not record or record.get("state") not in {
            "active", "locking", "deleting"}:
        return None
    key = _key(chat_id)
    existing = _TIMERS.get(key)
    if existing is not None and not existing.done() and _same_loop(existing):
        return existing
    if existing is not None:
        # تایمری که در حلقهٔ دیگری (مرده) ساخته شده هرگز اجرا نمی‌شود.
        existing.cancel()
        _TIMERS.pop(key, None)

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


async def _timer(bot, chat_id, _delay, logger):
    """تیکِ زمان‌سنج: هر ۳۰ ثانیه بیدار می‌شود و «زمان پایانِ ذخیره‌شده» را
    می‌سنجد.

    چرا تیک‌دار (و نه یک ``sleep`` یک‌جا):
    * ``asyncio.sleep(3600)`` با تغییرِ ساعتِ دیوار یا خوابِ دستگاه/حلقه
      دقیق نیست و می‌تواند ساعت‌ها دیر اجرا شود (باگِ گزارش‌شده: بازهٔ ۲→۳
      ساعت ۵ اجرا شد).
    * در هر تیک دوباره رکورد از دیسک خوانده می‌شود؛ پس «لغو»، «تمدید» و
      تغییرِ وضعیت بلافاصله دیده می‌شوند و هیچ اجرای مرده‌ای باقی نمی‌ماند.
    * تأخیرِ واقعی نسبت به ``end_ts`` در رکورد و لاگ ثبت می‌شود
      (``TIMED LATE``) تا اگر سرور/دستگاه خواب بوده، پنهان نماند.
    """
    key = _key(chat_id)
    try:
        while True:
            record = get_record(chat_id)
            if not record:
                return
            state = record.get("state")
            if state not in {"active", "locking", "deleting"}:
                return
            if state == "active":
                remaining = float(record.get("end_ts", 0)) - storage.now()
                if remaining > 0:
                    await asyncio.sleep(min(TICK_SECONDS, max(0.05, remaining)))
                    continue
                late = -remaining
                if late >= LATE_WARN_SECONDS:
                    log_info(logger,
                             f"TIMED LATE chat_id={chat_id} late_seconds="
                             f"{int(late)} end_at={record.get('end_at_tehran')}")
                    try:
                        current = get_record(chat_id) or record
                        current["late_seconds"] = int(late)
                        _write(chat_id, current)
                    except Exception:                # noqa: BLE001
                        pass
            # بازه تمام شده (یا اجرای نیمه‌کاره از قبل) ⇒ همان‌جا اجرا شود.
            await QUEUE.submit(key=key, job=lambda: finish(bot, chat_id,
                                                           logger=logger),
                               dedupe_key="finish", logger=logger)
            return
    except asyncio.CancelledError:
        raise
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    "TIMED FINISH SUBMIT FAILED "
                    f"chat_id={chat_id} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
    finally:
        _TIMERS.pop(key, None)


# ---------------------------------------------------------------------------
#  پایان بازه: قفل + حذف
# ---------------------------------------------------------------------------
async def lock_group(bot, chat_id, logger=None):
    """قفل گروه با همان سیستم موجود پروژه (بدون تغییر در آن ماژول).

    اگر InputPeer گروه در دسترس باشد به همان ماژول پاس داده می‌شود تا
    دوباره resolve نشود.
    """
    actions = getattr(bot, "group_actions", None)
    if actions is None:
        from modules.group_actions import GroupActions

        actions = GroupActions(bot.client, bot.logger)
    peer = resolve_peer(bot, chat_id)
    try:
        locked = await asyncio.wait_for(
            actions.lock_group(peer if peer is not None else chat_id),
            timeout=RPC_TIMEOUT)
    except asyncio.TimeoutError:
        log_info(logger, f"TIMED LOCK TIMEOUT chat_id={chat_id}")
        return False
    log_info(logger, f"TIMED LOCK chat_id={chat_id} result={locked}")
    return locked


async def delete_batch(bot, chat_id, ids, logger=None, peer=None):
    """حذف یک دسته (حداکثر ۱۰۰ شناسه) با تلاش مجدد؛ شمارشِ واقعی.

    تفاوت با نسخهٔ اول (که در عمل حذف نمی‌کرد):
    * ``entity`` همان InputPeer گروه است، نه شناسهٔ مثبت.
    * ``revoke=True`` صریح فرستاده می‌شود (حذف برای همه) — چون برای پیامِ
      دیگران مقدار پیش‌فرض کافی نیست. اگر کلاینت این کلیدواژه را نپذیرد
      یک بار بدون آن تلاش می‌شود.
    * خروجی کتابخانه برای هر دستهٔ ۱۰۰تایی یک ``AffectedMessages`` است؛
      اگر ``pts_count`` نبود، «تعداد درخواستی» شمرده می‌شود (رفتار خودِ
      کتابخانه: خطا raise نمی‌شود).
    """
    ids = [int(value) for value in ids if value]
    if not ids:
        return 0
    target = peer if peer is not None else group_peer(bot, chat_id)
    what = f"TIMED DELETE chat={chat_id} count={len(ids)}"
    try:
        try:
            rpc = bot.client.delete_messages(target, ids, revoke=True)
        except TypeError as type_error:              # کلاینتِ بدون revoke
            if "revoke" not in str(type_error).lower():
                raise
            rpc = bot.client.delete_messages(target, ids)
        result = await asyncio.wait_for(rpc, timeout=RPC_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception as error:                       # noqa: BLE001
        # یک تلاش دوبارهٔ کوتاه (خطای گذرا/شبکه) پیش از شمارشِ «ناموفق».
        if not _is_transient_delete_error(error):
            log_info(logger, f"{what} FAILED error={error!r}")
            return 0
        await asyncio.sleep(0.4)
        try:
            rpc = bot.client.delete_messages(target, ids, revoke=True)
            result = await asyncio.wait_for(rpc, timeout=RPC_TIMEOUT)
        except Exception as retry_error:             # noqa: BLE001
            if logger is not None:
                try:
                    logger.log_error(
                        "TIMED DELETE BATCH FAILED "
                        f"chat_id={chat_id} count={len(ids)} "
                        f"error={retry_error!r}")
                except Exception:                    # noqa: BLE001
                    pass
            return 0
    rows = result if isinstance(result, (list, tuple)) else [result]
    counted = 0
    for row in rows:
        value = getattr(row, "pts_count", None)
        counted += int(value) if isinstance(value, int) and value >= 0 else 0
    if counted <= 0:
        return len(ids)
    return min(counted, len(ids))


def _is_transient_delete_error(error) -> bool:
    """آیا خطای حذف موقتی است (شبکه/تایم‌اوت/سرور)؟"""
    from modules.manage_features.rpc import flood_seconds, is_transient

    if flood_seconds(error) is not None:
        return True
    return is_transient(error)


async def finish(bot, chat_id, *, logger=None, now_ts=None):
    """پایان بازه: بررسی وضعیت، قفل، حذف بازه، ثبت نتیجه. همه‌چیز idempotent.

    کلِ عملیات یک «بودجهٔ زمانی» دارد؛ اگر از آن گذشت (سرورِ گیرکرده)،
    وضعیت ``failed_timeout`` ثبت و همان پیامِ وضعیت با همین گزارش ویرایش
    می‌شود تا کارِ نیمه‌کاره پنهان نماند.
    """
    try:
        return await asyncio.wait_for(
            _finish(bot, chat_id, logger=logger, now_ts=now_ts),
            timeout=FINISH_BUDGET_SECONDS,
        )
    except asyncio.TimeoutError:
        record = get_record(chat_id) or {}
        record = dict(record)
        record["state"] = "failed_timeout"
        record["finished_at_tehran"] = storage.iso_tehran()
        record["last_error"] = f"timeout after {FINISH_BUDGET_SECONDS}s"
        record["timeout_retries"] = int(record.get("timeout_retries", 0) or 0) + 1
        _write(chat_id, record)
        if logger is not None:
            try:
                logger.log_error(
                    f"TIMED FINISH TIMEOUT chat_id={chat_id} "
                    f"budget={FINISH_BUDGET_SECONDS}s")
            except Exception:                        # noqa: BLE001
                pass
        await _emit(bot, chat_id, deleted=int(record.get("deleted", 0) or 0),
                    failed=int(record.get("failed", 0) or 0),
                    elapsed_seconds=int(FINISH_BUDGET_SECONDS),
                    locked=bool(record.get("lock_ok", False)),
                    state="failed_timeout", logger=logger)
        return {"reason": "timeout", "tracked": len(tracked_ids(chat_id,
                                                                record)),
                "deleted": int(record.get("deleted", 0) or 0),
                "failed": int(record.get("failed", 0) or 0)}


async def _finish(bot, chat_id, *, logger=None, now_ts=None):
    """بدنهٔ واقعی ``finish`` (قفل + حذف + ثبت نتیجه)."""
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

    # شناسهٔ پیام وضعیت: اگر در رکورد نیست از دیسک گرفته می‌شود.
    disk_record = get_record(chat_id) or {}
    if not record.get("status_message_id") and disk_record.get("status_message_id"):
        record["status_message_id"] = disk_record["status_message_id"]

    # پیامِ وضعیتِ زنده فقط از این‌جا (لحظهٔ پایان بازه) ساخته می‌شود.
    ensure_status = STATUS_HOOK
    if ensure_status is not None:
        try:
            await ensure_status(bot, chat_id, record=record, logger=logger)
        except Exception as error:                   # noqa: BLE001
            log_info(logger, f"TIMED STATUS HOOK FAILED chat_id={chat_id} "
                             f"error={error!r}")
    if not record.get("status_message_id"):
        log_info(logger, f"TIMED STATUS MESSAGE MISSING chat_id={chat_id} "
                         f"(counts will still be recorded)")

    # ادغامِ شناسه‌ها: رکوردِ روی دیسک مرجع است (ممکن است بین راه‌اندازی و
    # پایان، پیام‌های تازه‌ای ثبت شده باشد) و شناسه‌های نسخهٔ درون‌حافظه هم
    # اضافه می‌شوند — هیچ پیامی از بازه جا نمی‌ماند.
    on_disk = get_record(chat_id) or {}
    merged = list(dict.fromkeys(
        [int(value) for value in (on_disk.get("message_ids") or [])] +
        [int(value) for value in (record.get("message_ids") or [])] +
        [int(value) for value in (record.get("pending_ids") or [])]))
    if merged:
        record["message_ids"] = merged
        record["tracked"] = len(merged)
    ids = merged
    record["state"] = "deleting"
    _write(chat_id, record)
    # InputPeer یک بار resolve می‌شود و به همهٔ دسته‌ها داده می‌شود
    # (بدون RPC تکراری برای هر دسته). اولویت: رکورد (ذخیره‌شده در لحظهٔ
    # راه‌اندازی) ← کش ربات ← شناسهٔ منفی.
    peer = resolve_peer(bot, chat_id, record)

    deleted = 0
    failed_ids = []
    last_emit = 0.0

    def _elapsed():
        return int(max(0.0, time.monotonic() - started))

    await _emit(bot, chat_id, deleted=0, failed=0,
                elapsed_seconds=_elapsed(), locked=locked, state="deleting",
                logger=logger)
    last_emit = time.monotonic()

    aborted = False
    for start in range(0, len(ids), DELETE_BATCH):
        # «لغو پیام زماندار» در میانهٔ حذف: دسته‌های بعدی شروع نمی‌شوند
        # (کارِ در جریان با امنیت تمام می‌شود و همان گروه‌ها ثبت می‌شوند).
        current_state = (get_record(chat_id) or {}).get("state")
        if current_state == "cancelled":
            aborted = True
            record["aborted_by_cancel"] = True
            log_info(logger, f"TIMED FINISH ABORTED BY CANCEL chat_id={chat_id} "
                             f"deleted={deleted}")
            break
        batch = ids[start:start + DELETE_BATCH]
        removed = await delete_batch(bot, chat_id, batch, logger, peer=peer)
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
            removed = await delete_batch(bot, chat_id, batch, logger,
                                         peer=peer)
            deleted += removed
            if removed < len(batch):
                failed_ids.extend(batch[removed:])

    current_state = (get_record(chat_id) or {}).get("state")
    kept_cancelled = current_state == "cancelled"
    record["state"] = "cancelled" if kept_cancelled else "finished"
    record["deleted"] = int(deleted)
    record["failed"] = len(failed_ids)
    record["failed_ids"] = failed_ids[:500]
    record["tracked"] = len(ids)
    record["finished_at_tehran"] = storage.iso_tehran()
    record["retry_rounds"] = rounds
    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    record["end_ts"] = float(record.get("end_ts", 0) or 0)
    record["finished_ts"] = storage.now()
    _write(chat_id, record)
    await _emit(bot, chat_id, deleted=deleted, failed=len(failed_ids),
                elapsed_seconds=_elapsed(), locked=locked,
                state="cancelled" if kept_cancelled else "done", logger=logger)
    if kept_cancelled:
        log_info(logger, "TIMED FINISH STOPPED BY CANCEL "
                         f"chat_id={chat_id} deleted={deleted} "
                         f"failed={len(failed_ids)}")
        return {"reason": "cancelled", "tracked": len(ids),
                "deleted": deleted, "failed": len(failed_ids),
                "locked": locked, "aborted": aborted}
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
    # نگهبان همیشه بیدار می‌ماند تا اگر تایمری (به هر دلیلی) مرد یا حلقهٔ
    # asyncio عوض شد، بازه از دست نرود.
    try:
        ensure_recovery_loop(bot, logger)
    except RuntimeError:
        pass
    return restored


# ---------------------------------------------------------------------------
#  نگهبانِ بازیابی (ضدِ گم‌شدنِ بازه بعد از ری‌استارت/قطعی)
# ---------------------------------------------------------------------------
#: هر چند ثانیه وضعیتِ ذخیره‌شده بررسی می‌شود. اگر تایمر در حافظه نبود (مثلاً
#: حلقهٔ asyncio عوض شده یا تسک مرده) دوباره ساخته می‌شود؛ اگر پایانِ بازه
#: گذشته و کاری شروع نشده، همان‌جا اجرا می‌شود. هیچ RPC‌ای در حالت عادی زده
#: نمی‌شود (فقط خواندنِ حافظه/فایل با کش mtime).
RECOVERY_INTERVAL = 60.0
_RECOVERY = {"task": None}


async def _recovery_loop(bot, logger=None):
    while True:
        try:
            await asyncio.sleep(RECOVERY_INTERVAL)
            await recover_once(bot, logger)
        except asyncio.CancelledError:
            raise
        except Exception as error:                   # noqa: BLE001
            log_info(logger, f"TIMED RECOVERY LOOP ERROR error={error!r}")


async def recover_once(bot, logger=None):
    """یک دورِ بازبینی: هر اجرای ذخیره‌شده باید تایمرِ زنده داشته باشد."""
    data = storage.TIMED_STORAGE.load()
    checked = 0
    resumed = 0
    for key, record in (data.get("groups") or {}).items():
        if not isinstance(record, dict):
            continue
        state = record.get("state")
        retryable = state in {"active", "locking", "deleting"} or (
            state == "failed_timeout"
            and int(record.get("timeout_retries", 0) or 0) < 3)
        if not retryable:
            continue
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            continue
        checked += 1
        if _timer_alive(chat_id):
            continue
        if record.get("state") == "active" and \
                float(record.get("end_ts", 0)) > storage.now() + 1:
            schedule(bot, chat_id, logger=logger)
            resumed += 1
            continue
        # زمان تمام شده ولی کاری در جریان نیست ⇒ همین حالا اجرا شود.
        resumed += 1
        try:
            await QUEUE.submit(key=_key(chat_id),
                               job=lambda c=chat_id: finish(
                                   bot, c, logger=logger),
                               dedupe_key="finish", logger=logger)
        except Exception:                            # noqa: BLE001
            pass
    if resumed:
        log_info(logger, f"TIMED RECOVERY RESUMED count={resumed} "
                         f"checked={checked}")
    return resumed


def ensure_recovery_loop(bot, logger=None):
    """اگر حلقهٔ نگهبان زنده نیست، دوباره بالا می‌آید (idempotent)."""
    task = _RECOVERY.get("task")
    if task is not None and not task.done():
        if task.get_loop() is asyncio.get_running_loop():
            return task
    _RECOVERY["task"] = asyncio.ensure_future(_recovery_loop(bot, logger))
    try:
        _RECOVERY["task"].set_name("timed:recovery")
    except Exception:                                # noqa: BLE001
        pass
    return _RECOVERY["task"]


def stop_recovery_loop():
    task = _RECOVERY.pop("task", None)
    if task is not None and not task.done():
        task.cancel()


async def shutdown():
    """خاموشی تمیز: تایمرها و نگهبان متوقف می‌شوند (وضعیت در دیسک می‌ماند)."""
    stop_recovery_loop()
    for task in list(_TIMERS.values()):
        task.cancel()
    _TIMERS.clear()


def reset_all(chat_id=None):
    """پاک‌سازی وضعیت درون‌حافظه (تست)."""
    stop_recovery_loop()
    if chat_id is None:
        for task in list(_TIMERS.values()):
            task.cancel()
        _TIMERS.clear()
        return
    _drop_timer(chat_id)
