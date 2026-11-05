# -*- coding: utf-8 -*-
"""🛟 فراخوانی‌های گروهیِ مقاوم — یک کاربر/آی‌دیِ حذف‌شده کل batch را نمی‌کشد.

خطِ لاگِ مشاهده‌شده::

    GetUsersRequest → RPCError 404: NOT_FOUND (user id=7704762870259379992)
    AUTO CLEANUP SNAPSHOT RETRY ... error=NotFoundError

سروش وقتی **یک** شناسهٔ حذف‌شده در لیست باشد، کلِ درخواست را ۴۰۴ می‌کند.
این ماژول دو قاعده دارد:

1. شناسه‌های نامعتبر (غیرعدد، صفر، منفی) از پیش حذف می‌شوند.
2. اگر batch شکست خورد، همان لیست **تک‌تک** دوباره خوانده می‌شود و فقط
   شناسه‌های واقعاً موجود برمی‌گردند (نه خطا).

هیچ RPC اضافه‌ای در حالتِ سالم زده نمی‌شود: اول batch، و فقط در شکست
fallbackِ تک‌به‌تک.
"""
from __future__ import annotations

DEFAULT_CHUNK = 50


def _safe_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def clean_ids(user_ids):
    """حذفِ شناسه‌های نامعتبر + یکتا کردن، با حفظ ترتیب."""
    cleaned = []
    seen = set()
    for value in user_ids or ():
        number = _safe_int(value)
        if number is None or number in seen:
            continue
        seen.add(number)
        cleaned.append(number)
    return cleaned


def _is_not_found(error) -> bool:
    name = type(error).__name__.lower()
    text = str(error).lower()
    return (
        "notfound" in name
        or "not_found" in text
        or "404" in text
        or "no user has" in text
    )


async def get_users_safe(client, user_ids, *, chunk=DEFAULT_CHUNK, logger=None,
                         label="GET USERS"):
    """گرفتنِ user با تحملِ شناسه‌های حذف‌شده.

    خروجی: لیستِ ``User``هایی که واقعاً برگشتند (بدون خطا).
    """
    ids = clean_ids(user_ids)
    if not ids:
        return []
    getter = getattr(client, "get_users", None)
    if not callable(getter):
        return []

    users = []
    chunk = max(1, int(chunk or DEFAULT_CHUNK))
    for start in range(0, len(ids), chunk):
        batch = ids[start:start + chunk]
        try:
            result = await getter(batch)
        except Exception as error:
            if logger is not None:
                try:
                    logger.log_info(
                        f"{label} BATCH FAILED size={len(batch)} "
                        f"error={type(error).__name__} action=per_id_retry"
                    )
                except Exception:
                    pass
            result = await _fetch_individually(getter, batch, logger, label)
        for user in result or ():
            if user is not None:
                users.append(user)
    return users


async def _fetch_individually(getter, batch, logger, label):
    """fallbackِ تک‌به‌تک؛ شناسه‌های حذف‌شده فقط رد می‌شوند."""
    found = []
    for user_id in batch:
        try:
            result = await getter([user_id])
        except Exception as error:
            if _is_not_found(error):
                if logger is not None:
                    try:
                        logger.log_info(
                            f"{label} SKIP DELETED USER id={user_id} "
                            "reason=not_found"
                        )
                    except Exception:
                        pass
                continue
            if logger is not None:
                try:
                    logger.log_error(
                        f"{label} PER-ID FAILED id={user_id} "
                        f"error={type(error).__name__}: {error}"
                    )
                except Exception:
                    pass
            continue
        for user in result or ():
            if user is not None:
                found.append(user)
    return found
