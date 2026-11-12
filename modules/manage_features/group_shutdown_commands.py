# -*- coding: utf-8 -*-
"""🌖 هندلر دستورات «خاموشی گروه» — ویژهٔ مالک ثبت‌شدهٔ اصلی ربات."""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Tuple

from modules.group_id import normalize_group_id
from modules.manage_features import group_shutdown, texts

#: مهلت انتظار برای ورود زمان توسط مالک پس از ارسال «خاموشی گروه» (ثانیه).
AWAITING_TTL_SECONDS = 300.0

_AWAITING_LOCK = threading.RLock()
#: (group_key, owner_id_str) -> expire_monotonic
_AWAITING_INPUT: Dict[Tuple[str, str], float] = {}

#: دستورات صریحی که اگر مالک در حین وضعیت انتظار بفرستد، نباید به عنوان ورودی زمان بلعیده شوند.
_PASSTHROUGH_OWNER_COMMANDS = frozenset({
    "قفل",
    "باز",
    "لیست ادمینی",
    "لیست ادمین",
    "راهنما",
    "help",
    "وضعیت ربات",
    "پینگ ربات",
    "آمار گروه",
    "آمار گپ",
    "نمودار",
    "پاکسازی کامل",
    "پیام زماندار",
    "لغو پیام زماندار",
})


def _await_key(chat_id, user_id) -> Tuple[str, str]:
    return normalize_group_id(chat_id), str(int(user_id))


def set_awaiting(chat_id, user_id, *, ttl: float = AWAITING_TTL_SECONDS) -> None:
    if chat_id is None or user_id is None:
        return
    with _AWAITING_LOCK:
        _AWAITING_INPUT[_await_key(chat_id, user_id)] = time.monotonic() + float(ttl)


def clear_awaiting(chat_id, user_id) -> None:
    if chat_id is None or user_id is None:
        return
    with _AWAITING_LOCK:
        try:
            _AWAITING_INPUT.pop(_await_key(chat_id, user_id), None)
        except (TypeError, ValueError):
            pass


def clear_all_awaiting() -> None:
    """پاک‌سازی کامل وضعیت‌های انتظار (برای تست‌ها)."""
    with _AWAITING_LOCK:
        _AWAITING_INPUT.clear()


def is_awaiting(chat_id, user_id) -> bool:
    """بررسی سریع O(1) بدون I/O: آیا مالک در این گروه منتظر وارد کردن زمان است؟"""
    if chat_id is None or user_id is None:
        return False
    try:
        key = _await_key(chat_id, user_id)
    except (TypeError, ValueError):
        return False
    now_m = time.monotonic()
    with _AWAITING_LOCK:
        exp = _AWAITING_INPUT.get(key)
        if exp is None:
            return False
        if now_m > exp:
            _AWAITING_INPUT.pop(key, None)
            return False
        return True


def _parse_command_kind(text: str) -> Tuple[Optional[str], str]:
    norm = group_shutdown.normalize_text(text)
    if not norm:
        return None, ""
    if norm == "خاموشی گروه":
        return "start", ""
    if norm.startswith("خاموشی گروه "):
        return "inline", norm[len("خاموشی گروه "):].strip()
    if norm in ("لغو خاموشی گروه", "لغو خاموشی"):
        return "cancel_schedule", ""
    return None, norm


def is_command(text: str, chat_id=None, user_id=None) -> bool:
    """تشخیص سریع دستور «خاموشی گروه» یا مرحلهٔ دریافت زمان از مالک ثبت‌شده."""
    kind, norm = _parse_command_kind(text)
    if kind is not None:
        return True
    if not norm or chat_id is None or user_id is None:
        return False
    if not is_awaiting(chat_id, user_id):
        return False
    if norm in _PASSTHROUGH_OWNER_COMMANDS or norm.startswith(("محدودیت پیام", "لغو محدودیت")):
        return False
    return True


def _remember_event_peer(bot, event, chat_id, chat=None) -> None:
    """ذخیرهٔ InputPeer گروه در کش ربات تا اجرای آیندهٔ زمان‌بند بدون خطای resolve باشد."""
    cache = getattr(bot, "reply_input_peer_cache", None)
    if not isinstance(cache, dict):
        return
    candidate = getattr(event, "input_chat", None)
    if candidate is not None and not callable(candidate):
        cache[chat_id] = candidate
        return
    chat_obj = chat or getattr(event, "chat", None)
    cid = getattr(chat_obj, "id", None)
    ahash = getattr(chat_obj, "access_hash", None)
    if isinstance(cid, int) and isinstance(ahash, int):
        try:
            from splusthon.tl import types

            cache[chat_id] = types.InputPeerChannel(channel_id=cid, access_hash=ahash)
        except Exception:  # noqa: BLE001
            pass


async def _reply_formatted(event, text: str, entities: list) -> None:
    await event.reply(
        text,
        formatting_entities=list(entities) if entities else None,
    )


async def _reply_optional(event, kind: str, **context) -> None:
    tpl = texts.group_shutdown_optional_template(kind, **context)
    if not tpl:
        return
    if isinstance(tpl, tuple) and len(tpl) == 2:
        await _reply_formatted(event, tpl[0], tpl[1])
    else:
        await _reply_formatted(event, str(tpl), [])


async def handle(
    bot,
    event,
    chat_id,
    user_id,
    sender,
    raw_text: str,
    logger=None,
    *,
    chat=None,
) -> bool:
    """مدیریت دستور «خاموشی گروه» و دریافت مدت زمان از مالک اصلی ثبت‌شدهٔ ربات."""
    if getattr(event, "is_private", False):
        return False

    log = logger or getattr(bot, "logger", None)
    kind, arg = _parse_command_kind(raw_text)

    # ۱) بررسی سخت‌گیرانهٔ سطح دسترسی: فقط و فقط مالک ثبت‌شدهٔ اصلی ربات
    if kind is not None:
        if not group_shutdown.is_authorized_owner(user_id):
            if log and hasattr(log, "log_info"):
                log.log_info(
                    f"[GROUP_SHUTDOWN] رد دستور غیرمجاز chat={chat_id} user={user_id} cmd={kind}"
                )
            await _reply_optional(event, "unauthorized", chat_id=chat_id, user_id=user_id)
            return True

        _remember_event_peer(bot, event, chat_id, chat=chat)

        if kind == "start":
            set_awaiting(chat_id, user_id)
            prompt_text, prompt_entities = texts.group_shutdown_prompt()
            await _reply_formatted(event, prompt_text, prompt_entities)
            return True

        if kind == "cancel_schedule":
            clear_awaiting(chat_id, user_id)
            cancelled = group_shutdown.cancel_shutdown(
                chat_id, owner_id=user_id, logger=log
            )
            await _reply_optional(
                event, "cancelled", chat_id=chat_id, cancelled=cancelled
            )
            return True

        if kind == "inline":
            clear_awaiting(chat_id, user_id)
            return await _schedule_from_input(
                bot, event, chat_id, user_id, arg, log, keep_awaiting_on_error=False
            )

    # ۲) مرحلهٔ دریافت زمان در وضعیت انتظار
    if not is_awaiting(chat_id, user_id):
        return False

    # بررسی مجدد مالکیت در لحظهٔ دریافت زمان (ایمنی مضاعف در برابر تغییر فایل owner)
    if not group_shutdown.is_authorized_owner(user_id):
        clear_awaiting(chat_id, user_id)
        return False

    if arg in _PASSTHROUGH_OWNER_COMMANDS or arg.startswith(("محدودیت پیام", "لغو محدودیت")):
        clear_awaiting(chat_id, user_id)
        return False

    if arg in ("لغو", "انصراف"):
        clear_awaiting(chat_id, user_id)
        await _reply_optional(event, "cancelled", chat_id=chat_id, cancelled=False)
        return True

    _remember_event_peer(bot, event, chat_id, chat=chat)
    return await _schedule_from_input(
        bot, event, chat_id, user_id, raw_text, log, keep_awaiting_on_error=True
    )


async def _schedule_from_input(
    bot,
    event,
    chat_id,
    user_id,
    duration_text: str,
    logger=None,
    *,
    keep_awaiting_on_error: bool = True,
) -> bool:
    try:
        secs = group_shutdown.parse_duration_seconds(duration_text)
    except group_shutdown.DurationTooLongError as exc:
        if keep_awaiting_on_error:
            set_awaiting(chat_id, user_id)
        if logger and hasattr(logger, "log_info"):
            logger.log_info(
                f"[GROUP_SHUTDOWN] رد زمان بیش از سقف chat={chat_id} user={user_id} "
                f"seconds={exc.seconds} max={exc.maximum}"
            )
        tpl = texts.group_shutdown_optional_template(
            "duration_too_long",
            chat_id=chat_id,
            seconds=exc.seconds,
            maximum=exc.maximum,
        )
        if tpl is not None:
            await _reply_optional(
                event,
                "duration_too_long",
                chat_id=chat_id,
                seconds=exc.seconds,
                maximum=exc.maximum,
            )
        else:
            await _reply_optional(
                event, "invalid_input", chat_id=chat_id, reason="duration_too_long"
            )
        return True
    except group_shutdown.InvalidDurationError as exc:
        if keep_awaiting_on_error:
            set_awaiting(chat_id, user_id)
        if logger and hasattr(logger, "log_info"):
            logger.log_info(
                f"[GROUP_SHUTDOWN] ورودی زمان نامعتبر chat={chat_id} user={user_id} "
                f"reason={exc.reason} input={duration_text!r}"
            )
        await _reply_optional(
            event, "invalid_input", chat_id=chat_id, reason=exc.reason
        )
        return True

    clear_awaiting(chat_id, user_id)
    await group_shutdown.schedule_shutdown(
        bot,
        chat_id,
        user_id,
        duration_text,
        duration_seconds=secs,
        logger=logger,
    )
    ok_text, ok_entities = texts.group_shutdown_scheduled()
    await _reply_formatted(event, ok_text, ok_entities)
    return True


def recover(bot, logger=None) -> dict:
    """نقطهٔ ورود بازیابی استارتاپ (هماهنگ با سایر ماژول‌های manage_features)."""
    return group_shutdown.recover(bot, logger=logger)
