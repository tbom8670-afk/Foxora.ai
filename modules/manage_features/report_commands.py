# -*- coding: utf-8 -*-
"""🎛️ روترِ «گزارش» + تأیید/لغو مالک.

* «گزارش» با ریپلای ⇒ قالب ۴ (درخواست دلیل) و ثبت وضعیت انتظار.
* پیام بعدی همان کاربر در همان گروه = دلیل ⇒ ساخت پرونده و ارسال قالب ۵
  برای **مالک ثبت‌شده** در همان گروه.
* «تایید» / «لغو» فقط توسط مالک ثبت‌شده (نه ادمین عادی) و فقط روی آخرین
  پروندهٔ در انتظارِ همان گروه.
* محدودیت ۷ ثانیه‌ای پایدار به‌ازای (گروه، کاربر).
* کل کار سنگین (ساخت پرونده/اطلاع‌رسانی) در صف مستقل گزارش اجرا می‌شود.
"""
from __future__ import annotations

from modules.group_id import normalize_group_id
from modules.manage_features import access, reports, storage, texts
from modules.manage_features.queue import PerGroupQueue

QUEUE = PerGroupQueue("user_report", max_pending_per_key=32,
                      max_pending_total=512, task_timeout=30.0)

#: پروندهٔ در انتظارِ تصمیمِ مالک: chat_key → case_id
_AWAITING: dict = {}

CONFIRM_WORDS = frozenset({"تایید", "تأیید", "✅ تایید", "✅ تأیید",
                           "◆ تایید", "◆ تأیید"})
CANCEL_WORDS = frozenset({"لغو", "❌ لغو", "◆ لغو"})
WARNINGS_PER_REPORT = 2


def normalize(text) -> str:
    value = str(text or "").replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\ufeff", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return " ".join(value.split())


def is_command(text, chat_id=None, user_id=None) -> bool:
    """آیا این پیام به «گزارش» مربوط است؟ (ارزان: بدون RPC)

    شامل: خودِ دستور، دلیلِ کسی که در انتظار است، و تأیید/لغو وقتی پرونده‌ای
    منتظر تصمیم مالک است.
    """
    cleaned = normalize(text)
    if cleaned == "گزارش":
        return True
    if chat_id is None:
        return False
    if user_id is not None and reports.pending_reason(chat_id, user_id):
        return bool(cleaned)
    if awaiting_case(chat_id) and (cleaned in CONFIRM_WORDS
                                   or cleaned in CANCEL_WORDS):
        return True
    return False


async def _reply(bot, event, payload, logger=None):
    text, entities = payload
    try:
        return await event.reply(text, formatting_entities=entities)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(f"REPORT REPLY FAILED error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return None


def display_of(user) -> str:
    from modules.user_display import format_user

    return format_user(user, fallback="----")


async def _target_from_reply(bot, event, chat_id):
    reply_to = getattr(event, "reply_to", None)
    if reply_to is None:
        return None
    message = await bot.client.get_messages(
        chat_id, ids=getattr(reply_to, "reply_to_msg_id", None))
    if message is None:
        return None
    sender = await message.get_sender()
    if sender is None:
        return None
    message_id = getattr(message, "id", None) or getattr(
        reply_to, "reply_to_msg_id", None)
    return sender, message_id


# ---------------------------------------------------------------------------
#  ورودی اصلی
# ---------------------------------------------------------------------------
async def handle(bot, event, chat_id, user_id, sender, text, logger=None):
    """``True`` یعنی پیام مصرف شد."""
    cleaned = normalize(text)
    if getattr(event, "is_private", False):
        return False

    # ---- ۱) دستور «گزارش» ----
    if cleaned == "گزارش":
        wait = reports.seconds_until_allowed(chat_id, user_id)
        if wait > 0:
            message = f"کمی صبر کنید ({int(wait) + 1} ثانیه)"
            await _reply(bot, event,
                         (message, [texts.MessageEntityBold(
                             offset=0, length=texts.u16(message))]), logger)
            return True

        target = await _target_from_reply(bot, event, chat_id)
        if target is None:
            message = "روی پیام فرد متخلف ریپلای کنید"
            await _reply(bot, event,
                         (message, [texts.MessageEntityBold(
                             offset=0, length=texts.u16(message))]), logger)
            return True
        target_user, target_message_id = target
        target_id = getattr(target_user, "id", None)
        if target_id is None:
            return True
        if str(target_id) == str(user_id):
            message = "گزارش خودتان ممکن نیست"
            await _reply(bot, event,
                         (message, [texts.MessageEntityBold(
                             offset=0, length=texts.u16(message))]), logger)
            return True

        reporter_label = display_of(sender)
        reports.remember_pending(
            chat_id, user_id, message_id=target_message_id,
            target_id=target_id, target_name=display_of(target_user),
            target_username=getattr(target_user, "username", "") or "")
        await _reply(bot, event, texts.report_ask(username=reporter_label),
                     logger)
        return True

    # ---- ۲) دلیل گزارش (پاسخ کاربر در انتظار) ----
    pending = reports.pending_reason(chat_id, user_id)
    if pending is not None and cleaned:
        return await _submit_reason(bot, event, chat_id, user_id, sender,
                                    cleaned, pending, logger)

    # ---- ۳) تأیید/لغو مالک ----
    if cleaned in CONFIRM_WORDS or cleaned in CANCEL_WORDS:
        case_id = _AWAITING.get(storage.group_key(chat_id))
        if not case_id:
            return False
        if not access.is_owner(chat_id, user_id):
            message = "فقط مالک ثبت‌شده گروه می‌تواند تصمیم بگیرد"
            await _reply(bot, event,
                         (message, [texts.MessageEntityBold(
                             offset=0, length=texts.u16(message))]), logger)
            return True
        approve = cleaned in CONFIRM_WORDS
        return await _decide(bot, chat_id, case_id, user_id, sender,
                             approve, event, logger)

    return False


async def _submit_reason(bot, event, chat_id, user_id, sender, reason,
                         pending, logger=None):
    # متن دلیل ورودی غیرقابل‌اعتماد است: فقط تمیز و محدود می‌شود.
    reason = " ".join(reason.split())[:1000]
    reports.forget_pending(chat_id, user_id)
    wait = reports.seconds_until_allowed(chat_id, user_id)
    if wait > 0:
        return True
    owner = access.owner_id(chat_id)
    reporter_label = display_of(sender)
    try:
        case = reports.create_case(
            chat_id, user_id, pending.get("target_id"),
            pending.get("message_id"), reason,
            reporter_name=reporter_label,
            reporter_username=getattr(sender, "username", "") or "",
            target_name=pending.get("target_name", ""),
            target_username=pending.get("target_username", ""),
            owner_id=owner)
    except reports.ReportError as error:
        message = ("این گزارش قبلاً ثبت شده است"
                   if "duplicate" in str(error) else "گزارش ثبت نشد")
        await _reply(bot, event,
                     (message, [texts.MessageEntityBold(
                         offset=0, length=texts.u16(message))]), logger)
        return True
    reports._stamp_rate(chat_id, user_id)

    _AWAITING[storage.group_key(chat_id)] = case["case_id"]

    owner_user = None
    if owner is not None:
        try:
            owner_user = await bot.client.get_entity(int(owner))
        except Exception:                            # noqa: BLE001
            owner_user = None
    owner_label = display_of(owner_user) if owner_user is not None else "----"
    target_label = (("@" + case["target_username"])
                    if case.get("target_username")
                    else (case.get("target_name") or "----"))
    await _reply(bot, event, texts.report_to_owner(
        owner_username=owner_label,
        reporter_username=reporter_label,
        target_username=target_label,
        warnings=WARNINGS_PER_REPORT), logger)
    if logger is not None:
        try:
            logger.log_error(
                "REPORT CREATED "
                f"chat_id={chat_id} case={case['case_id']} "
                f"reporter={user_id} target={case['target_id']} "
                f"owner={owner}")
        except Exception:                            # noqa: BLE001
            pass
    return True


async def _decide(bot, chat_id, case_id, user_id, sender, approve, event,
                  logger=None):
    async def job():
        if approve:
            return reports.approve(
                bot, chat_id, case_id, actor_id=user_id, actor=sender,
                logger=logger)
        return reports.reject(chat_id, case_id, actor_id=user_id, actor=sender,
                              logger=logger)

    try:
        case = await QUEUE.run(key=storage.group_key(chat_id), job=job,
                               dedupe_key=f"decide:{case_id}", logger=logger)
    except reports.CaseAlreadyDecided:
        return True
    except reports.CaseNotFound:
        return True
    except reports.SelfDecision:
        message = "گزارش‌دهنده نمی‌تواند تصمیم بگیرد"
        await _reply(bot, event,
                     (message, [texts.MessageEntityBold(
                         offset=0, length=texts.u16(message))]), logger)
        return True
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    f"REPORT DECIDE FAILED chat_id={chat_id} "
                    f"case={case_id} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return True

    _AWAITING.pop(storage.group_key(chat_id), None)
    return True


def awaiting_case(chat_id):
    return _AWAITING.get(storage.group_key(chat_id))


def reset_all():
    _AWAITING.clear()
    reports.reset_all()


def recover(bot, logger=None):
    """بازیابی پس از ری‌استارت: پرونده‌های در انتظار تصمیم مالک.

    دلیل‌ها و پرونده‌ها در دیسک‌اند؛ فقط «کدام پرونده منتظر تصمیم است» از
    وضعیت `pending` بازسازی می‌شود تا «تایید» بعد از ری‌استارت هم کار کند.
    """
    data = storage.REPORT_STORAGE.load()
    restored = 0
    for key, record in (data.get("groups") or {}).items():
        pending = [case for case in (record.get("cases") or {}).values()
                   if case.get("state") == "pending"]
        if not pending:
            continue
        pending.sort(key=lambda case: case.get("created_ts", 0))
        _AWAITING[key] = pending[-1]["case_id"]
        restored += 1
    if restored and logger is not None:
        try:
            logger.log_info(f"REPORT RECOVERY count={restored}")
        except Exception:                            # noqa: BLE001
            pass
    return restored
