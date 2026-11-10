# -*- coding: utf-8 -*-
"""🎛️ فرمان‌ها و هندلرِ رمزِ «تأیید دو مرحله‌ای دستورات خطرناک».

سه چیز در این لایه انجام می‌شود:

1. فرمانِ ``دو مرحله ای`` / ``لغو دو مرحله ای`` (فقط از مالک، در گروه):
   گام ۱ رمزِ جدید، گام ۲ تکرارِ همان رمز. برای لغو هم رمز فعلی پرسیده
   می‌شود (طبق مشخصات). پیام‌های رمز بلافاصله (best-effort) پاک می‌شوند.
2. گیتِ اجرای دستورهای خطرناک: وقتی قفل روشن است، غیرمالک فقط پیامِ
   «تأیید دو مرحله‌ای دارد …» را می‌بیند و **هیچ** درخواستی ساخته نمی‌شود.
   مالک یک «درخواست در انتظار» (nonce، کلید دستور، متن، انقضای ۲ دقیقه)
   می‌گیرد و رمز می‌فرستد؛ با رمزِ درست همان دستورِ اصلی، **فوراً** و با
   پرچمِ «تأیید شد» اجرا می‌شود (مالک لازم نیست دوباره دستور را بنویسد).
3. هندلرِ پیام ۸ رقمی: فقط از مالکِ همان گروه، فقط وقتی حالتِ انتظار یا
   درخواست وجود دارد؛ پیامِ رمز پاک می‌شود. هر پیام ۸ رقمیِ دیگر (یا از
   دیگران) دست‌نخورده به بقیهٔ هندلرها (بازی‌ها/جستجو/…) می‌رسد.

هیچ امضای عمومیِ ماژول‌های دیگر تغییر نمی‌کند و گروه‌های بدون قفل همان
رفتار قبلی را دارند (``gate`` → ``"ok"``).
"""
from __future__ import annotations

import asyncio

from modules.manage_features import storage, texts, two_factor
from modules.manage_features.rpc import log_info

#: نوع رویدادهایی که «پاک» می‌شوند تا بقیهٔ مسیرها آن را نبینند.
CONSUMED = True

#: متن‌های فرمان‌ها برای «لیست ادمینی»/راهنما (از یک منبع).
COMMAND_ENABLE = two_factor.COMMAND_ENABLE
COMMAND_DISABLE = two_factor.COMMAND_DISABLE


# ---------------------------------------------------------------------------
#  کمک‌های ارسال/حذف
# ---------------------------------------------------------------------------
async def _reply(bot, event, payload, logger=None):
    text, entities = payload
    try:
        return await event.reply(text, formatting_entities=entities)
    except Exception as error:                        # noqa: BLE001
        two_factor.log_error(logger, f"TWO FACTOR REPLY FAILED error={error!r}")
        try:
            return await bot.client.send_message(
                getattr(event, "chat_id", None), text,
                formatting_entities=entities)
        except Exception:                             # noqa: BLE001
            return None


async def _send(bot, chat_id, payload, logger=None):
    text, entities = payload
    try:
        return await bot.client.send_message(chat_id, text,
                                             formatting_entities=entities)
    except Exception as error:                        # noqa: BLE001
        two_factor.log_error(
            logger, f"TWO FACTOR SEND FAILED chat_id={chat_id} error={error!r}")
        return None


async def _wipe(bot, chat_id, event, logger=None):
    """پیامِ رمز را پاک کن (best-effort) — قبل از هر پاسخِ دیگر."""
    message_id = getattr(getattr(event, "message", None), "id", None)
    await two_factor.wipe_password_message(bot, chat_id, message_id, logger)


def _sender_username(sender):
    return getattr(sender, "username", None) if sender is not None else None


def _sender_name(sender):
    if sender is None:
        return ""
    parts = [getattr(sender, "first_name", None) or "",
             getattr(sender, "last_name", None) or ""]
    return " ".join(part for part in parts if part).strip()


# ---------------------------------------------------------------------------
#  ۱) فرمان‌های خودِ قابلیت
# ---------------------------------------------------------------------------
def is_command(text) -> bool:
    """آیا این متن فرمانِ «دو مرحله ای/لغو دو مرحله ای» است؟"""
    return two_factor.match_command(text) is not None


async def handle_command(bot, event, chat_id, user_id, sender, text,
                         logger=None):
    """``True`` یعنی این پیام مصرف شد."""
    action = two_factor.match_command(text)
    if action is None:
        return False
    if getattr(event, "is_private", False):
        return False

    if not two_factor.is_owner(chat_id, user_id):
        # فقط مالکِ ثبت‌شده (یا مالکِ سراسری وقتی گروه مالک ندارد).
        has_any_owner = _has_registered_owner(chat_id)
        await _reply(bot, event, texts.two_factor_owner_required(
            registered=has_any_owner), logger)
        log_info(logger, "TWO FACTOR COMMAND DENIED "
                         f"chat_id={chat_id} user_id={user_id} "
                         f"action={action}")
        return True

    enabled, _entry = two_factor.status(chat_id)
    if action == "enable":
        if enabled:
            await _reply(bot, event, texts.two_factor_message("already_on"),
                         logger)
            return True
        two_factor.begin_setup(chat_id, user_id)
        # کارتِ کامل: عنوان + نُه دستور داخل یک نقل‌قول + درخواستِ رمز.
        await _reply(bot, event, texts.two_factor_activate_request(), logger)
        log_info(logger, f"TWO FACTOR SETUP START chat_id={chat_id} "
                         f"owner_id={user_id}")
        return True

    # ---- لغو ----
    if not enabled:
        await _reply(bot, event, texts.two_factor_message("not_on"), logger)
        return True
    two_factor.begin_cancel(chat_id, user_id)   # حالت انتظار برای رمزِ فعلی
    await _reply(bot, event, texts.two_factor_disable_request(), logger)
    log_info(logger, f"TWO FACTOR DISABLE START chat_id={chat_id} "
                     f"owner_id={user_id}")
    return True


def _has_registered_owner(chat_id) -> bool:
    try:
        from modules.group_storage import get_group_owner
        return get_group_owner(chat_id) is not None
    except Exception:                                 # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
#  ۲) هندلرِ پیام ۸ رقمی (تنظیم/تکرار/لغو/تأییدِ دستور)
# ---------------------------------------------------------------------------
def looks_like_password(text) -> bool:
    """۸ رقم با ارقام فارسی/عربی/لاتین (برای تصمیمِ «این پیام را بگیرم؟»)."""
    value = two_factor.to_latin_digits(str(text or "")).strip()
    return len(value) == two_factor.PASSWORD_LENGTH and value.isdigit()


def _looks_like_short_number(text) -> bool:
    """پیامِ فقط‌عددیِ کوتاه (برای تشخیصِ «طولِ رمز اشتباه» در حالت انتظار)."""
    value = two_factor.to_latin_digits(str(text or "")).strip()
    return 1 <= len(value) <= 12 and value.isdigit()


def interest(chat_id, user_id, text) -> bool:
    """پیش‌بررسیِ ارزانِ مسیر گرم: آیا اصلاً باید این پیام را بررسی کنیم؟

    فقط چند عملیاتِ رشته‌ای و دو جست‌وجوی دیکشنری (بدون I/O، بدون RPC).
    برای اکثریتِ قاطعِ پیام‌ها ``False`` است، پس گیت/هندلر هزینه‌ای به مسیر
    عادی اضافه نمی‌کند.
    """
    if two_factor.match_command(text) is not None:
        return True
    if two_factor.awaiting_stage(chat_id, user_id) is not None:
        return True
    # دستورهای خطرناک کوتاه‌اند؛ طولِ پیام یک فیلترِ ارزان است تا برای پیام‌های
    # بلندِ عادی هیچ مقایسهٔ رشته‌ای انجام نشود.
    if len(text) <= 48 and two_factor.protected_command(text) is not None:
        return True
    if not looks_like_password(text):
        return False
    return two_factor.pending_request(chat_id) is not None


def claims(bot, chat_id, user_id, text) -> bool:
    """آیا این پیام متعلق به همین قابلیت است؟ (بدون I/O سنگین)

    * فقط **مالک** می‌تواند claim شود.
    * در حالتِ «منتظر رمز جدید/تکرار/لغو» هر پیامِ فقط‌عددیِ کوتاه از مالک
      claim می‌شود تا «رمز باید ۸ رقم باشد» صریح گفته شود (مالک در وسطِ یک
      جریانِ صریح است).
    * در حالتِ «درخواستِ اجرای دستور» فقط پیامِ **دقیقاً ۸ رقمی** claim
      می‌شود تا پاسخ‌های بازی‌ها و پیام‌های عادی دست‌نخورده بمانند.
    * پیامِ ۸ رقمی از دیگران هرگز claim نمی‌شود (به بازی‌ها می‌رسد).
    """
    # حالتِ انتظار با کلیدِ (گروه، مالک) ذخیره شده است؛ پس صرفِ وجودِ آن
    # یعنی همین کاربر همان مالک است — بدون هیچ خواندنِ جداگانه.
    if two_factor.awaiting_stage(chat_id, user_id) is not None:
        return _looks_like_short_number(text)
    if not looks_like_password(text):
        return False
    request = two_factor.pending_request(chat_id)
    return bool(request and str(request.get("owner_id")) == str(user_id))


#: محدودیتِ نرخ برای پیامِ «شما مالک ثبت شده نیستید» (ثانیه).
NOT_OWNER_NOTICE_INTERVAL = 30.0
_NOT_OWNER_NOTICE = {}


async def handle_password(bot, event, chat_id, user_id, sender, text,
                          logger=None):
    """مصرفِ رمز: ``True`` یعنی پیام مصرف شد (باید پاک شود و برگردد)."""
    if not claims(bot, chat_id, user_id, text):
        # ⚠️ پیامِ ۸ رقمی از غیرمالک: **مصرف نمی‌شود** (به بازی‌ها/بقیهٔ
        # هندلرها می‌رسد)، ولی اگر برای این گروه درخواستی در جریان باشد،
        # یک بار پیامِ «شما مالک ثبت شده نیستید» می‌گیرد — دقیقاً مثل تصویر.
        if looks_like_password(text) and two_factor.has_pending_state(chat_id):
            await _not_owner_notice(bot, chat_id, user_id, logger)
        return False

    # «فقط مالک»: یک بار، فقط وقتی واقعاً جریانی در جریان است (نه در مسیر گرم).
    if not two_factor.is_owner(chat_id, user_id):
        log_info(logger, "TWO FACTOR PASSWORD IGNORED (not owner) "
                         f"chat_id={chat_id} user_id={user_id}")
        return False

    # پیامِ رمز پیش از هر پردازش پاک می‌شود؛ شکست فقط لاگ است.
    await _wipe(bot, chat_id, event, logger)

    stage = two_factor.awaiting_stage(chat_id, user_id)
    if stage is not None:
        return await _handle_setup_stage(bot, chat_id, user_id, text, stage,
                                         sender, logger)

    return await _handle_request_password(bot, event, chat_id, user_id, text,
                                          logger)


async def _handle_setup_stage(bot, chat_id, user_id, text, stage, sender,
                              logger=None):
    """گام‌های تنظیم (new → repeat) و لغو (cancel)."""
    mode = stage.get("stage")

    if mode == "cancel":
        if not two_factor.verify_password(chat_id, text):
            return await _password_rejected(bot, chat_id, None, user_id,
                                            "cancel", logger)
        two_factor.register_success(chat_id)
        two_factor.clear_group(chat_id)
        two_factor.cancel_await(chat_id, user_id)
        await _send(bot, chat_id, texts.two_factor_message("disabled"), logger)
        log_info(logger, f"TWO FACTOR DISABLED chat_id={chat_id} "
                         f"owner_id={user_id}")
        return CONSUMED

    password, reason = two_factor.validate_password(text)
    if password is None:
        if reason == "obvious":
            payload = texts.two_factor_message("obvious")
        else:
            payload = texts.two_factor_message("invalid")
        await _send(bot, chat_id, payload, logger)
        return CONSUMED

    if mode == "new":
        two_factor.begin_repeat(chat_id, user_id, password)
        await _send(bot, chat_id, texts.two_factor_message("repeat_request"),
                    logger)
        return CONSUMED

    # ---- گام تکرار ----
    first = stage.get("first")
    if not isinstance(first, str) or not first:
        two_factor.cancel_await(chat_id, user_id)
        await _send(bot, chat_id, texts.two_factor_message("expired"), logger)
        return CONSUMED
    import hmac
    if not hmac.compare_digest(two_factor.to_latin_digits(text), first):
        # تکرار نامطابق: رمزِ اول دور ریخته می‌شود؛ باید از نو شروع کند.
        two_factor.cancel_await(chat_id, user_id)
        await _send(bot, chat_id, texts.two_factor_message("mismatch"), logger)
        log_info(logger, f"TWO FACTOR REPEAT MISMATCH chat_id={chat_id} "
                         f"owner_id={user_id}")
        return CONSUMED

    two_factor.set_password(chat_id, first, set_by=user_id)
    two_factor.cancel_await(chat_id, user_id)
    await _send(bot, chat_id, texts.two_factor_created(), logger)
    log_info(logger, f"TWO FACTOR ENABLED chat_id={chat_id} "
                     f"owner_id={user_id}")
    return CONSUMED


async def _not_owner_notice(bot, chat_id, user_id, logger=None):
    """پیامِ «رمز شما تایید نشد …» با محدودیتِ نرخ (ضدِ اسپم)."""
    import time

    key = (storage.group_key(chat_id), storage.user_key(user_id))
    now = time.monotonic()
    if now - float(_NOT_OWNER_NOTICE.get(key, 0.0)) < NOT_OWNER_NOTICE_INTERVAL:
        return False
    _NOT_OWNER_NOTICE[key] = now
    if len(_NOT_OWNER_NOTICE) > 512:
        _NOT_OWNER_NOTICE.clear()
    await _send(bot, chat_id, texts.two_factor_not_registered_owner(), logger)
    log_info(logger, "TWO FACTOR NOT OWNER (password) "
                     f"chat_id={chat_id} user_id={user_id}")
    return True


async def _handle_request_password(bot, event, chat_id, user_id, text,
                                   logger=None):
    """رمز برای اجرای یک دستورِ خطرناک."""
    request = two_factor.pending_request(chat_id)
    if request is None:
        await _send(bot, chat_id, texts.two_factor_message("expired"), logger)
        return CONSUMED

    # مالک بودن دوباره بررسی می‌شود (ممکن است مالکیت عوض شده باشد).
    if not two_factor.is_owner(chat_id, user_id) or str(
            request.get("owner_id")) != str(user_id):
        two_factor.drop_request(chat_id)
        await _send(bot, chat_id, texts.two_factor_not_registered_owner(),
                    logger)
        return CONSUMED

    remaining_lockout = two_factor.lockout_remaining(chat_id)
    if remaining_lockout > 0:
        await _send(bot, chat_id,
                    texts.two_factor_lockout(int(round(remaining_lockout))),
                    logger)
        return CONSUMED

    if not two_factor.verify_password(chat_id, text):
        return await _password_rejected(bot, chat_id, request, user_id,
                                        "request", logger)

    # ---- موفق: درخواست یک‌بار مصرف شود، بعد دستور اجرا شود ----
    request = two_factor.drop_request(chat_id, request.get("nonce"))
    two_factor.register_success(chat_id)
    if request is None:
        await _send(bot, chat_id, texts.two_factor_message("expired"), logger)
        return CONSUMED

    # ⚠️ پیامِ «اجرا شد» **بعد از** اجرای موفقِ دستور فرستاده می‌شود.
    await _run_confirmed(bot, event, chat_id, user_id, request, logger)
    return CONSUMED


async def _password_rejected(bot, chat_id, request, user_id, mode, logger=None):
    """رمزِ غلط: شمارش، قفلِ تدریجی، باطل‌کردن درخواست پس از ۳ بار."""
    recent, lockout, _level = two_factor.register_failure(chat_id)
    if lockout:
        two_factor.drop_request(chat_id)
        await _send(bot, chat_id, texts.two_factor_lockout(int(lockout)),
                    logger)
        log_info(logger, "TWO FACTOR LOCKOUT "
                         f"chat_id={chat_id} user_id={user_id} "
                         f"seconds={int(lockout)}")
        return CONSUMED

    if mode == "request" and request is not None:
        tries, dropped = two_factor.register_request_failure(
            chat_id, request.get("nonce"))
        if dropped:
            await _send(bot, chat_id, texts.two_factor_message("too_many"),
                        logger)
            log_info(logger, "TWO FACTOR REQUEST WIPED (3 wrong) "
                             f"chat_id={chat_id} user_id={user_id}")
            return CONSUMED
        attempts_left = max(0, two_factor.MAX_WRONG_PER_REQUEST - int(tries))
        await _send(bot, chat_id,
                    texts.two_factor_wrong_password(attempts_left), logger)
        return CONSUMED

    # تنظیم/تکرار/لغو: هر خطا همان پیامِ «رمز رد شد اشتباه میباشد».
    await _send(bot, chat_id, texts.two_factor_message("wrong"), logger)
    log_info(logger, f"TWO FACTOR WRONG PASSWORD chat_id={chat_id} "
                     f"user_id={user_id} mode={mode}")
    return CONSUMED


# ---------------------------------------------------------------------------
#  ۳) گیتِ دستورهای خطرناک
# ---------------------------------------------------------------------------
async def gate(bot, event, chat_id, user_id, sender, text, logger=None):
    """``True`` یعنی پیام مصرف شد (گیت جلوی اجرا را گرفت).

    سه حالت:

    * قفل خاموش / دستور محافظت‌شده نیست ⇒ ``False`` (مسیر قبلی دست‌نخورده).
    * قفل روشن + غیرمالک ⇒ پیامِ «فقط مالک با رمز» و ``True`` (هیچ درخواستی
      ساخته نمی‌شود).
    * قفل روشن + مالک ⇒ درخواستِ در انتظار + پیامِ «رمز را وارد کنید» و
      ``True`` (اجرا پس از رمزِ درست).
    """
    if getattr(event, "is_private", False):
        return False
    verdict = two_factor.gate(chat_id, user_id, text)
    if verdict == "ok":
        return False

    command = two_factor.protected_command(text) or ""

    if verdict == "denied":
        await _reply(bot, event, texts.two_factor_gate_owner_only(), logger)
        log_info(logger, "TWO FACTOR GATE DENIED "
                         f"chat_id={chat_id} user_id={user_id} "
                         f"command={command}")
        return True

    # ---- مالک: ساختِ درخواست و پرسیدن رمز ----
    lockout = two_factor.lockout_remaining(chat_id)
    if lockout > 0:
        await _reply(bot, event,
                     texts.two_factor_lockout(int(round(lockout))), logger)
        return True

    two_factor.create_request(
        chat_id, user_id, command, text,
        callback_kind=_callback_kind(bot, command),
    )
    await _reply(bot, event, texts.two_factor_ask_password(), logger)
    log_info(logger, "TWO FACTOR GATE OWNER "
                     f"chat_id={chat_id} user_id={user_id} command={command}")
    return True


#: نگاشتِ کلیدِ دستور → «handling» تا اجرای تأییدشده همان مسیرِ اصلی برود.
_HANDLERS = {}


def register_handler(command, callback):
    """ثبتِ مسیرِ اجرای یک دستورِ محافظت‌شده (برای اجرای تأییدشده)."""
    _HANDLERS[two_factor.normalize(command)] = callback
    return callback


def _callback_kind(bot, command):
    normalized = two_factor.normalize(command)
    return normalized if normalized in _HANDLERS else None


async def _run_confirmed(bot, event, chat_id, user_id, request, logger=None):
    """اجرای همان دستور با پرچمِ «تأیید شد» (بدون نیاز به نوشتنِ دوباره)."""
    command = request.get("command") or ""
    text = request.get("text") or command
    callback = _HANDLERS.get(two_factor.normalize(command))

    if callback is not None:
        try:
            result = callback(bot, event, chat_id, user_id, text)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as error:                    # noqa: BLE001
            two_factor.log_error(
                logger, "TWO FACTOR CONFIRMED RUN FAILED "
                        f"chat_id={chat_id} user_id={user_id} "
                        f"command={command!r} error={error!r}")
            await _send(bot, chat_id, texts.two_factor_message("failed"),
                        logger)
            return False
        await _send(bot, chat_id, texts.two_factor_message("executed"),
                    logger)
        log_info(logger, "TWO FACTOR CONFIRMED RUN OK "
                         f"chat_id={chat_id} user_id={user_id} "
                         f"command={command!r}")
        return True

    # مسیرِ پیش‌فرض (وقتی دستوری هنوز هندلرِ ثبت‌شده ندارد): اجرای تأییدشده از
    # همان روترِ اصلی خواسته می‌شود — با پرچمِ تأییدشده تا گیت دوباره فعال
    # نشود. نتیجه هم صادقانه گزارش می‌شود.
    try:
        from handlers import message_handler as _handler
    except Exception as error:                        # noqa: BLE001
        two_factor.log_error(
            logger, "TWO FACTOR ROUTER IMPORT FAILED error=" f"{error!r}")
        await _send(bot, chat_id, texts.two_factor_message("failed"), logger)
        return False

    runner = getattr(_handler, "run_confirmed_command", None)
    if runner is None:
        await _send(bot, chat_id, texts.two_factor_message("failed"), logger)
        return False
    try:
        result = runner(bot, event, text, two_factor_confirmed=True)
        if asyncio.iscoroutine(result):
            result = await result
    except Exception as error:                        # noqa: BLE001
        two_factor.log_error(
            logger, "TWO FACTOR CONFIRMED ROUTE FAILED "
                    f"chat_id={chat_id} command={command!r} error={error!r}")
        await _send(bot, chat_id, texts.two_factor_message("failed"), logger)
        return False
    if result is False:
        await _send(bot, chat_id, texts.two_factor_message("failed"), logger)
        log_info(logger, "TWO FACTOR CONFIRMED ROUTE DECLINED "
                         f"chat_id={chat_id} command={command!r}")
        return False
    await _send(bot, chat_id, texts.two_factor_message("executed"), logger)
    log_info(logger, "TWO FACTOR CONFIRMED ROUTE OK "
                     f"chat_id={chat_id} user_id={user_id} "
                     f"command={command!r}")
    return True


# ---------------------------------------------------------------------------
#  ۴) وضعیت (برای پنل/راهنما) و ری‌ست
# ---------------------------------------------------------------------------
def status(chat_id):
    enabled, entry = two_factor.status(chat_id)
    return {
        "enabled": enabled,
        "set_by": (entry or {}).get("set_by"),
        "created_at": (entry or {}).get("created_at"),
        "lockout": two_factor.lockout_remaining(chat_id),
        "requests": 1 if two_factor.pending_request(chat_id) else 0,
    }


def reset_all():
    two_factor.reset_all()
    _HANDLERS.clear()
    _NOT_OWNER_NOTICE.clear()
