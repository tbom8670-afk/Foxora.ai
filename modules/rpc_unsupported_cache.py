# -*- coding: utf-8 -*-
"""🚫 کشِ منفیِ سراسریِ ``NOT_SUPPORTED`` (به‌تفکیکِ کانال).

سروش‌پلاس برای بعضی متدهای TL Schema خطای ``RPCError 422: NOT_SUPPORTED``
می‌دهد؛ مثلاً ``channels.getParticipant``. تا امروز این دانش فقط داخلِ
``force_join_service`` (و آن هم فقط برای کانالِ عضویتِ اجباری) کش می‌شد، ولی
همان درخواست از مسیرهای دیگری (``bio_link_service.fetch_native_role``،
``core.is_currently_restricted``، ``manage_features/exclusive``) هم صدا زده
می‌شد و هر کدام یک RPCِ محکوم‌به‌شکست می‌فرستادند.

این ماژول یک منبعِ حقیقتِ مشترک است:

* کلید = ``(نامِ درخواست, شناسهٔ کانال)``.
* بعد از اولین ``NOT_SUPPORTED``، تا ``RPC_NOT_SUPPORTED_COOLDOWN`` ثانیه
  (پیش‌فرض ۳۶۰۰ = همان cooldownِ یک‌ساعتهٔ force-join) درخواست **از شبکه
  فرستاده نمی‌شود**؛ همان خطای «پشتیبانی نمی‌شود» به‌صورتِ محلی بالا می‌آید.
* خطای محلی از نظرِ **نوع و متن** با خطای واقعیِ سرور یکی است
  (``RpcError 422: NOT_SUPPORTED (cached)``) تا همهٔ callerها و
  ``force_join_api.classify_error`` دقیقاً مثل قبل رفتار کنند.
* چون درخواستی ساخته نمی‌شود، بعد از reconnect هم هیچ resendی ندارد.
* با ``RPC_NOT_SUPPORTED_CACHE_ENABLED=0`` کلِ این لایه خاموش است.
"""
from __future__ import annotations

import os
import time

#: درخواست‌هایی که کشِ منفی برایشان اعمال می‌شود (قابلِ توسعه با env).
DEFAULT_REQUESTS = frozenset({"GetParticipantRequest"})

#: (request_name, chat_key) → زمانِ انقضا (monotonic)
_UNTIL: dict = {}
#: شمارنده‌ها (فقط برای گزارش/تست)
COUNTERS = {"recorded": 0, "skipped": 0, "expired": 0}


class NotSupportedCached(Exception):
    """«سرور این متد را ندارد» — از کش، بدونِ هیچ درخواستِ شبکه‌ای."""

    def __init__(self, request_name, chat_key):
        self.request_name = request_name
        self.chat_key = chat_key
        super().__init__(
            f"RPCError 422: NOT_SUPPORTED (cached, request={request_name})"
        )


def _env_flag(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


#: کشِ خواندنِ env تا مسیرِ داغ فقط یک مقایسهٔ رشته داشته باشد (نه getenv).
_ENV_CACHE = {"raw": None, "enabled": True, "list_raw": None, "names": None}


def _read_enabled() -> bool:
    raw = os.getenv("RPC_NOT_SUPPORTED_CACHE_ENABLED", "1")
    if _ENV_CACHE["raw"] != raw:
        _ENV_CACHE["raw"] = raw
        _ENV_CACHE["enabled"] = str(raw).strip().lower() not in (
            "", "0", "false", "no", "off")
    return bool(_ENV_CACHE["enabled"])


def enabled() -> bool:
    return _read_enabled()


def cooldown_seconds() -> float:
    try:
        value = float(str(os.getenv("RPC_NOT_SUPPORTED_COOLDOWN", "3600")).strip())
    except (TypeError, ValueError):
        return 3600.0
    return max(1.0, min(7 * 24 * 3600.0, value))


def cached_requests() -> frozenset:
    extra = os.getenv("RPC_NOT_SUPPORTED_CACHE_REQUESTS", "").strip()
    if _ENV_CACHE["list_raw"] != extra:
        names = set(DEFAULT_REQUESTS)
        if extra:
            names.update(part.strip() for part in extra.split(",") if part.strip())
        _ENV_CACHE["list_raw"] = extra
        _ENV_CACHE["names"] = frozenset(names)
    return _ENV_CACHE["names"]


def _key(request_name, chat_key):
    return (str(request_name or ""), str(chat_key))


def is_cached(request_name, chat_key) -> bool:
    """آیا برای این (درخواست، کانال) «پشتیبانی نمی‌شود» کش شده است؟"""
    if not enabled() or not request_name or request_name not in cached_requests():
        return False
    until = _UNTIL.get(_key(request_name, chat_key))
    if until is None:
        return False
    if time.monotonic() >= until:
        _UNTIL.pop(_key(request_name, chat_key), None)
        COUNTERS["expired"] += 1
        return False
    return True


def note(request_name, chat_key) -> bool:
    """ثبتِ «پشتیبانی نمی‌شود» برای cooldown. خروجی: آیا ثبت شد؟"""
    if not enabled() or not request_name or request_name not in cached_requests():
        return False
    _UNTIL[_key(request_name, chat_key)] = time.monotonic() + cooldown_seconds()
    COUNTERS["recorded"] += 1
    return True


def is_not_supported_error(error) -> bool:
    """آیا این خطا «متد پشتیبانی نمی‌شود» است؟"""
    if error is None:
        return False
    if isinstance(error, NotSupportedCached):
        return True
    name = type(error).__name__.lower().replace("_", "")
    if "notsupported" in name or "unimplemented" in name:
        return True
    text = str(error).lower()
    return "not_supported" in text or "not supported" in text


def note_if_unsupported(request_name, chat_key, error) -> bool:
    """اگر خطا NOT_SUPPORTED بود، ثبتش کن (برای فراخوانی در مسیرِ خطا)."""
    if not is_not_supported_error(error):
        return False
    return note(request_name, chat_key)


def error_for(request_name, chat_key) -> NotSupportedCached:
    COUNTERS["skipped"] += 1
    return NotSupportedCached(request_name, chat_key)


def reset() -> None:
    """صفرکردنِ کامل (تست/ری‌استارت سوپروایزر)."""
    _UNTIL.clear()
    for key in COUNTERS:
        COUNTERS[key] = 0


def snapshot() -> dict:
    """وضعیتِ فعلی برای گزارش/تست."""
    now = time.monotonic()
    rows = {
        f"{name}|{chat}": round(until - now, 1)
        for (name, chat), until in _UNTIL.items() if until > now
    }
    return {"entries": rows, "counters": dict(COUNTERS)}
