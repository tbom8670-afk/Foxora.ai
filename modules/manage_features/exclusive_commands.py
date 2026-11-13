# -*- coding: utf-8 -*-
"""🎛️ روترِ دستورهای «اختصاصی» / «لغو اختصاصی».

* دسترسی: فقط مالک/ادمین ثبت‌شده (ادمین واقعی سروش‌پلاس کافی نیست).
* هدف: با ریپلای، یا با «اختصاصی @username» / شناسهٔ عددی. **entity واقعی**
  کاربر در کل مسیر (handle ← grant ← grant_participant_rights) پاس داده
  می‌شود تا resolveِ دوبارهٔ ``get_input_entity(int)`` و خطای
  «Could not find the input entity» رخ ندهد.
* هر تغییر RPC در **صف اختصاصی** خودش اجرا می‌شود (بدون تماس مستقیم و
  بدون قفل‌کردن مسیر پیام‌ها).
* تشخیص باز شدن گروه: در هر پیام فقط یک بررسی ارزان از حافظه (TTL) انجام
  می‌شود، نه RPC برای تک‌تک پیام‌ها. بررسی واقعی داخل صف انجام و در صورت
  باز شدن گروه، همهٔ مجوزها لغو می‌شوند.

⚠️ «موفق» فقط وقتی اعلام می‌شود که RPC واقعاً بدون خطا انجام شده باشد
(``exclusive.grant``/``revoke`` بدون استثنا برگردند). هر خطا به یک پیامِ
فارسیِ معنادار نگاشت می‌شود و پروندهٔ کاملِ خطا در لاگ ثبت می‌گردد؛ رکورد هم
برای ریکاوری روی دیسک می‌ماند.
"""
from __future__ import annotations

import asyncio

from modules.manage_features import (access, exclusive, peers, storage,
                                     timed as timed_engine, texts)
from modules.manage_features.queue import PerGroupQueue
from modules.self_admin_shield import is_self_target

#: صف مستقلِ اختصاصی — هیچ حذف پیام/زمان‌داری داخلش اجرا نمی‌شود.
QUEUE = PerGroupQueue("exclusive_access", max_pending_per_key=16,
                      max_pending_total=256, task_timeout=60.0)

#: فاصلهٔ بررسی وضعیت قفل در پیام‌های عادی (ثانیه، ساعت یکنواخت).
#: کشِ واقعی در ``peers`` است (``peers.LOCK_CACHE_TTL``) تا فقط یک منبعِ
#: حقیقت وجود داشته باشد.
LOCK_CHECK_INTERVAL = peers.LOCK_CACHE_TTL
_PENDING_GRANT: dict = {}       # chat_key → {"ts": ..., "target": ...}


def is_command(text) -> bool:
    return normalize(text) in {"اختصاصی", "لغو اختصاصی"} or \
        normalize(text).startswith(("اختصاصی ", "لغو اختصاصی "))


def normalize(text) -> str:
    value = str(text or "").replace("\u200c", " ").replace("\u200f", "")
    value = value.replace("\u200e", "").replace("\ufeff", "")
    value = value.replace("ي", "ی").replace("ك", "ک")
    return " ".join(value.split())


def _target_token(text):
    """یوزرنیم/شناسهٔ بعد از دستور (اگر با ریپلای نیامده باشد)."""
    cleaned = normalize(text)
    for prefix in ("لغو اختصاصی", "اختصاصی"):
        if cleaned.startswith(prefix):
            rest = cleaned[len(prefix):].strip()
            if rest:
                return rest.lstrip("@")
    return None


def _sender_from_event(event):
    """نویسندهٔ پیامِ ریپلای‌شده، اگر روی رویداد/کش موجود باشد (بدون RPC)."""
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


def display_of(user) -> str:
    """یوزرنیم اگر داشت، وگرنه نام نمایشی — هرگز شناسهٔ عددی."""
    from modules.user_display import format_user

    return format_user(user, fallback="----")


async def _resolve_target(bot, event, chat_id, token):
    """``(entity, user_id, name, username)`` از ریپلای یا یوزرنیم/آیدی.

    ``entity`` همان چیزی است که در ادامهٔ مسیر پاس داده می‌شود (بدون resolveِ
    دوباره). اگر resolve شکست بخورد ``entity=None`` است و ``user_id`` عددی
    باقی می‌ماند؛ لایهٔ پایین‌تر با پیامِ روشن شکست را گزارش می‌کند.
    """
    reply_to = getattr(event, "reply_to", None)
    if reply_to is not None and not token:
        cached_sender = _sender_from_event(event)
        if cached_sender is not None:
            identifier = peers.user_identifier(cached_sender)
            return (cached_sender, identifier, display_of(cached_sender),
                    getattr(cached_sender, "username", None) or "")
        # مسیر پشتیبان: یک get_messages روی peer واقعی (نه شناسهٔ مثبت).
        message = await bot.client.get_messages(
            peers.group_peer(bot, chat_id),
            ids=getattr(reply_to, "reply_to_msg_id", None))
        sender = await message.get_sender() if message else None
        if sender is None:
            return None
        return (sender, peers.user_identifier(sender), display_of(sender),
                getattr(sender, "username", None) or "")

    if token:
        numeric = token.lstrip("-").isdigit()
        try:
            entity, identifier = await peers.resolve_user(bot, token,
                                                         chat_id=chat_id)
        except Exception as error:                   # noqa: BLE001
            # resolve نشد ⇒ حداقل شناسهٔ عددی یا یوزرنیم را نگه می‌داریم تا
            # لایهٔ پایین پیامِ روشن بدهد (نه «موفق»).
            fallback_id = int(token) if numeric else None
            try:
                bot.logger.log_error(
                    "EXCLUSIVE TARGET RESOLVE FAILED "
                    f"chat_id={chat_id} token={token!r} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
            return (None, fallback_id, "", token if not numeric else "")
        if entity is None:
            return None
        return (entity, identifier, display_of(entity),
                getattr(entity, "username", None) or "")
    return None


async def _reply(bot, event, payload, logger=None):
    text, entities = payload
    try:
        return await event.reply(text, formatting_entities=entities)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(f"EXCLUSIVE REPLY FAILED error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        return None


def _plain_bold(text):
    return (text, [texts.MessageEntityBold(offset=0,
                                           length=texts.u16(text))])


def _error_payload(kind, username):
    """پیامِ فارسیِ متناسب با نوعِ خطا (قالب‌ها در ``texts``)."""
    return texts.exclusive_message(username=username, kind=kind)


# ---------------------------------------------------------------------------
#  بررسی ارزانِ «گروه باز شد؟»
# ---------------------------------------------------------------------------
def lock_state_cached(chat_id, *, force=False):
    """وضعیت کش‌شدهٔ قفل (منبعِ واحد: ``peers``)."""
    if force:
        return None
    return peers.lock_state_cached(chat_id)


def remember_lock_state(chat_id, locked):
    return peers.remember_lock_state(chat_id, locked)


async def refresh_lock_state(bot, chat_id, logger=None):
    """خواندن وضعیت واقعی + لغو خودکارِ مجوزها اگر گروه باز شده باشد."""
    # این مسیر خودش «بررسیِ واقعی» است؛ نباید از کشِ کوتاه‌مدت بخواند.
    exclusive.forget_lock_state(chat_id)
    locked = await exclusive.is_group_locked(bot, chat_id)
    remember_lock_state(chat_id, locked)
    if not locked and exclusive.list_entries(chat_id):
        try:
            await QUEUE.run(
                key=storage.group_key(chat_id),
                job=lambda: exclusive.revoke_all(
                    bot, chat_id, reason="group_unlocked", logger=logger),
                dedupe_key="revoke_all", logger=logger,
            )
        except Exception as error:                   # noqa: BLE001
            if logger is not None:
                try:
                    logger.log_error(
                        "EXCLUSIVE AUTO REVOKE FAILED "
                        f"chat_id={chat_id} error={error!r}")
                except Exception:                    # noqa: BLE001
                    pass
    return locked


async def watch(chat_id, context, *, logger=None):
    """هوکِ مسیر عادی پیام: اگر گروهی مجوزِ اختصاصی دارد و بررسی کهنه است،
    وضعیت قفل را تازه می‌کند (فقط برای همان گروه‌هایی که رکورد دارند)."""
    if not exclusive.list_entries(chat_id):
        return
    if lock_state_cached(chat_id) is not None:
        return
    bot = (
        context.get("bot")
        if isinstance(context, dict)
        else getattr(context, "bot", context)
    )
    try:
        await QUEUE.submit(
            key=storage.group_key(chat_id),
            job=lambda: refresh_lock_state(bot, chat_id, logger),
            dedupe_key="refresh_lock_state",
            serialize=True, logger=logger)
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  دستور
# ---------------------------------------------------------------------------
def _label(name, username, target_id):
    if username:
        return "@" + str(username).lstrip("@")
    if name:
        return name
    return "----"


async def handle(bot, event, chat_id, user_id, sender, text, logger=None):
    """``True`` یعنی پیام مصرف شد."""
    cleaned = normalize(text)
    if cleaned not in {"اختصاصی", "لغو اختصاصی"} and \
            not cleaned.startswith(("اختصاصی ", "لغو اختصاصی ")):
        return False
    if getattr(event, "is_private", False):
        return False

    username = getattr(sender, "username", None)
    if not access.is_registered_admin(chat_id, user_id, username):
        await _reply(bot, event,
                     _plain_bold(texts.EXCLUSIVE_NO_PERMISSION), logger)
        return True

    revoke = cleaned.startswith("لغو اختصاصی")
    token = _target_token(text)
    # ⚠️ سرعت: مقصد از رویداد/کش resolve می‌شود، نه با یک RPC اضافه.
    try:
        target = await _resolve_target(bot, event, chat_id, token)
    except Exception as error:                       # noqa: BLE001
        if logger is not None:
            try:
                logger.log_error(
                    "EXCLUSIVE TARGET RESOLVE CRASHED "
                    f"chat_id={chat_id} token={token!r} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        target = None
    if target is None:
        await _reply(bot, event, _plain_bold(texts.EXCLUSIVE_NO_TARGET), logger)
        return True

    entity, target_id, target_name, target_username = target
    if entity is None and target_id is None:
        # هدفِ خالی با یوزرنیمِ ناموجود اشتباه گرفته نشود: این‌جا کاربر
        # شناسایی نشده، پس پیام «کاربر پیدا نشد» داده می‌شود.
        await _reply(bot, event, _error_payload("no_target_entity", ""),
                     logger)
        return True
    if is_self_target(target_id, bot=bot) or is_self_target(entity, bot=bot):
        await _reply(
            bot,
            event,
            _plain_bold("⚠️ این پیام متعلق به خود ربات است؛ روی پیام کاربر موردنظر ریپلای کنید"),
            logger,
        )
        return True
    label = _label(target_name, target_username, target_id)

    async def job():
        """فقط ارتقا/لغو — هیچ خواندنِ اضافی، هیچ انتظارِ تأیید."""
        if revoke:
            await exclusive.revoke(bot, chat_id, target_id, entity=entity)
            return "revoked"
        await exclusive.grant(bot, chat_id, target_id,
                              actor_id=user_id, name=target_name,
                              username=target_username, entity=entity)
        return "granted"

    try:
        result = await QUEUE.run(
            key=storage.group_key(chat_id), job=job,
            dedupe_key=f"{'revoke' if revoke else 'grant'}:"
                       f"{peers.user_key(target_id)}",
            logger=logger)
    except exclusive.NotLocked:
        await _reply(bot, event,
                     _error_payload("not_locked", label), logger)
        return True
    except exclusive.AlreadyExclusive:
        await _reply(bot, event,
                     _error_payload("already", label), logger)
        return True
    except exclusive.NotExclusive:
        # «در فهرست اختصاصی نیست» فقط وقتی درست است که کاربر واقعاً نه رکورد
        # داشته باشد و نه ادمینِ گروه باشد (این بررسی داخل خودِ revoke انجام شد).
        await _reply(bot, event,
                     _error_payload("missing", label), logger)
        return True
    except exclusive.ExcludedUser as error:
        _log_full(bot, logger, "EXCLUSIVE BLOCKED (user banned by bot)",
                  chat_id, target_id, error)
        await _reply(bot, event,
                     _error_payload("banned_user", label), logger)
        return True
    except exclusive.BotLacksRights as error:
        _log_full(bot, logger, "EXCLUSIVE BLOCKED (bot lacks rights)",
                  chat_id, target_id, error)
        await _reply(bot, event,
                     _error_payload("bot_no_rights", label), logger)
        return True
    except exclusive.TargetChanged as error:
        _log_full(bot, logger, "EXCLUSIVE BLOCKED (target/group unresolved)",
                  chat_id, target_id, error)
        await _reply(bot, event,
                     _error_payload("no_target_entity", label), logger)
        return True
    except exclusive.GrantRejected as error:
        _log_full(bot, logger, "EXCLUSIVE GRANT REJECTED (kept for recovery)",
                  chat_id, target_id, error)
        await _reply(bot, event,
                     _error_payload("failed", label), logger)
        return True
    except asyncio.TimeoutError as error:
        _log_full(bot, logger, "EXCLUSIVE TIMEOUT (kept for recovery)",
                  chat_id, target_id, error)
        await _reply(bot, event, _error_payload("timeout", label), logger)
        return True
    except Exception as error:                       # noqa: BLE001
        # خطای ناشناخته: **هرگز** «موفق» اعلام نمی‌شود؛ پروندهٔ کامل لاگ و
        # رکورد برای ریکاوری باقی می‌ماند.
        _log_full(bot, logger, "EXCLUSIVE COMMAND FAILED",
                  chat_id, target_id, error)
        await _reply(bot, event,
                     _error_payload("failed", label), logger)
        return True

    if result == "granted":
        # پاسخِ کاربر **بلافاصله** می‌رود؛ تأییدِ سرور (سه‌حالته) بعد از آن.
        _spawn_verification(bot, chat_id, target_id, entity, logger)
    await _reply(bot, event, texts.exclusive_message(
        username=label, kind="revoked" if result == "revoked" else "granted"),
        logger)
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
            "EXCLUSIVE TRACEBACK " +
            "".join(traceback.format_exception(type(error), error,
                                               error.__traceback__))[-1500:])
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  بازیابی پس از ری‌استارت
# ---------------------------------------------------------------------------
def _spawn_verification(bot, chat_id, target_id, entity=None, logger=None):
    """اجرای تأیید در پس‌زمینه (پاسخ کاربر معطل نمی‌شود)."""
    try:
        task = asyncio.ensure_future(
            exclusive._verify_in_background(bot, chat_id, target_id, None))
    except RuntimeError:
        return None
    try:
        task.set_name(f"exclusive-verify:{storage.group_key(chat_id)}")
    except Exception:                                # noqa: BLE001
        pass
    return task


def recover(bot, logger=None):
    """رکوردهای نیمه‌کاره را برمی‌گرداند تا صف دوباره بررسی‌شان کند."""
    jobs = exclusive.pending_jobs()
    if jobs and logger is not None:
        try:
            logger.log_info(f"EXCLUSIVE RECOVERY QUEUED count={len(jobs)}")
        except Exception:                            # noqa: BLE001
            pass
    return jobs


def reset_all():
    peers.clear_caches()
    _PENDING_GRANT.clear()
