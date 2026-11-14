# -*- coding: utf-8 -*-
"""🎛️ روترِ «پیام زماندار» / «لغو پیام زماندار».

جریان (مطابق قالب‌های اسکرین‌شات):

1. «پیام زماندار» ⇒ قالبِ انتخاب بازه (۱ - 30 دقیقه … 5 - 24 ساعته).
2. انتخاب عدد ۱..۵ ⇒ ثبت زمان‌سنج + پیامِ «⚙️ : پاکسازی زماندار پیام ها
   تنظیم شد» + قالبِ پیشرفت که **همان پیام** است و تا پایان ویرایش می‌شود.
3. پایان بازه ⇒ قفل گروه + حذف دقیقاً همان بازه، با به‌روزرسانی همان پیام.
4. «لغو پیام زماندار» ⇒ توقف کامل، عدم حذف، غیرفعال شدن رکورد.

هیچ پیامِ وضعیتی حذف نمی‌شود: شناسهٔ آن در رکورد ذخیره و به موتور پاکسازی
داده می‌شود (همان قاعدهٔ «پاکسازی کامل»).
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from modules.group_id import normalize_group_id
from modules.manage_features import access, storage, texts, timed
from modules.manage_features.queue import PerGroupQueue

#: صف مستقلِ ویرایش/وضعیت پیام زماندار (کارهای سبک؛ حذف در صف خودِ موتور).
QUEUE = PerGroupQueue("timed_message_ui", max_pending_per_key=8,
                      max_pending_total=128, task_timeout=30.0)

CHOICE_TTL = 180.0
#: آخرین زمانِ بازبینیِ نگهبان (برای پرهیز از کارِ تکراری در مسیر گرم).
_WATCH: dict = {}
_CHOICES: dict = {}            # chat_key → {"ts":..., "user_id":..., "message_id":...}
_STATUS: dict = {}


def normalize(text) -> str:
    value = str(text or "").replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\ufeff", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return " ".join(value.split())


def is_command(text, chat_id=None) -> bool:
    """آیا این پیام به «پیام زماندار» مربوط است؟ (فقط ارزان: بدون RPC)"""
    cleaned = normalize(text)
    if cleaned in {"پیام زماندار", "لغو پیام زماندار"}:
        return True
    if chat_id is not None and _choice_for(chat_id) and \
            cleaned in {str(index) for index in range(1, 6)}:
        return True
    return False


def _key(chat_id):
    return storage.group_key(chat_id)


def _choice_for(chat_id):
    entry = _CHOICES.get(_key(chat_id))
    if not entry:
        return None
    if storage.now() - entry["ts"] > CHOICE_TTL:
        _CHOICES.pop(_key(chat_id), None)
        return None
    return entry


def clear_choice(chat_id):
    _CHOICES.pop(_key(chat_id), None)


async def _reply(bot, event, payload, logger=None):
    text, entities = payload
    try:
        sent = await event.reply(text, formatting_entities=entities)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(f"TIMED REPLY FAILED error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return None
    return sent


async def _edit(bot, chat_id, message, payload, logger=None):
    """ویرایش همان پیامِ وضعیت — با entity واقعیِ گروه.

    ⚠️ با شناسهٔ مثبتِ گروه، SPlusthon سراغ GetUsersRequestِ کاربر ۱۰۰…
    می‌رود و ویرایش شکست می‌خورد؛ پس peer از ``timed.group_peer`` می‌آید.
    """
    text, entities = payload
    try:
        # اولویت: peerِ ذخیره‌شده در رکورد (بعد از ری‌استارت هم کار می‌کند)
        # ← کش ربات ← شناسهٔ منفیِ گروه.
        peer = timed.resolve_peer(bot, chat_id)
        return await asyncio.wait_for(
            bot.client.edit_message(peer, message, text,
                                    formatting_entities=entities),
            timeout=timed.RPC_TIMEOUT)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    f"TIMED STATUS EDIT FAILED chat_id={chat_id} "
                    f"error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return None


def register_status(chat_id, message_id):
    """شناسهٔ پیامِ پیشرفت را نگه می‌دارد (همان پیام ویرایش می‌شود).

    ⚠️ فقط همین فیلد روی رکوردِ موجود نوشته می‌شود؛ ``timed.start`` دوباره
    صدا زده نمی‌شود تا هرگز یک اجرای دوم/زمان‌سنج تازه ساخته نشود.
    """
    if message_id is None:
        return None
    _STATUS[_key(chat_id)] = message_id
    try:
        timed.register_status(chat_id, message_id)
    except Exception as error:                       # noqa: BLE001
        return None
    return message_id


async def _progress_hook(bot, chat_id, *, deleted, failed, elapsed_seconds,
                         locked, state, logger=None):
    """قلابِ موتورِ زمان‌دار → ویرایش همان پیام وضعیت (هرگز پیام جدید)."""
    # وضعیت‌های موتور عیناً به لایهٔ نمایش می‌رسند (لغو/تایم‌اوت هم دیده شود).
    mapped = state if state in {"running", "done", "cancelled",
                                "failed_timeout"} else "running"
    await publish_status(bot, chat_id, deleted=deleted, failed=failed,
                         elapsed=elapsed_seconds, locked=locked,
                         state=mapped, logger=logger)




def status_id(chat_id):
    record = timed.get_record(chat_id) or {}
    return record.get("status_message_id") or _STATUS.get(_key(chat_id))


#: قفلِ ساختِ پیام وضعیت به‌ازای هر گروه — جلوگیری از پیام‌های تکراری.
_ENSURE_LOCKS: dict = {}

#: گروه‌هایی که یک‌بار پیام وضعیت فرستادند ولی شناسه‌اش به دست نیامد.
#: دیگر پیام تازه‌ای نمی‌سازیم (کاربر به‌جای یک کارت، سه کارت نبیند).
_STATUS_FAILED: set = set()


def _sent_id(sent) -> Optional[int]:
    """شناسهٔ پیام از نتیجهٔ ارسال — با همان استخراج‌کنندهٔ سرویسِ ربات."""
    try:
        from modules.notice_cleanup import extract_sent_id
        found = extract_sent_id(sent)
        if found:
            return int(found)
    except Exception:                                # noqa: BLE001
        pass
    value = getattr(sent, "id", None)
    if isinstance(value, int) and value > 0:
        return value
    return None


async def _locate_status(bot, chat_id, text, record=None, logger=None):
    """اگر ارسال شناسه برنگرداند، همان پیام را از تاریخچهٔ کوتاه پیدا می‌کند.

    بعضی لفافه‌های ارسال در سروش ``Message`` برنمی‌گردانند؛ در آن حالت
    شناسهٔ پیامِ وضعیت را از ۲۰ پیامِ آخرِ همان گروه می‌گیریم تا پیام
    **ویرایش** شود و کارتِ تازه ساخته نشود.
    """
    if not text:
        return None
    try:
        peer = timed.resolve_peer(bot, chat_id, record)
    except Exception:                                # noqa: BLE001
        peer = None
    try:
        history = await asyncio.wait_for(
            bot.client.get_messages(peer if peer is not None else chat_id,
                                    limit=20),
            timeout=timed.RPC_TIMEOUT)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    f"TIMED STATUS LOCATE FAILED chat_id={chat_id} "
                    f"error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return None
    if history is None:
        return None
    if not isinstance(history, (list, tuple)):
        history = [history]
    wanted = str(text).strip()
    for message in history:
        body = getattr(message, "message", None) or getattr(message, "text", None)
        if body is None or str(body).strip() != wanted:
            continue
        found = getattr(message, "id", None)
        if isinstance(found, int) and found > 0:
            return found
    return None


def _message_key(chat_id):
    return storage.group_key(chat_id)


async def ensure_status_message(bot, chat_id, *, record=None, logger=None):
    """پیامِ وضعیت را **دقیقاً یک بار** می‌سازد و شناسه‌اش را ثبت می‌کند.

    ⚠️ باگِ واقعی: قلابِ ساخت پیام وضعیت هم از مسیر «پایان بازه» صدا زده
    می‌شد و هم از مسیر پیشرفت؛ چون هنوز شناسه‌ای روی رکورد نبود، هر بار یک
    پیام **تازه** ساخته می‌شد (کاربر سه پیام دید). این‌جا:

    * قفلِ ``asyncio`` به‌ازای هر گروه ⇒ هیچ ساختِ هم‌زمانِ دوتایی نداریم.
    * بعد از گرفتن قفل، رکورد **از دیسک** دوباره خوانده می‌شود؛ اگر شناسه
      نوشته شده باشد همان برگردانده می‌شود.
    * شناسه در حافظه هم نگه داشته می‌شود تا حتی اگر نوشتن روی دیسک شکست
      بخورد، پیام دوم ساخته نشود.
    """
    key = _message_key(chat_id)
    if key in _STATUS_FAILED:
        return None
    in_memory = _STATUS.get(key)
    if in_memory:
        return in_memory
    record = record or timed.get_record(chat_id) or {}
    message_id = record.get("status_message_id")
    if message_id:
        _STATUS[key] = message_id
        return message_id

    lock = _ENSURE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ENSURE_LOCKS[key] = lock
    async with lock:
        # بازبینیِ دوباره زیرِ قفل (ممکن است کارِ هم‌زمان ساخته باشد).
        if _STATUS.get(key):
            return _STATUS[key]
        fresh = timed.get_record(chat_id) or {}
        if fresh.get("status_message_id"):
            _STATUS[key] = fresh["status_message_id"]
            return fresh["status_message_id"]

        total = fresh.get("tracked")
        if total is None:
            total = len(timed.tracked_ids(chat_id, fresh))
        payload = texts.timed_progress(deleted=0, failed=0, elapsed_seconds=0,
                                       locked=True, state="running",
                                       total=total,
                                       ended_at=fresh.get("end_at_tehran"))
        try:
            sent = await asyncio.wait_for(
                bot.client.send_message(timed.resolve_peer(bot, chat_id, fresh),
                                        payload[0],
                                        formatting_entities=payload[1]),
                timeout=timed.RPC_TIMEOUT)
        except Exception as error:                   # noqa: BLE001
            if logger is not None:
                try:
                    logger.log_error(
                        f"TIMED STATUS SEND FAILED chat_id={chat_id} "
                        f"error={error!r}")
                except Exception:                    # noqa: BLE001
                    pass
            return None
        message_id = _sent_id(sent)
        if message_id is None:
            # 🔁 راهِ دوم: پیدا کردنِ همان پیام در تاریخچهٔ گروه
            message_id = await _locate_status(bot, chat_id, payload[0],
                                              fresh, logger)
        if message_id is None:
            _STATUS_FAILED.add(key)
            if logger is not None:
                try:
                    logger.log_error(
                        f"TIMED STATUS ID UNKNOWN chat_id={chat_id} "
                        "action=no_more_status_messages")
                except Exception:                    # noqa: BLE001
                    pass
            return None
        _STATUS_FAILED.discard(key)
        return register_status(chat_id, message_id)


async def publish_status(bot, chat_id, *, deleted, failed, elapsed, locked=None,
                         state="running", logger=None, total=None,
                         duration_seconds=None):
    """همان پیامِ وضعیت را با شمارشِ واقعی ویرایش می‌کند (هرگز پیام جدید).

    اطلاعات از **رکوردِ پایدار** خوانده می‌شود (نه از حافظه): تعداد کل
    شناسه‌های ثبت‌شده، زمان پایانِ ذخیره‌شده و تاخیر — تا اگر ربات/دستگاه خواب
    بوده باشد، در همان پیام دیده شود.
    """
    record = timed.get_record(chat_id) or {}
    if locked is None:
        locked = bool(record.get("lock_ok", False))
    if total is None:
        total = record.get("tracked") if record.get("tracked") is not None \
            else len(timed.tracked_ids(chat_id, record))
    if duration_seconds is None and record.get("start_ts") and record.get("end_ts"):
        duration_seconds = int(float(record["end_ts"]) -
                               float(record["start_ts"]))
    ended_at = record.get("end_at_tehran") or (
        storage.iso_tehran(float(record["end_ts"]))
        if record.get("end_ts") else None)
    late = int(record.get("late_seconds", 0) or 0)
    if state in {"done", "cancelled"}:
        late = 0

    message_id = status_id(chat_id) or record.get("status_message_id")
    payload = texts.timed_progress(deleted=deleted, failed=failed,
                                   elapsed_seconds=elapsed, locked=locked,
                                   state=state, total=total, ended_at=ended_at,
                                   late_seconds=late)
    if not message_id:
        # ⚠️ دقیقاً یک بار (با قفلِ داخلی). انتشارهای بعدی (پیشرفت) دیگر پیام
        # تازه نمی‌سازند؛ شناسه در حافظه/دیسک می‌ماند.
        message_id = await ensure_status_message(bot, chat_id, record=record,
                                                 logger=logger)
    if not message_id:
        return None
    # شناسهٔ عددیِ پیام برای edit_message کافی است (همان پیامی که فرستادیم).
    return await _edit(bot, chat_id, message_id, payload, logger)


# ---------------------------------------------------------------------------
#  دستور
# ---------------------------------------------------------------------------
async def handle(bot, event, chat_id, user_id, sender, text, logger=None):
    cleaned = normalize(text)
    if getattr(event, "is_private", False):
        return False

    username = getattr(sender, "username", None)
    allowed = access.is_registered_admin(chat_id, user_id, username)

    # ---- انتخابِ عددیِ بازه ----
    choice = _choice_for(chat_id)
    if choice and cleaned in {str(index) for index in range(1, 6)}:
        if not allowed:
            return False
        if choice.get("user_id") not in (None, user_id):
            return False
        return await _activate(bot, event, chat_id, user_id, sender,
                               int(cleaned), logger)

    if cleaned == "پیام زماندار":
        if not allowed:
            await _reply(bot, event,
                         (texts.EXCLUSIVE_NO_PERMISSION,
                          [texts.MessageEntityBold(
                              offset=0,
                              length=texts.u16(texts.EXCLUSIVE_NO_PERMISSION))]),
                         logger)
            return True
        if timed.active(chat_id):
            # از ساخت زمان‌سنج دوم جلوگیری می‌شود.
            remaining = timed.remaining_seconds(chat_id)
            await _reply(bot, event,
                         (f"زمان‌دار فعال است — باقی‌مانده "
                          f"{storage.fa_duration(remaining)}",
                          [texts.MessageEntityBold(
                              offset=0,
                              length=texts.u16(
                                  f"زمان‌دار فعال است — باقی‌مانده "
                                  f"{storage.fa_duration(remaining)}"))]),
                         logger)
            return True
        sent = await _reply(bot, event, texts.timed_chooser(
            list(enumerate(timed.DURATIONS.values(), start=1))), logger)
        _CHOICES[_key(chat_id)] = {
            "ts": storage.now(),
            "user_id": user_id,
            "message_id": getattr(sent, "id", None),
        }
        return True

    if cleaned == "لغو پیام زماندار":
        if not allowed:
            return False
        try:
            timed.cancel(chat_id, reason="manual")
        except timed.NotActive:
            return True
        await _reply(bot, event,
                     ("لغو شد؛ هیچ پیامی حذف نمی‌شود",
                      [texts.MessageEntityBold(
                          offset=0,
                          length=texts.u16("لغو شد؛ هیچ پیامی حذف نمی‌شود"))]),
                     logger)
        return True

    return False


async def _activate(bot, event, chat_id, user_id, sender, index, logger=None):
    clear_choice(chat_id)
    minutes = list(timed.DURATIONS.keys())[index - 1]
    label = timed.DURATIONS[minutes]
    actor_username = getattr(sender, "username", None) or ""
    display = " ".join(part for part in (
        getattr(sender, "first_name", None), getattr(sender, "last_name", None))
        if part) or ""
    # InputPeerِ گروه همان‌جا (وقتی همه‌چیز گرم است) گرفته و در رکورد
    # ذخیره می‌شود؛ بعد از ری‌استارت بدون resolve و بدون شکست کار می‌کند.
    peer = None
    try:
        cached = getattr(bot, "reply_input_peer_cache", None) or {}
        peer = cached.get(chat_id)
        if peer is None:
            peer = await bot.client.get_input_entity(chat_id)
    except Exception:                                # noqa: BLE001
        peer = None
    try:
        record = timed.start(chat_id, minutes, actor_id=user_id, name=display,
                             username=actor_username, peer=peer)
    except (timed.AlreadyActive, timed.InvalidDuration):
        return True

    bot_username = ""
    try:
        me = await bot.client.get_me()
        bot_username = getattr(me, "username", "") or ""
    except Exception:                                # noqa: BLE001
        bot_username = ""

    await _reply(bot, event, texts.timed_started(
        label=label,
        bot_username=("@" + bot_username) if bot_username else "",
        actor_username=("@" + actor_username) if actor_username else display),
        logger)

    # ⚠️ پیام «🧹 شروع پاکسازی …» **در لحظهٔ تنظیم** فرستاده نمی‌شود؛ این
    # پیام فقط وقتی می‌آید که بازه تمام شده باشد. پیامِ وضعیتِ زنده هم همان‌جا
    # ساخته می‌شود تا شمارشِ پیام‌ها و زمان از مقدار واقعی شروع شود.
    timed.schedule(bot, chat_id, logger=logger)
    if logger is not None:
        try:
            logger.log_info(
                f"TIMED ACTIVATED chat_id={chat_id} minutes={minutes} "
                f"by={user_id}")
        except Exception:                            # noqa: BLE001
            pass
    return True


# ---------------------------------------------------------------------------
#  هوکِ ثبت پیام‌های بازه (مسیر گرم — بدون I/O در حالت غیرفعال)
# ---------------------------------------------------------------------------
def track(chat_id, message_id, user_id=None):
    try:
        return timed.track_message(chat_id, message_id, user_id=user_id)
    except Exception:                                # noqa: BLE001
        return False


async def publish_from_record(bot, chat_id, logger=None):
    """وضعیت نهایی را روی همان پیام وضعیت می‌نویسد."""
    record = timed.get_record(chat_id) or {}
    started = float(record.get("start_ts", 0) or 0)
    elapsed = int(max(0, float(record.get("end_ts", 0) or 0) - started))
    return await publish_status(
        bot, chat_id,
        deleted=int(record.get("deleted", 0) or 0),
        failed=int(record.get("failed", 0) or 0),
        elapsed=elapsed,
        locked=bool(record.get("lock_ok", False)),
        state="done",
        logger=logger,
    )


def recover(bot, logger=None):
    return timed.recover(bot, logger)


def watch(bot, logger=None):
    """نگهبانِ ارزان: تایمرها و حلقهٔ بازیابی را زنده نگه می‌دارد.

    در مسیر گرم صدا زده می‌شود (فقط چند dict lookup) تا اگر حلقهٔ asyncio
    عوض شد یا تسک تایمر مرد، بازهٔ زمان‌دار از دست نرود و در پایان همیشه
    اجرا شود.
    """
    # حداکثر هر ۳۰ ثانیه یک بار (مسیر گرم نباید هر پیام یک بازبینی کامل کند).
    last = _WATCH.get("ts", 0.0)
    if storage.now() - last < 30.0:
        return
    _WATCH["ts"] = storage.now()
    try:
        timed.ensure_recovery_loop(bot, logger)
        data = storage.TIMED_STORAGE.load()
        for key, record in (data.get("groups") or {}).items():
            if not isinstance(record, dict):
                continue
            if record.get("state") != "active":
                continue
            if timed._timer_alive(int(key)):
                continue
            timed.schedule(bot, int(key), logger=logger)
    except Exception:                                # noqa: BLE001
        pass


# موتور زمان‌دار برای ویرایشِ زندهٔ همان پیام و ساختِ آن در لحظهٔ پایان به
# این دو قلاب تکیه می‌کند (هر دو تابع بالاتر تعریف شده‌اند).
timed.set_progress_hook(_progress_hook)
timed.set_status_hook(ensure_status_message)


def reset_all(chat_id=None):
    timed.reset_all(chat_id)
    _WATCH.clear()
    if chat_id is None:
        _CHOICES.clear()
        _STATUS.clear()
        _ENSURE_LOCKS.clear()
        _STATUS_FAILED.clear()
    else:
        clear_choice(chat_id)
        _STATUS.pop(_key(chat_id), None)
        _STATUS_FAILED.discard(_key(chat_id))
