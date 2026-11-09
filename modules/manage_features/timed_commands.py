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

from modules.group_id import normalize_group_id
from modules.manage_features import access, storage, texts, timed
from modules.manage_features.queue import PerGroupQueue

#: صف مستقلِ ویرایش/وضعیت پیام زماندار (کارهای سبک؛ حذف در صف خودِ موتور).
QUEUE = PerGroupQueue("timed_message_ui", max_pending_per_key=8,
                      max_pending_total=128, task_timeout=30.0)

CHOICE_TTL = 180.0
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
    text, entities = payload
    try:
        return await bot.client.edit_message(message, text,
                                             formatting_entities=entities)
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
    """شناسهٔ پیامِ پیشرفت را نگه می‌دارد (همان پیام ویرایش می‌شود)."""
    _STATUS[_key(chat_id)] = message_id
    record = timed.get_record(chat_id)
    if record:
        record = dict(record)
        record["status_message_id"] = message_id
        timed._write(chat_id, record)


def status_id(chat_id):
    record = timed.get_record(chat_id) or {}
    return record.get("status_message_id") or _STATUS.get(_key(chat_id))


async def publish_status(bot, chat_id, *, deleted, failed, elapsed, locked,
                         state="running", logger=None):
    message_id = status_id(chat_id)
    payload = texts.timed_progress(deleted=deleted, failed=failed,
                                   elapsed_seconds=elapsed, locked=locked,
                                   state=state)
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
    try:
        record = timed.start(chat_id, minutes, actor_id=user_id, name=display,
                             username=actor_username)
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

    status = await _reply(bot, event, texts.timed_progress(
        deleted=0, failed=0, elapsed_seconds=0, locked=False), logger)
    if status is not None:
        register_status(chat_id, getattr(status, "id", None))

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
    if not record.get("status_message_id"):
        return None
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


def reset_all(chat_id=None):
    timed.reset_all(chat_id)
    if chat_id is None:
        _CHOICES.clear()
        _STATUS.clear()
    else:
        clear_choice(chat_id)
        _STATUS.pop(_key(chat_id), None)
