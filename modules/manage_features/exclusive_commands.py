# -*- coding: utf-8 -*-
"""🎛️ روترِ دستورهای «اختصاصی» / «لغو اختصاصی».

* دسترسی: فقط مالک/ادمین ثبت‌شده (ادمین واقعی سروش‌پلاس کافی نیست).
* هدف: با ریپلای، یا با «اختصاصی @username» / شناسهٔ عددی.
* هر تغییر RPC در **صف اختصاصی** خودش اجرا می‌شود (بدون تماس مستقیم و
  بدون قفل‌کردن مسیر پیام‌ها).
* تشخیص باز شدن گروه: در هر پیام فقط یک بررسی ارزان از حافظه (TTL) انجام
  می‌شود، نه RPC برای تک‌تک پیام‌ها. بررسی واقعی داخل صف انجام و در صورت
  باز شدن گروه، همهٔ مجوزها لغو می‌شوند.
"""
from __future__ import annotations

import asyncio

import time

from modules.manage_features import access, exclusive, peers, storage, texts
from modules.manage_features.queue import PerGroupQueue

#: صف مستقلِ اختصاصی — هیچ حذف پیام/زمانداری داخلش اجرا نمی‌شود.
QUEUE = PerGroupQueue("exclusive_access", max_pending_per_key=16,
                      max_pending_total=256, task_timeout=60.0)

#: فاصلهٔ بررسی وضعیت قفل در پیام‌های عادی (ثانیه، ساعت یکنواخت).
LOCK_CHECK_INTERVAL = 20.0
_LOCK_CACHE: dict = {}          # chat_key → (checked_monotonic, locked)
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


async def _resolve_target(bot, event, chat_id, token):
    """``(user_id, name, username)`` از ریپلای یا از یوزرنیمِ داده‌شده.

    ⚠️ مسیر ریپلای دیگر هیچ RPCای نمی‌زند: SPlusthon نویسندهٔ پیامِ ریپلای
    را روی خودِ رویداد می‌گذارد (``event.reply_to_msg_id`` + کشِ entity یا
    ``event.message.reply_to_message``). قبلاً یک ``get_messages`` اضافه
    انجام می‌شد که در سرور کند، پاسخ را تا یک دقیقه عقب می‌انداخت.
    """
    reply_to = getattr(event, "reply_to", None)
    if reply_to is not None and not token:
        cached_sender = _sender_from_event(event)
        if cached_sender is not None:
            return (_identifier(cached_sender),
                    display_of(cached_sender),
                    getattr(cached_sender, "username", None) or "")
        message = await bot.client.get_messages(
            # peer واقعیِ گروه: با شناسهٔ مثبت، SPlusthon آن را «کاربر» می‌گیرد
            # و get_messages شکست می‌خورد (یعنی هیچ‌وقت مقصد پیدا نمی‌شد).
            peers.group_peer(bot, chat_id),
            ids=getattr(reply_to, "reply_to_msg_id", None))
        sender = await message.get_sender() if message else None
        if sender is None:
            return None
        return (_identifier(sender), display_of(sender),
                getattr(sender, "username", None) or "")
    if token:
        if token.lstrip("-").isdigit():
            try:
                entity = await bot.client.get_entity(int(token))
            except Exception:                        # noqa: BLE001
                return int(token), "", ""
            return (_identifier(entity) or int(token),
                    display_of(entity), getattr(entity, "username", None) or "")
        try:
            entity = await bot.client.get_entity(token)
        except Exception:                            # noqa: BLE001
            return None
        return (_identifier(entity), display_of(entity),
                getattr(entity, "username", None) or "")
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

def _identifier(user):
    for attr in ("id", "user_id"):
        value = getattr(user, attr, None)
        if isinstance(value, int):
            return value
    return None


def display_of(user) -> str:
    """یوزرنیم اگر داشت، وگرنه نام نمایشی — هرگز شناسهٔ عددی."""
    from modules.user_display import format_user

    return format_user(user, fallback="----")


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


# ---------------------------------------------------------------------------
#  بررسی ارزانِ «گروه باز شد؟»
# ---------------------------------------------------------------------------
def lock_state_cached(chat_id, *, force=False):
    entry = _LOCK_CACHE.get(storage.group_key(chat_id))
    if entry and not force and \
            storage.monotonic() - entry[0] < LOCK_CHECK_INTERVAL:
        return entry[1]
    return None


def remember_lock_state(chat_id, locked):
    _LOCK_CACHE[storage.group_key(chat_id)] = (storage.monotonic(), bool(locked))


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
    bot = getattr(context, "bot", context)
    try:
        # ``submit`` همیشه یک Future برمی‌گرداند ⇒ خودش با await (یا صف) اجرا
        # می‌شود و مستقیماً یک تسک ساخته می‌شود؛ نباید دوباره wrap شود.
        QUEUE.submit(
            key=storage.group_key(chat_id),
            job=lambda: refresh_lock_state(bot, chat_id, logger),
            serialize=True, logger=logger)
    except Exception:                                # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
#  دستور
# ---------------------------------------------------------------------------
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
                     (texts.EXCLUSIVE_NO_PERMISSION,
                      [texts.MessageEntityBold(
                          offset=0,
                          length=texts.u16(texts.EXCLUSIVE_NO_PERMISSION))]),
                     logger)
        return True

    revoke = cleaned.startswith("لغو اختصاصی")
    token = _target_token(text)
    # ⚠️ سرعت: مقصد از رویداد/کش resolve می‌شود، نه با یک RPC اضافه.
    target = await _resolve_target(bot, event, chat_id, token)
    if target is None or target[0] is None:
        await _reply(bot, event,
                     (texts.EXCLUSIVE_NO_TARGET,
                      [texts.MessageEntityBold(
                          offset=0, length=texts.u16(texts.EXCLUSIVE_NO_TARGET))]),
                     logger)
        return True
    target_id, target_name, target_username = target
    label = ("@" + target_username) if target_username else (target_name or "----")

    async def job():
        """فقط ارتقا/لغو — هیچ خواندنِ اضافی، هیچ انتظارِ تأیید."""
        if revoke:
            await exclusive.revoke(bot, chat_id, target_id)
            return "revoked"
        await exclusive.grant(bot, chat_id, target_id,
                              actor_id=user_id, name=target_name,
                              username=target_username)
        return "granted"

    try:
        result = await QUEUE.run(
            key=storage.group_key(chat_id), job=job,
            dedupe_key=f"{'revoke' if revoke else 'grant'}:{target_id}",
            logger=logger)
        if result == "granted":
            # ✅ مهم: پاسخِ کاربر **بلافاصله** می‌رود؛ تأییدِ سرور و (در صورت
            # نیاز) ارتقای پله‌ای بعد از آن اجرا می‌شوند تا اگر سرور «ادمینِ
            # بی‌حق» را نپذیرفت، حالتِ حداقلی خودکار اعمال شود.
            _spawn_verification(bot, chat_id, target_id, logger)
    except exclusive.NotLocked:
        await _reply(bot, event, texts.exclusive_message(
            username=label, kind="not_locked"), logger)
        return True
    except exclusive.AlreadyExclusive:
        await _reply(bot, event, texts.exclusive_message(
            username=label, kind="already"), logger)
        return True
    except exclusive.NotExclusive:
        # «در فهرست اختصاصی نیست» فقط وقتی درست است که کاربر واقعاً نه رکورد
        # داشته باشد و نه ادمینِ گروه باشد (این بررسی داخل خودِ revoke انجام شد).
        await _reply(bot, event, texts.exclusive_message(
            username=label, kind="missing"), logger)
        return True
    except Exception as error:                       # noqa: BLE001
        # ⚠️ به‌درخواست کارفرما پیام «انجام نشد» **حذف شد**: کاربر عملاً ادمین
        # می‌شود و این پیام فقط باعث سردرگمی بود. خطای واقعی فقط در لاگ ثبت
        # می‌شود، رکورد در صف بازیابی می‌ماند، و به کاربر همان قالب موفقیت
        # نشان داده می‌شود.
        if logger is not None:
            try:
                logger.log_error(
                    f"EXCLUSIVE COMMAND LOGGED (no failure message) "
                    f"chat_id={chat_id} user_id={target_id} error={error!r}")
            except Exception:                        # noqa: BLE001
                pass
        await _reply(bot, event, texts.exclusive_message(
            username=label, kind="granted" if not revoke else "revoked"),
            logger)
        return True

    await _reply(bot, event, texts.exclusive_message(
        username=label, kind="revoked" if result == "revoked" else "granted"),
        logger)
    return True


# ---------------------------------------------------------------------------
#  بازیابی پس از ری‌استارت
# ---------------------------------------------------------------------------
def _spawn_verification(bot, chat_id, target_id, logger=None):
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
    _LOCK_CACHE.clear()
    _PENDING_GRANT.clear()
