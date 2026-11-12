# -*- coding: utf-8 -*-
"""روتر دستورهای «محدودیت پیام <عدد>» و «لغو محدودیت پیام» / «لغو محدودیت».

قواعد اجرایی:

* فقط مدیر/مالک مجاز (``access.is_registered_admin``: مالک سراسری ربات، مالک
  ثبت‌شدهٔ گروه یا ادمین ثبت‌شدهٔ ربات) اجازهٔ اجرای این دستورات را دارد.
* هر دو دستور با ریپلای روی پیامِ کاربرِ هدف کار می‌کنند و هویتِ هدف به صورت
  دقیق بررسی می‌شود (کاربر دیلیت‌اکانت، پیام سرویس یا هویت نامعتبر رد می‌شود).
* فقط چهار قالبِ رسمیِ اسکرین‌شات کارفرما در گروه ارسال می‌شوند؛ در خطاها یا
  حالت‌های نامعتبر، رویداد در لاگ داخلی ثبت می‌شود و در صورت تعریف‌شدنِ قالبِ
  تکمیلی در ``texts.MESSAGE_LIMIT_EXTRA_TEMPLATES`` از همان نقطهٔ اتصال استفاده
  خواهد شد.
"""
from __future__ import annotations

from modules.user_display import format_user

from . import access, message_limit, peers, texts
from .rpc import call


def is_command(text: str) -> bool:
    """آیا متن پیام یکی از دستورهای «محدودیت پیام» یا «لغو محدودیت پیام» است؟"""
    matched_set, _ = message_limit.parse_set_command(text)
    if matched_set:
        return True
    return message_limit.is_cancel_command(text)


def _has_reply_indicator(event) -> bool:
    if getattr(event, "is_reply", False):
        return True
    if getattr(event, "reply_to", None) is not None:
        return True
    if getattr(event, "reply_to_msg_id", None) is not None:
        return True
    if getattr(event, "reply_to_sender", None) is not None:
        return True
    if getattr(event, "_bot_cached_reply_sender", None) is not None:
        return True
    msg = getattr(event, "message", None)
    if msg is not None:
        if getattr(msg, "reply_to", None) is not None:
            return True
        if getattr(msg, "reply_to_msg_id", None) is not None:
            return True
        if getattr(msg, "reply_to_message", None) is not None:
            return True
    return False


def _is_invalid_user_entity(entity) -> bool:
    if entity is None:
        return False
    if bool(getattr(entity, "deleted", False)):
        return True
    cls_name = type(entity).__name__
    if cls_name in ("Channel", "ChannelForbidden", "Chat", "ChatForbidden", "PeerChannel", "PeerChat"):
        return True
    if hasattr(entity, "broadcast") or hasattr(entity, "megagroup"):
        return True
    return False


def _cached_reply_sender(event):
    candidates = [
        getattr(event, "reply_to_sender", None),
        getattr(event, "_bot_cached_reply_sender", None),
    ]
    msg = getattr(event, "message", None)
    reply_msg = getattr(msg, "reply_to_message", None) if msg is not None else None
    if reply_msg is not None:
        candidates.append(getattr(reply_msg, "sender", None))
        candidates.append(getattr(reply_msg, "from_id", None))
    for cand in candidates:
        if cand is not None and (hasattr(cand, "id") or hasattr(cand, "user_id")):
            return cand
    return None


async def _resolve_reply_target(bot, event, chat_id):
    """استخراج ``(entity, user_id, label, username)`` از پیام ریپلای‌شده یا ``None``."""
    if not _has_reply_indicator(event):
        return None

    sender = _cached_reply_sender(event)
    reply_msg = None

    if sender is None and hasattr(event, "get_reply_message"):
        try:
            reply_msg = await event.get_reply_message()
        except Exception:  # noqa: BLE001
            reply_msg = None

    if reply_msg is None and sender is None:
        reply_to = getattr(event, "reply_to", None)
        reply_msg_id = (
            getattr(reply_to, "reply_to_msg_id", None)
            or getattr(event, "reply_to_msg_id", None)
            or getattr(getattr(event, "message", None), "reply_to_msg_id", None)
        )
        client = getattr(bot, "client", None)
        if reply_msg_id and client is not None and hasattr(client, "get_messages"):
            try:
                fetched = await client.get_messages(
                    peers.group_peer(bot, chat_id),
                    ids=int(reply_msg_id),
                )
                reply_msg = fetched[0] if isinstance(fetched, list) and fetched else fetched
            except Exception:  # noqa: BLE001
                reply_msg = None

    if reply_msg is not None:
        # رد کردن پیام‌های سرویس (ورود/خروج/پین و ...)
        if getattr(reply_msg, "action", None) is not None or "Service" in type(reply_msg).__name__:
            return None
        if sender is None:
            sender = getattr(reply_msg, "sender", None)
        if sender is None and hasattr(reply_msg, "get_sender"):
            try:
                sender = await reply_msg.get_sender()
            except Exception:  # noqa: BLE001
                sender = None

    if sender is not None:
        if _is_invalid_user_entity(sender):
            return None
        uid = peers.user_identifier(sender)
        if uid is None or int(uid) <= 0:
            return None
        uname = str(getattr(sender, "username", "") or "").strip().lstrip("@")
        label = format_user(sender, fallback=f"@{uname}" if uname else "کاربر")
        return sender, int(uid), label, uname

    if reply_msg is not None:
        raw_sid = getattr(reply_msg, "sender_id", None)
        if raw_sid is None:
            from_id = getattr(reply_msg, "from_id", None)
            if from_id is not None and "Channel" in type(from_id).__name__:
                return None
            raw_sid = peers.user_identifier(from_id)
        uid = peers.user_identifier(raw_sid)
        if uid is not None and int(uid) > 0:
            cached_entity = message_limit.cached_user_entity(bot, int(uid))
            if _is_invalid_user_entity(cached_entity):
                return None
            uname = str(getattr(cached_entity, "username", "") or "").strip().lstrip("@")
            label = (
                format_user(cached_entity, fallback=f"@{uname}" if uname else "کاربر")
                if cached_entity is not None
                else "کاربر"
            )
            return cached_entity, int(uid), label, uname

    return None


async def _send_reply(event, payload, logger=None, what: str = "MSG_LIMIT CMD_REPLY"):
    if payload is None:
        return None
    text, entities = payload
    try:
        return await call(
            lambda: event.reply(text, formatting_entities=entities),
            logger=logger,
            timeout=15.0,
            max_retry=2,
            what=what,
        )
    except Exception as exc:  # noqa: BLE001
        if logger and hasattr(logger, "log_error"):
            logger.log_error(f"[MSG_LIMIT] خطا در ارسال پاسخ دستور: {exc}")
        return None


async def _emit_optional(event, kind: str, logger=None, **context) -> None:
    payload = texts.message_limit_optional_template(kind, **context)
    if payload is not None:
        await _send_reply(event, payload, logger=logger, what=f"MSG_LIMIT OPT:{kind}")


async def handle(
    bot,
    event,
    chat_id,
    user_id,
    sender=None,
    text: str = "",
    logger=None,
    *,
    chat=None,
) -> bool:
    """پردازش دستور «محدودیت پیام» یا «لغو محدودیت پیام».

    خروجی ``True`` یعنی پیام مربوط به این قابلیت بود و مصرف شد.
    """
    if getattr(event, "is_private", False):
        return False

    matched_set, limit_val = message_limit.parse_set_command(text)
    matched_cancel = False if matched_set else message_limit.is_cancel_command(text)
    if not matched_set and not matched_cancel:
        return False

    log = logger or getattr(bot, "logger", None)

    # ۱) بررسی دسترسی مدیر/مالک مجاز
    if not access.is_registered_admin(chat_id, user_id):
        if log and hasattr(log, "log_info"):
            log.log_info(
                f"[MSG_LIMIT] دستور رد شد (عدم دسترسی ادمین) chat={chat_id} user={user_id}"
            )
        await _emit_optional(event, "no_permission", logger=log)
        return True

    # ۲) استخراج و اعتبارسنجی کاربر هدف از ریپلای
    resolved = await _resolve_reply_target(bot, event, chat_id)
    if resolved is None:
        if log and hasattr(log, "log_info"):
            log.log_info(
                f"[MSG_LIMIT] دستور رد شد (بدون ریپلای معتبر) chat={chat_id} by={user_id}"
            )
        await _emit_optional(event, "invalid_reply", logger=log)
        return True

    target_entity, target_id, target_label, _target_uname = resolved
    event_chat = chat if chat is not None else getattr(event, "chat", None)
    actor_uname = str(getattr(sender, "username", "") or "").strip().lstrip("@")

    # ۳-الف) دستور ثبت محدودیت پیام
    if matched_set:
        if limit_val is None or limit_val <= 0:
            if log and hasattr(log, "log_info"):
                log.log_info(
                    f"[MSG_LIMIT] عدد سقف نامعتبر است chat={chat_id} by={user_id} text={text!r}"
                )
            await _emit_optional(event, "invalid_limit", logger=log)
            return True

        try:
            saved = await message_limit.set_limit(
                bot,
                chat_id,
                target_id,
                limit_val,
                actor_id=user_id,
                actor_username=actor_uname,
                entity=target_entity,
                chat=event_chat,
                logger=log,
            )
        except message_limit.ProtectedTargetError as exc:
            if log and hasattr(log, "log_info"):
                log.log_info(
                    f"[MSG_LIMIT] هدف معاف است ({exc.reason}) chat={chat_id} target={target_id}"
                )
            await _emit_optional(
                event, "protected_target", logger=log, reason=exc.reason, username=target_label
            )
            return True
        except message_limit.UnrestrictableTargetError as exc:
            if log and hasattr(log, "log_info"):
                log.log_info(
                    f"[MSG_LIMIT] هدف از نظر API قابل محدودسازی نیست ({exc.reason}) "
                    f"chat={chat_id} target={target_id}"
                )
            await _emit_optional(
                event, "unrestrictable_target", logger=log, reason=exc.reason, username=target_label
            )
            return True
        except message_limit.MessageLimitError as exc:
            if log and hasattr(log, "log_error"):
                log.log_error(
                    f"[MSG_LIMIT] خطا در ثبت محدودیت chat={chat_id} target={target_id}: {exc}"
                )
            await _emit_optional(event, "failed", logger=log)
            return True

        final_label = saved.get("display_name") or target_label
        payload = texts.message_limit_set(
            username=final_label,
            limit=saved["allowed_count"],
        )
        await _send_reply(event, payload, logger=log, what=f"MSG_LIMIT SET chat={chat_id}")
        return True

    # ۳-ب) دستور لغو محدودیت پیام
    try:
        await message_limit.cancel_limit(
            bot,
            chat_id,
            target_id,
            actor_id=user_id,
            entity=target_entity,
            chat=event_chat,
            logger=log,
        )
    except message_limit.NoActiveLimitError:
        if log and hasattr(log, "log_info"):
            log.log_info(
                f"[MSG_LIMIT] محدودیت فعالی برای لغو وجود ندارد chat={chat_id} target={target_id}"
            )
        await _emit_optional(event, "no_active_limit", logger=log, username=target_label)
        return True
    except message_limit.UnmuteFailedError:
        if log and hasattr(log, "log_error"):
            log.log_error(
                f"[MSG_LIMIT] رفع سکوت هنگام لغو محدودیت ناموفق بود chat={chat_id} target={target_id}"
            )
        await _emit_optional(event, "unmute_failed", logger=log, username=target_label)
        return True
    except message_limit.MessageLimitError as exc:
        if log and hasattr(log, "log_error"):
            log.log_error(
                f"[MSG_LIMIT] خطا در لغو محدودیت chat={chat_id} target={target_id}: {exc}"
            )
        await _emit_optional(event, "failed", logger=log)
        return True

    payload = texts.message_limit_cancelled()
    await _send_reply(event, payload, logger=log, what=f"MSG_LIMIT CANCEL chat={chat_id}")
    return True
