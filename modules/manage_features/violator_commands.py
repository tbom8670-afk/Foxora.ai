# -*- coding: utf-8 -*-
"""🎛️ روترِ دستورهای «متخلف» / «لغو متخلف».

قواعد (مطابق خواستهٔ کارفرما):

* **ثبت** («متخلف») فقط با ریپلای روی پیامِ کاربرِ هدف کار می‌کند؛ بدون
  ریپلای پیامِ راهنما داده می‌شود.
* **لغو** («لغو متخلف») هم با ریپلای و هم با یوزرنیم کار می‌کند:
  ``لغو متخلف @یوزرنیم``. اگر یوزرنیم داده شده باشد، یوزرنیم بر ریپلای
  اولویت دارد (همان سیاستِ «اختصاصی»).
* دسترسی: فقط ``access.is_registered_admin`` (مالکِ ثبت‌شده یا ادمینِ
  **ثبت‌شدهٔ ربات**). ادمینِ عادیِ خودِ سروش که در ربات ثبت نشده باشد رد
  می‌شود.
* هدفِ محافظت‌شده (مالکِ ثبت‌شدهٔ گروه، مالک سراسری، سازندهٔ گروه، خود
  ربات) با پیامِ روشن رد می‌شود.
* اگر نتوانستیم سازندهٔ گروه را از API رسمی تشخیص بدهیم، ثبت انجام
  **نمی‌شود** و پیامِ خطای فنی داده می‌شود.
* اگر ربات حقِ «حذف پیام» در گروه را نداشته باشد، ثبت انجام نمی‌شود.
* پس از ثبتِ موفق، **پیامِ ریپلای‌شده** هم حذف می‌شود (اگر حذف آن شکست
  بخورد فقط لاگ می‌شود).
* هر تغییرِ RPC در صفِ مستقل خودش اجرا می‌شود (بدون قفل‌کردنِ مسیرِ پیام).

خودِ متخلف (حتی اگر ادمینِ ثبت‌شده باشد) نمی‌تواند این دستور را پردازش کند:
گیتِ ``violators.gate_message`` پیش از این روتر اجرا می‌شود و پیامش را
حذف می‌کند (پایانِ پردازش).
"""
from __future__ import annotations

import asyncio

from modules.manage_features import access, peers, storage, texts, violators
from modules.manage_features.queue import PerGroupQueue
from modules.manage_features.rpc import call

#: صف مستقلِ دستورهای متخلف (حذفِ پیام‌ها در صفِ خودِ موتور انجام می‌شود).
QUEUE = PerGroupQueue("violator_command", max_pending_per_key=8,
                      max_pending_total=128, task_timeout=30.0)

COMMANDS = frozenset({"متخلف", "لغو متخلف"})


def normalize(text) -> str:
    value = str(text or "").replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\ufeff", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return " ".join(value.split())


def is_command(text) -> bool:
    """آیا این پیام به «متخلف» مربوط است؟ (ارزان: بدون RPC)

    ``متخلف`` / ``لغو متخلف`` تنها، یا با آرگومانِ یوزرنیم:
    ``لغو متخلف @یوزرنیم`` (همان الگوی ``exclusive_commands``).
    """
    cleaned = normalize(text)
    return cleaned in COMMANDS or cleaned.startswith(
        ("متخلف ", "لغو متخلف "))


def _target_token(text):
    """یوزرنیم/شناسهٔ بعد از دستور (اگر با ریپلای نیامده باشد)."""
    cleaned = normalize(text)
    for prefix in ("لغو متخلف", "متخلف"):
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix):].strip()
            if rest:
                return rest.lstrip("@")
    return None


def _label(name, username, target_id):
    if username:
        return "@" + str(username).lstrip("@")
    if name:
        return name
    return "----"


def display_of(user) -> str:
    """یوزرنیم اگر داشت، وگرنه نام نمایشی — هرگز شناسهٔ عددی."""
    from modules.user_display import format_user

    return format_user(user, fallback="----")


def _sender_from_event(event):
    """نویسندهٔ پیامِ ریپلای‌شده اگر روی رویداد/کش موجود باشد (بدون RPC)."""
    candidates = [
        getattr(event, "reply_to_sender", None),
        getattr(event, "_bot_cached_reply_sender", None),
    ]
    message = getattr(event, "message", None)
    reply_message = getattr(message, "reply_to_message", None) if message else None
    if reply_message is not None:
        candidates.append(getattr(reply_message, "sender", None))
        candidates.append(getattr(reply_message, "from_id", None))
    for candidate in candidates:
        if candidate is None:
            continue
        if hasattr(candidate, "id") or hasattr(candidate, "user_id"):
            return candidate
    return None


async def _resolve_target(bot, event, chat_id, token=None):
    """``((entity, user_id, name, username), token_used)`` — یا ``(None, …)``.

    * با **ریپلای** (و بدون آرگومانِ یوزرنیم): هدف از پیامِ ریپلای‌شده خوانده
      می‌شود.
    * با **یوزرنیم** (``لغو متخلف @یوزرنیم``): یک resolve رسمی (همان مسیرِ
      «اختصاصی»: ``peers.resolve_user``). یوزرنیم بر ریپلای اولویت دارد.

    ``token_used`` به لایهٔ بالا می‌گوید پیامِ خطا باید «یوزرنیم پیدا نشد»
    باشد یا «روی پیام کاربر ریپلای کنید».
    ``entity`` در کل مسیر پاس داده می‌شود تا resolveِ دوباره («Could not
    find the input entity») رخ ندهد.
    """
    token = str(token or "").strip().lstrip("@").strip()
    if not token:
        reply_to = getattr(event, "reply_to", None)
        if reply_to is None:
            return None, False
        target = await _reply_target(bot, event, chat_id)
        return target, False

    numeric = token.lstrip("-").isdigit()
    try:
        entity, identifier = await peers.resolve_user(bot, token,
                                                     chat_id=chat_id)
    except Exception as error:                       # noqa: BLE001
        # resolve نشد ⇒ حداقل شناسهٔ عددی را نگه می‌داریم تا لایهٔ پایین
        # پیامِ روشن بدهد (نه «موفق»).
        if getattr(bot, "logger", None) is not None:
            try:
                bot.logger.log_error(
                    "VIOLATOR USERNAME RESOLVE FAILED "
                    f"chat_id={chat_id} token={token!r} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        fallback_id = int(token) if numeric else None
        if fallback_id is None:
            return None, True
        return (None, fallback_id, "", ""), True
    if entity is None and identifier is None:
        return None, True
    return (entity, identifier, display_of(entity),
            getattr(entity, "username", None) or ""), True


async def _reply_target(bot, event, chat_id):
    """``(entity, user_id, name, username)`` از پیامِ ریپلای‌شده یا ``None``."""
    cached_sender = _sender_from_event(event)
    if cached_sender is not None:
        identifier = peers.user_identifier(cached_sender)
        return (cached_sender, identifier, display_of(cached_sender),
                getattr(cached_sender, "username", None) or "")
    # مسیر پشتیبان: یک get_messages روی peer درستِ گروه (نه شناسهٔ مثبت).
    reply_to = getattr(event, "reply_to", None)
    try:
        message = await bot.client.get_messages(
            peers.group_peer(bot, chat_id),
            ids=getattr(reply_to, "reply_to_msg_id", None))
    except Exception as error:                       # noqa: BLE001
        if getattr(bot, "logger", None) is not None:
            try:
                bot.logger.log_error(
                    "VIOLATOR TARGET READ FAILED "
                    f"chat_id={chat_id} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return None
    sender = await message.get_sender() if message else None
    if sender is None:
        return None
    return (sender, peers.user_identifier(sender), display_of(sender),
            getattr(sender, "username", None) or "")


async def _reply(bot, event, payload, logger=None):
    text, entities = payload
    try:
        return await event.reply(text, formatting_entities=entities)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(f"VIOLATOR REPLY FAILED error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return None


def _plain_bold(text):
    return (text, [texts.MessageEntityBold(offset=0,
                                           length=texts.u16(text))])


def _error_payload(kind, username):
    return texts.violator_message(username=username, kind=kind)


async def _delete_reply_message(bot, event, chat_id, logger=None):
    """حذفِ پیامِ ریپلای‌شده (پیامی که مالک/ادمین روی آن دستور زده).

    اگر حذف نشد **فقط لاگ** می‌شود و دستورِ اصلی شکست نمی‌خورد.
    """
    reply_to = getattr(event, "reply_to", None)
    message_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
    if not message_id:
        return False
    try:
        numeric = int(message_id)
    except (TypeError, ValueError):
        return False
    peer = peers.group_peer(bot, chat_id)
    what = f"VIOLATOR REPLY DELETE chat={chat_id}"
    try:
        await violators_call_delete(bot, peer, numeric, logger=logger, what=what)
        return True
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    f"VIOLATOR REPLY DELETE FAILED chat_id={chat_id} "
                    f"message_id={numeric} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return False


async def violators_call_delete(bot, peer, message_id, *, logger=None,
                                what="VIOLATOR REPLY DELETE"):
    """یک حذفِ تکی از طریق پوششِ امنِ ربات (تایم‌اوت + FloodWait)."""
    try:
        return await call(bot.client.delete_messages, peer, [message_id],
                          revoke=True, logger=logger, timeout=10.0,
                          max_retry=2, what=what)
    except TypeError as error:
        if "revoke" not in str(error).lower():
            raise
        return await call(bot.client.delete_messages, peer, [message_id],
                          logger=logger, timeout=10.0, max_retry=2,
                          what=what)


async def handle(bot, event, chat_id, user_id, sender, text, logger=None):
    """``True`` یعنی پیام مصرف شد."""
    cleaned = normalize(text)
    if not is_command(text):
        return False
    if getattr(event, "is_private", False):
        return False

    username = getattr(sender, "username", None)
    if not access.is_registered_admin(chat_id, user_id, username):
        await _reply(bot, event,
                     _plain_bold(texts.VIOLATOR_NO_PERMISSION), logger)
        return True

    revoke = cleaned == "لغو متخلف" or cleaned.startswith("لغو متخلف ")

    # ⚠️ «ثبت» فقط با ریپلای؛ «لغو» با ریپلای یا با یوزرنیم.
    token = _target_token(text)
    if token and not revoke:
        # «متخلف @یوزرنیم» پشتیبانی نمی‌شود: ثبت باید روی پیامِ خودِ کاربر
        # ریپلای شود تا هویتِ هدف قطعی باشد.
        await _reply(bot, event,
                     _plain_bold(texts.VIOLATOR_NO_TARGET), logger)
        return True

    try:
        target, token_used = await _resolve_target(bot, event, chat_id,
                                                   token)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    "VIOLATOR TARGET RESOLVE CRASHED "
                    f"chat_id={chat_id} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        target, token_used = None, bool(token)
    if target is None:
        if token_used:
            await _reply(bot, event,
                         _error_payload("username_not_found",
                                        "@" + str(token or "")), logger)
        else:
            await _reply(bot, event,
                         _plain_bold(texts.VIOLATOR_NO_TARGET), logger)
        return True

    entity, target_id, target_name, target_username = target
    if target_id is None:
        await _reply(bot, event, _error_payload("no_target_entity", ""),
                     logger)
        return True
    label = _label(target_name, target_username, target_id)

    async def job():
        """فقط تغییرِ رکورد — بدون خواندنِ اضافه و بدون انتظارِ تأیید."""
        if revoke:
            violators.unmark(chat_id, target_id, reason="manual",
                             logger=logger)
            return "revoked"
        await violators.mark(bot, chat_id, target_id, actor_id=user_id,
                             name=target_name, username=username,
                             entity=entity, logger=logger)
        return "marked"

    try:
        result = await QUEUE.run(
            key=storage.group_key(chat_id), job=job,
            dedupe_key=f"{'revoke' if revoke else 'mark'}:"
                       f"{peers.user_key(target_id)}",
            logger=logger)
    except violators.ProtectedUser as error:
        _log_full(bot, logger, "VIOLATOR BLOCKED (protected target)",
                  chat_id, target_id, error)
        await _reply(bot, event, _error_payload("protected", label), logger)
        return True
    except violators.CreatorUnknown as error:
        _log_full(bot, logger, "VIOLATOR BLOCKED (creator unknown)",
                  chat_id, target_id, error)
        await _reply(bot, event, _error_payload("creator_unknown", label),
                     logger)
        return True
    except violators.BotLacksDeleteRights as error:
        _log_full(bot, logger, "VIOLATOR BLOCKED (bot lacks delete right)",
                  chat_id, target_id, error)
        await _reply(bot, event, _error_payload("bot_no_rights", label),
                     logger)
        return True
    except violators.AlreadyViolator:
        await _reply(bot, event, _error_payload("already", label), logger)
        return True
    except violators.NotViolator:
        await _reply(bot, event, _error_payload("missing", label), logger)
        return True
    except asyncio.TimeoutError as error:
        _log_full(bot, logger, "VIOLATOR TIMEOUT", chat_id, target_id, error)
        await _reply(bot, event, _error_payload("timeout", label), logger)
        return True
    except Exception as error:                       # noqa: BLE001
        # خطای ناشناخته: هرگز «موفق» اعلام نمی‌شود.
        _log_full(bot, logger, "VIOLATOR COMMAND FAILED",
                  chat_id, target_id, error)
        await _reply(bot, event, _error_payload("failed", label), logger)
        return True

    if result == "marked":
        # پیامِ ریپلای‌شده هم حذف می‌شود (شکستِ آن فقط لاگ است).
        await _delete_reply_message(bot, event, chat_id, logger)
        await _reply(bot, event, texts.violator_message(
            username=label, kind="marked"), logger)
    else:
        await _reply(bot, event, texts.violator_message(
            username=label, kind="unmarked"), logger)
    return True


def _log_full(bot, logger, title, chat_id, target_id, error):
    """ثبتِ کاملِ خطا (نوع + متن + traceback کوتاه) در لاگ."""
    import traceback

    message = (f"{title} chat_id={chat_id} "
               f"user_id={target_id} error={error!r}")
    target_logger = logger if logger is not None else getattr(bot, "logger",
                                                              None)
    if target_logger is None:
        return
    try:
        target_logger.log_error(message)
    except Exception:                                # noqa: BLE001
        pass
    try:
        target_logger.log_error(
            "VIOLATOR TRACEBACK " +
            "".join(traceback.format_exception(type(error), error,
                                               error.__traceback__))[-1500:])
    except Exception:                                # noqa: BLE001
        pass


def recover(bot, logger=None) -> dict:
    """بازیابی پس از ری‌استارت: رکوردها روی دیسک‌اند و ایندکس بازسازی
    می‌شود؛ کش سازنده در پس‌زمینه پر می‌شود."""
    return violators.recover(bot, logger)


def reset_all() -> None:
    violators.reset_all()
