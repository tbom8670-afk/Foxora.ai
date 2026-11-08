# -*- coding: utf-8 -*-
"""🧬 «پاکسازی کامل» — جریانِ دستور: تأیید، قفل، صفِ اختصاصی، پیامِ وضعیت.

معماری (مطابقِ خواستهٔ کارفرما):

1. دستور «پاکسازی کامل» فقط از مالک/ادمینِ ثبت‌شده پذیرفته می‌شود
   (``_has_group_management_permission`` همان تابعِ بقیهٔ دستورهای مدیریتی).
2. اول پیامِ قالب ۱ (تأیید) فرستاده می‌شود؛ «تایید» ⇐ شروع، «لغو» ⇐ انصراف.
3. بعد از تأیید: گروه با همان سیستمِ قفلِ موجود
   (``bot.group_actions.lock_group/unlock_group``) قفل می‌شود، یک پیام
   وضعیت فرستاده و همان پیام در طول کار ویرایش می‌شود.
4. خودِ پاکسازی در یک تسکِ مستقل و صفِ مخصوصِ همین گروه اجرا می‌شود؛ هیچ
   اشتراکی با صف‌های حذفِ دیگر ندارد. در پایان، قفل به وضعیت قبلی برمی‌گردد
   (حتی در خطا/لغو).
"""
from __future__ import annotations

import asyncio
import time

from modules.full_purge import core, texts
from modules.full_purge.texts import MessageEntityBold

#: (chat_id, user_id) → {"created_at": float} — فقط تأییدهای در انتظارِ هر گروه.
_PENDING: dict = {}
PENDING_TTL = 180.0
_TASKS: dict = {}

CONFIRM_WORDS = frozenset({"تایید", "تأیید", "✅ تایید", "✅ تأیید"})
CANCEL_WORDS = frozenset({"لغو", "❌ لغو"})


def normalize(text) -> str:
    value = str(text or "").replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\ufeff", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return " ".join(value.split())


def is_command(text) -> bool:
    return normalize(text) == "پاکسازی کامل"


def pending_for(chat_id):
    entry = _PENDING.get(str(chat_id))
    if not entry:
        return None
    if time.monotonic() - entry.get("created_at", 0) > PENDING_TTL:
        _PENDING.pop(str(chat_id), None)
        return None
    return entry


def claims(chat_id, user_id, text) -> bool:
    """آیا این پیام مربوط به «پاکسازی کامل» است؟ (برای مسیر روتر)"""
    normalized = normalize(text)
    if normalized == "پاکسازی کامل":
        return True
    entry = pending_for(chat_id)
    if not entry:
        return False
    if entry.get("user_id") not in (None, user_id):
        return False
    return normalized in CONFIRM_WORDS or normalized in CANCEL_WORDS


def _remember(chat_id, user_id):
    _PENDING[str(chat_id)] = {"user_id": user_id,
                              "created_at": time.monotonic()}
    while len(_PENDING) > 500:
        oldest = min(_PENDING, key=lambda key: _PENDING[key]["created_at"])
        _PENDING.pop(oldest, None)


def forget(chat_id):
    _PENDING.pop(str(chat_id), None)


# ---------------------------------------------------------------------------
# ارسال/ویرایش
# ---------------------------------------------------------------------------
async def _reply(bot, event, chat_id, text, entities, logger=None):
    try:
        return await event.reply(text, formatting_entities=entities)
    except Exception as error:
        if logger is not None:
            try:
                logger.log_error(
                    f"FULL PURGE REPLY FAILED chat_id={chat_id} error={error!r}")
            except Exception:
                pass
        try:
            return await bot.client.send_message(chat_id, text,
                                                 formatting_entities=entities)
        except Exception:
            return None


async def send_confirm(bot, event, chat_id, logger=None):
    text = texts.confirm_text()
    return await _reply(bot, event, chat_id, text,
                        texts.confirm_entities(text), logger)


def start_allowed(bot, chat_id, user_id, username) -> bool:
    """فقط مالک/ادمینِ ثبت‌شده — از همان سیستمِ دسترسیِ موجود."""
    try:
        from handlers.message_handler import _has_group_management_permission
        return bool(_has_group_management_permission(
            bot, chat_id, user_id, username))
    except Exception:
        try:
            from modules import admin_tools
            return bool(admin_tools.has_admin_permission(chat_id, user_id, username))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# پاکسازی
# ---------------------------------------------------------------------------
def _lock_hooks(bot, chat_id):
    """قلاب‌های قفل/بازکردن با همان ``group_actions`` بات."""
    actions = getattr(bot, "group_actions", None)

    async def lock():
        if actions is None:
            return False
        try:
            await actions.lock_group(chat_id)
            return True
        except Exception as error:
            if getattr(bot, "logger", None):
                bot.logger.log_error(
                    f"FULL PURGE LOCK FAILED chat_id={chat_id} error={error!r}")
            return False

    async def unlock():
        if actions is None:
            return False
        try:
            await actions.unlock_group(chat_id)
            return True
        except Exception as error:
            if getattr(bot, "logger", None):
                bot.logger.log_error(
                    f"FULL PURGE UNLOCK FAILED chat_id={chat_id} error={error!r}")
            return False

    return lock, unlock


async def run_purge(bot, chat_id, *, status_message=None, logger=None):
    """اجرای پاکسازیِ کامل. اگر مسابقه‌ای در جریان باشد، کاری نمی‌کند."""
    if core.is_running(chat_id):
        return {"deleted": 0, "reason": "already_running"}
    lock, unlock = _lock_hooks(bot, chat_id)
    editor = core.StatusEditor(bot, chat_id, status_message, logger=logger)
    result = await core.run(bot, chat_id, status=editor, logger=logger,
                            on_lock=lock, on_unlock=unlock)
    if logger is not None:
        try:
            logger.log_info(
                "FULL PURGE FINISHED "
                f"chat_id={chat_id} deleted={result.get('deleted')} "
                f"scanned={result.get('scanned')} pages={result.get('pages')} "
                f"failed={result.get('failed')} reason={result.get('reason')!r} "
                f"elapsed_ms={int(result.get('elapsed', 0) * 1000)}"
            )
        except Exception:
            pass
    return result


def _spawn(coro, name):
    try:
        return asyncio.get_running_loop().create_task(coro, name=name)
    except RuntimeError:
        return None


def _track(chat_id, task):
    if task is None:
        return
    key = str(chat_id)
    _TASKS[key] = task

    def _done(_task, key=key):
        _TASKS.pop(key, None)

    try:
        task.add_done_callback(_done)
    except Exception:
        pass


async def handle(bot, event, chat_id, user_id, sender, text, logger=None):
    """مسیرِ کاملِ دستور: «پاکسازی کامل» → تأیید → اجرا → پایان.

    ``True`` یعنی پیام مصرف شد.
    """
    normalized = normalize(text)
    username = getattr(sender, "username", None)
    if getattr(event, "is_private", False):
        await _reply(bot, event, chat_id, texts.PRIVATE_NOTICE,
                     [MessageEntityBold(
                         offset=0, length=texts.u16(texts.PRIVATE_NOTICE))],
                     logger)
        return True

    # ---- ۱) دستور شروع ----
    if normalized == "پاکسازی کامل":
        if not start_allowed(bot, chat_id, user_id, username):
            await _reply(bot, event, chat_id, texts.NO_PERMISSION,
                         [MessageEntityBold(
                             offset=0,
                             length=texts.u16(texts.NO_PERMISSION))], logger)
            return True
        if core.is_running(chat_id):
            busy = f"{texts.BUSY_TITLE}\n\n{texts.BUSY_BODY}"
            await _reply(bot, event, chat_id, busy,
                         texts.status_entities(busy), logger)
            return True
        _remember(chat_id, user_id)
        await send_confirm(bot, event, chat_id, logger)
        if logger is not None:
            try:
                logger.log_info(
                    f"FULL PURGE CONFIRM SENT chat_id={chat_id} user_id={user_id}")
            except Exception:
                pass
        return True

    # ---- ۲) پاسخِ تأیید/لغو ----
    entry = pending_for(chat_id)
    if not entry:
        return False
    if entry.get("user_id") not in (None, user_id):
        return False
    if normalized in CANCEL_WORDS:
        forget(chat_id)
        cancel_text = f"{texts.CANCEL_TITLE}\n\n{texts.CANCEL_BODY}"
        await _reply(bot, event, chat_id, cancel_text,
                     texts.status_entities(cancel_text), logger)
        if logger is not None:
            try:
                logger.log_info(
                    f"FULL PURGE CANCELLED chat_id={chat_id} user_id={user_id}")
            except Exception:
                pass
        return True
    if normalized not in CONFIRM_WORDS:
        return False

    forget(chat_id)
    if core.is_running(chat_id):
        busy = f"{texts.BUSY_TITLE}\n\n{texts.BUSY_BODY}"
        await _reply(bot, event, chat_id, busy, texts.status_entities(busy), logger)
        return True

    # پیامِ وضعیت: همان پیامی که تا آخر ویرایش می‌شود.
    status_text = texts.status_text(state="locked")
    status_message = await _reply(bot, event, chat_id, status_text,
                                  texts.status_entities(status_text), logger)
    task = _spawn(
        run_purge(bot, chat_id, status_message=status_message, logger=logger),
        name=f"full-purge:{chat_id}",
    )
    _track(chat_id, task)
    if logger is not None:
        try:
            logger.log_info(
                f"FULL PURGE STARTED chat_id={chat_id} user_id={user_id}")
        except Exception:
            pass
    return True


def reset_all(chat_id=None):
    """آزادسازیِ وضعیتِ درون‌حافظه (برای تست/ری‌استارت)."""
    if chat_id is None:
        _PENDING.clear()
        _TASKS.clear()
        return
    forget(chat_id)
    _TASKS.pop(str(chat_id), None)
