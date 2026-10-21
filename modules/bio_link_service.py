"""🖇 سرویس «تشخیص لینک در بیوگرافی» — لایهٔ واقعی API + پایش اعضا.

این ماژول تنها جایی است که با SPlusthon برای خواندن بیوگرافی صحبت می‌کند.

مسیر واقعی API (تأییدشده روی نسخهٔ نصب‌شدهٔ کتابخانه):
    ``users.getFullUser#b60f5918`` → پاسخ ``users.UserFull`` با فیلد
    ``full_user`` از نوع ``UserFull`` که فیلد ``about`` همان بیوگرافی است.

    >>> from splusthon.tl.functions.users import GetFullUserRequest
    >>> full = await client(GetFullUserRequest(input_user))
    >>> about = full.full_user.about

برای گرفتن ``InputUser`` از سبک‌ترین راه استفاده می‌شود:
``splusthon.utils.get_input_user(user)`` که وقتی آبجکت ``User`` (دارای
``access_hash``) در دست است هیچ RPC اضافه‌ای لازم ندارد.

محدودیت‌های واقعی که این ماژول صریح مدیریت می‌کند:
  * بیوی کاربران دیگر تنها وقتی خوانده می‌شود که سرویس سروش اجازه دهد؛ در
    غیر این صورت ``status`` مقدار ``no_access`` می‌گیرد و کاربر مجازات
    نمی‌شود (نبودِ اطلاعات ≠ تخلف).
  * ``channels.getParticipants`` سقف دارد و برای گروه‌های بزرگ صفحه‌بندی
    لازم است؛ بنابراین اسکن با ``limit`` و بودجهٔ زمانی اجرا می‌شود و بین
    درخواست‌ها فاصله می‌گذارد (``BIO_LINK_RPC_DELAY``).
  * FloodWait: به‌جای خوابیدن در مسیر گرم، اسکن همان گروه متوقف و در دور
    بعدی ادامه داده می‌شود.

هیچ متن بیوگرافی، توکن، شماره یا اطلاعات نشست لاگ نمی‌شود.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Optional, Tuple

from modules import bio_link, bio_link_storage

# ---------------------------------------------------------------------------
# تنظیمات از محیط (بدون تغییر در فایل‌های حساس)
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_NO_ACCESS = "no_access"
STATUS_ERROR = "error"
STATUS_FLOOD = "flood_wait"


def _env_int(name: str, default: int, minimum: int = 0, maximum: int = 10_000_000) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 3600.0) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def scan_limit() -> int:
    return _env_int("BIO_LINK_SCAN_LIMIT", 200, 1, 5000)


def scan_interval() -> int:
    return _env_int("BIO_LINK_SCAN_INTERVAL", 6 * 60 * 60, 300, 7 * 24 * 60 * 60)


def scan_budget() -> float:
    return _env_float("BIO_LINK_SCAN_BUDGET", 120.0, 5.0, 3600.0)


def rpc_delay() -> float:
    return _env_float("BIO_LINK_RPC_DELAY", 0.4, 0.0, 30.0)


def fetch_timeout() -> float:
    return _env_float("BIO_LINK_FETCH_TIMEOUT", 20.0, 2.0, 120.0)


def check_on_message() -> bool:
    return _env_int("BIO_LINK_CHECK_ON_MESSAGE", 1, 0, 1) == 1


def check_on_join() -> bool:
    return _env_int("BIO_LINK_CHECK_ON_JOIN", 1, 0, 1) == 1


def await_first_check() -> bool:
    """آیا اولین پیام هر کاربر منتظر نتیجهٔ بررسی بیو بماند؟ (پیش‌فرض: بله)"""
    return _env_int("BIO_LINK_AWAIT_FIRST_CHECK", 1, 0, 1) == 1


def await_timeout() -> float:
    """سقف انتظار برای بررسی بیو هنگام اولین پیام (ثانیه)."""
    return _env_float("BIO_LINK_AWAIT_TIMEOUT", 8.0, 1.0, 30.0)


def auto_scan_members() -> bool:
    """اسکن دوره‌ای «همهٔ اعضا» — پیش‌فرض خاموش.

    سیاست پیش‌فرض: فقط کاربرانی که در گروه پیام می‌دهند بررسی می‌شوند.
    اسکن کامل اعضا فقط با دستور «بررسی بیو» یا با روشن کردن این گزینه.
    """
    return _env_int("BIO_LINK_AUTO_SCAN_MEMBERS", 0, 0, 1) == 1


def skip_native_admins() -> bool:
    """ادمین/مالکِ خودِ گروه (نقش سروش) معاف باشد؟ (پیش‌فرض: بله)"""
    return _env_int("BIO_LINK_SKIP_NATIVE_ADMINS", 1, 0, 1) == 1


def start_delay() -> float:
    return _env_float("BIO_LINK_START_DELAY", 120.0, 0.0, 3600.0)


MAX_PENDING = _env_int("BIO_LINK_MAX_PENDING", 400, 10, 5000)

_pending: dict = {}
_inflight: set = set()
# کاربرانی که سرویس برایشان خطا/محدودیت داده است، تا مدتی بررسی نمی‌شوند تا
# پیام‌های پیاپی همان کاربر یک RPC در هر پیام نسازند.
_retry_after: dict = {}
RETRY_AFTER_ERROR_SECONDS = _env_int("BIO_LINK_ERROR_RETRY_SECONDS", 1800, 60, 86400)
_semaphore: Optional[asyncio.Semaphore] = None
_group_locks: dict = {}
_last_flood_until = 0.0


def _loop_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


def _log(bot, message: str, *, error: bool = False):
    """لاگ ایمن: فقط شناسهٔ عددی و وضعیت؛ هرگز متن بیو یا اطلاعات نشست."""
    logger = getattr(bot, "logger", None)
    try:
        if error:
            logger.log_error(f"BIO LINK {message}")
        else:
            logger.log_info(f"BIO LINK {message}")
    except Exception:
        pass


def _display_name(user) -> Optional[str]:
    """نام نمایشی عمومی کاربر برای گزارش؛ بدون RPC و بدون اطلاعات حساس."""
    if user is None:
        return None
    username = getattr(user, "username", None)
    if username:
        return f"@{str(username).lstrip('@')}"
    first = (getattr(user, "first_name", None) or "").strip()
    last = (getattr(user, "last_name", None) or "").strip()
    full = (first + " " + last).strip()
    return full or None


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# لایهٔ API
# ---------------------------------------------------------------------------

def _classify_error(error: Exception) -> str:
    name = type(error).__name__.lower()
    text = str(error).lower()
    if "flood" in name or "floodwait" in text or "too many requests" in text:
        return STATUS_FLOOD
    if "privacy" in name or "privacy" in text or "not enough rights" in text:
        return STATUS_NO_ACCESS
    if "deactivated" in text or "usernotparticipant" in name:
        return STATUS_NO_ACCESS
    return STATUS_ERROR


async def fetch_about(bot, user) -> Tuple[str, Optional[str]]:
    """خواندن بیوگرافی یک کاربر از سرویس.

    خروجی ``(status, about)``؛ ``status`` یکی از ``ok`` ، ``empty`` ،
    ``no_access`` ، ``flood_wait`` ، ``error`` است.
    """
    client = getattr(bot, "client", None)
    if client is None:
        return STATUS_ERROR, None

    try:
        from splusthon.tl.functions.users import GetFullUserRequest
    except Exception:  # pragma: no cover - کتابخانه در محیط تست نصب نیست
        return STATUS_ERROR, None

    request_input = None
    try:
        from splusthon import utils as splus_utils
        request_input = splus_utils.get_input_user(user)
    except Exception:
        request_input = None

    if request_input is None:
        try:
            request_input = await asyncio.wait_for(
                client.get_input_entity(user), timeout=fetch_timeout()
            )
        except asyncio.TimeoutError:
            return STATUS_ERROR, None
        except Exception as error:
            return _classify_error(error), None

    try:
        full = await asyncio.wait_for(
            client(GetFullUserRequest(request_input)), timeout=fetch_timeout()
        )
    except asyncio.TimeoutError:
        return STATUS_ERROR, None
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return _classify_error(error), None

    full_user = getattr(full, "full_user", None)
    if full_user is None:
        return STATUS_NO_ACCESS, None
    about = getattr(full_user, "about", None)
    if about is None or not str(about).strip():
        return STATUS_EMPTY, None
    return STATUS_OK, str(about)


# ---------------------------------------------------------------------------
# معافیت‌ها: مالک ربات، مالک گروه، ادمین ثبت‌شده، ادمین واقعی گروه، VIP
# ---------------------------------------------------------------------------

ADMIN_ROLE_NAMES = ("creator", "admin", "owner")


def local_exemption(chat_id, user_id, username=None) -> Optional[str]:
    """معافیت‌های ارزان و محلی (بدون RPC): مالک/ادمین ثبت‌شده/VIP/مالک ربات."""
    try:
        from modules.admin_tools import has_admin_permission
        if has_admin_permission(chat_id, user_id, username):
            return "admin"
    except Exception:
        pass
    try:
        from modules import vip_storage
        if vip_storage.is_vip(chat_id, user_id):
            return "vip"
    except Exception:
        pass
    return None


def _participant_role_name(participant) -> Optional[str]:
    name = type(participant).__name__.lower()
    for role in ADMIN_ROLE_NAMES:
        if role in name:
            return role
    return None


async def fetch_native_role(bot, chat_id, user) -> Optional[str]:
    """نقش واقعی کاربر در گروه سروش را می‌خواند؛ ``None`` یعنی عضو عادی/نامعلوم.

    فقط یک‌بار در هر TTL برای هر کاربر صدا زده می‌شود (نه در مسیر هر پیام).
    """
    client = getattr(bot, "client", None)
    if client is None or not skip_native_admins():
        return None
    # سریع‌ترین راه: نقش همراه رویداد آمده باشد.
    role = _participant_role_name(getattr(user, "participant", None))
    if role:
        return role
    try:
        from splusthon.tl.functions.channels import GetParticipantRequest
        from splusthon import utils as splus_utils
    except Exception:  # pragma: no cover
        return None
    try:
        channel = None
        try:
            channel = getattr(bot, "reply_input_peer_cache", {}).get(chat_id)
        except Exception:
            channel = None
        if channel is None:
            channel = await asyncio.wait_for(
                client.get_input_entity(chat_id), timeout=fetch_timeout()
            )
        participant_input = await asyncio.wait_for(
            client.get_input_entity(user), timeout=fetch_timeout()
        )
        result = await asyncio.wait_for(
            client(
                GetParticipantRequest(
                    channel=channel, participant=participant_input
                )
            ),
            timeout=fetch_timeout(),
        )
        return _participant_role_name(getattr(result, "participant", None))
    except asyncio.CancelledError:
        raise
    except Exception:
        # نبود دسترسی/خطا یعنی «نامعلوم»؛ در این حالت کاربر عادی فرض می‌شود
        # اما هرگز به‌خاطر ندانستن نقش، اخطار اضافه نمی‌گیرد.
        return None


# ---------------------------------------------------------------------------
# مسیر گرم: خواندن سریع و بدون RPC
# ---------------------------------------------------------------------------

def link_state(chat_id, user_id) -> Tuple[bool, Optional[str]]:
    """``(لینک دارد؟، برچسب)`` از حافظه/دیسک — بدون هیچ RPC."""
    if not user_id:
        return False, None
    try:
        record = bio_link_storage.get_user(chat_id, user_id)
    except Exception:
        return False, None
    if not isinstance(record, dict):
        return False, None
    if record.get("exempt"):
        return False, None
    if record.get("link"):
        return True, record.get("label")
    return False, None


def needs_check(chat_id, user_id) -> bool:
    """آیا وضعیت این کاربر قدیمی/ناموجود است و باید تازه شود."""
    record = bio_link_storage.get_user(chat_id, user_id)
    if not isinstance(record, dict):
        return True
    checked_at = float(record.get("checked_at") or 0)
    if (time.time() - checked_at) >= bio_link_storage.ttl_seconds():
        return True
    # خطای API باید دیرتر ولی دوباره بررسی شود (نه هر پیام).
    return record.get("status") in {STATUS_ERROR, STATUS_FLOOD}


# ---------------------------------------------------------------------------
# بررسی یک کاربر
# ---------------------------------------------------------------------------

async def check_user(bot, chat_id, user, *, force: bool = False) -> Optional[dict]:
    """بیوگرافی یک کاربر را می‌خواند و نتیجه را پایدار ذخیره می‌کند."""
    user_id = _safe_int(getattr(user, "id", user if isinstance(user, int) else 0))
    if not user_id:
        return None
    if not bio_link_storage.is_enabled(chat_id):
        return None
    if not force and not needs_check(chat_id, user_id):
        return bio_link_storage.get_user(chat_id, user_id)

    display_name = _display_name(user)

    # معافیت محلی (مالک/ادمین ثبت‌شده/VIP) — بدون هیچ RPC.
    exempt = local_exemption(chat_id, user_id, getattr(user, "username", None))
    if exempt:
        bio_link_storage.set_user(
            chat_id, user_id, link=False, label=None, status=STATUS_OK,
            bio_len=0, name=display_name, exempt=exempt,
        )
        return bio_link_storage.get_user(chat_id, user_id)

    status, about = await fetch_about(bot, user)
    label = None
    if status == STATUS_OK:
        has, label = bio_link.analyze(
            about, detect_username=bio_link_storage.detect_username(chat_id)
        )
    elif status == STATUS_EMPTY:
        has, label = False, None
    else:
        # نبود دسترسی/خطا: هرگز به‌عنوان تخلف ثبت نمی‌شود.
        bio_link_storage.set_user(
            chat_id, user_id,
            link=False, label=None, status=status, bio_len=0,
            name=display_name,
        )
        if status in {STATUS_ERROR, STATUS_FLOOD, STATUS_NO_ACCESS}:
            _retry_after[(str(chat_id), str(user_id))] = (
                time.time() + RETRY_AFTER_ERROR_SECONDS
            )
        _log(bot, f"CHECK user_id={user_id} chat_id={chat_id} status={status}")
        return bio_link_storage.get_user(chat_id, user_id)

    # ادمین/مالک واقعی گروه: همان لحظه شناسایی و برای همیشه (تا انقضای TTL)
    # معاف ثبت می‌شود تا نه اخطار بگیرد و نه پیامی از او حذف شود.
    if has:
        role = await fetch_native_role(bot, chat_id, user)
        if role:
            bio_link_storage.set_user(
                chat_id, user_id, link=True, label=label, status=STATUS_OK,
                bio_len=len(about or ""), name=display_name, exempt=role,
            )
            _log(
                bot,
                f"CHECK user_id={user_id} chat_id={chat_id} link=True "
                f"exempt={role}",
            )
            return bio_link_storage.get_user(chat_id, user_id)

    record = bio_link_storage.set_user(
        chat_id, user_id,
        link=has,
        label=label,
        status=STATUS_OK,
        bio_len=len(about or ""),
        name=display_name,
    )
    _log(
        bot,
        "CHECK "
        f"user_id={user_id} chat_id={chat_id} status={STATUS_OK} "
        f"link={bool(has)} label={label!r} bio_len={len(about or '')}",
    )
    return record


async def ensure_user_now(bot, chat_id, user) -> Optional[dict]:
    """بیوی کاربر را همین حالا (در حد یک RPC) می‌خواند تا اولین پیام هم بررسی شود.

    اگر وضعیت تازه باشد، هیچ RPC انجام نمی‌شود. اگر سرویس کند/خطا بدهد،
    بررسی به پس‌زمینه منتقل می‌شود و پیام فعلی بدون تخلف پردازش می‌گردد.
    """
    if not check_on_message():
        return None
    user_id = _safe_int(getattr(user, "id", 0))
    if not user_id or user_id == _safe_int(getattr(bot, "bot_account_id", 0)):
        return None
    try:
        if not bio_link_storage.is_enabled(chat_id):
            return None
    except Exception:
        return None
    key = (str(chat_id), str(user_id))
    if time.time() < _retry_after.get(key, 0.0):
        return bio_link_storage.get_user(chat_id, user_id)
    if not needs_check(chat_id, user_id):
        return bio_link_storage.get_user(chat_id, user_id)
    if key in _inflight:
        return bio_link_storage.get_user(chat_id, user_id)
    _inflight.add(key)
    try:
        async with _loop_semaphore():
            return await asyncio.wait_for(
                check_user(bot, chat_id, user), timeout=await_timeout()
            )
    except asyncio.TimeoutError:
        _log(bot, f"CHECK TIMEOUT user_id={user_id} chat_id={chat_id}")
        schedule_check(bot, chat_id, user)
        return bio_link_storage.get_user(chat_id, user_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _log(
            bot,
            f"CHECK FAILED user_id={user_id} chat_id={chat_id} "
            f"error={type(error).__name__}",
            error=True,
        )
        return bio_link_storage.get_user(chat_id, user_id)
    finally:
        _inflight.discard(key)


def schedule_check(bot, chat_id, user):
    """بررسی بیو در پس‌زمینه — هرگز مسیر پیام را بلاک نمی‌کند."""
    if not check_on_message():
        return False
    user_id = _safe_int(getattr(user, "id", 0))
    if not user_id or user_id == _safe_int(getattr(bot, "bot_account_id", 0)):
        return False
    try:
        if not bio_link_storage.is_enabled(chat_id):
            return False
    except Exception:
        return False
    key = (str(chat_id), str(user_id))
    if time.time() < _retry_after.get(key, 0.0):
        return False
    if not needs_check(chat_id, user_id):
        return False
    if key in _inflight:
        return False
    if len(_pending) >= MAX_PENDING and key not in _pending:
        return False
    _pending[key] = (bot, chat_id, user, time.time())
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(_drain_pending(), name="bio-link-check")
    return True


async def _drain_pending():
    """صرف صف بررسی با هم‌زمانی ۱ و فاصلهٔ کنترل‌شده."""
    for key in list(_pending.keys()):
        if key in _inflight:
            _pending.pop(key, None)
            continue
        item = _pending.pop(key, None)
        if not item:
            continue
        bot, chat_id, user, _queued_at = item
        _inflight.add(key)
        try:
            async with _loop_semaphore():
                await check_user(bot, chat_id, user)
                delay = rpc_delay()
            if delay:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            _inflight.discard(key)
            raise
        except Exception as error:
            _log(bot, f"PENDING CHECK FAILED key={key} error={type(error).__name__}", error=True)
        finally:
            _inflight.discard(key)
        if not _pending:
            break


# ---------------------------------------------------------------------------
# اسکن اعضای گروه
# ---------------------------------------------------------------------------

def _participant_is_admin(user) -> bool:
    """ادمین/سازندهٔ گروه در نتیجهٔ getParticipants نباید اسکن شود."""
    participant = getattr(user, "participant", None)
    if participant is None:
        return False
    for attr in ("is_admin", "is_creator", "admin_rights"):
        try:
            value = getattr(participant, attr, None)
        except Exception:
            value = None
        if attr == "admin_rights":
            if value is not None:
                return True
        elif value:
            return True
    return False


def _group_lock(key: str) -> asyncio.Lock:
    lock = _group_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _group_locks[key] = lock
    return lock


async def _resolve_chat(bot, chat_id):
    client = getattr(bot, "client", None)
    if client is None:
        return None
    try:
        peer = getattr(bot, "reply_input_peer_cache", {}).get(chat_id)
        if peer is not None:
            return peer
    except Exception:
        pass
    key = _safe_int(chat_id)
    for candidate in (chat_id, key):
        try:
            return await asyncio.wait_for(
                client.get_input_entity(candidate), timeout=fetch_timeout()
            )
        except Exception:
            continue
    return None


async def scan_group(bot, chat_id, *, limit: Optional[int] = None,
                     logger=None, force: bool = False) -> dict:
    """بررسی بیوی اعضای یک گروه با سقف، بودجهٔ زمانی و فاصلهٔ RPC.

    نتیجهٔ اسکن پایدار ذخیره می‌شود تا دور بعد فقط وضعیت‌های کهنه را
    تازه کند (نه همه‌چیز را).
    """
    global _last_flood_until
    summary = {
        "chat_id": chat_id, "checked": 0, "found": 0,
        "skipped": 0, "failed": 0, "status": STATUS_OK,
    }
    if not bio_link_storage.is_enabled(chat_id):
        summary["status"] = "disabled"
        return summary

    client = getattr(bot, "client", None)
    if client is None or not hasattr(client, "iter_participants"):
        summary["status"] = STATUS_ERROR
        return summary

    if time.time() < _last_flood_until:
        summary["status"] = STATUS_FLOOD
        return summary

    lock = _group_lock(str(chat_id))
    if lock.locked():
        summary["status"] = "busy"
        return summary

    async with lock:
        entity = await _resolve_chat(bot, chat_id)
        if entity is None:
            summary["status"] = STATUS_NO_ACCESS
            bio_link_storage.mark_scan(chat_id, status=STATUS_NO_ACCESS)
            _log(bot, f"SCAN chat_id={chat_id} status={STATUS_NO_ACCESS}", error=True)
            return summary

        cap = int(limit or scan_limit())
        budget = scan_budget()
        started = time.monotonic()
        detect_username = bio_link_storage.detect_username(chat_id)

        try:
            async for user in client.iter_participants(entity, limit=cap):
                if user is None:
                    continue
                if time.monotonic() - started > budget:
                    summary["status"] = "partial"
                    break
                if getattr(user, "bot", False) or getattr(user, "is_self", False):
                    summary["skipped"] += 1
                    continue
                user_id = _safe_int(getattr(user, "id", 0))
                if not user_id or user_id == _safe_int(getattr(bot, "bot_account_id", 0)):
                    summary["skipped"] += 1
                    continue
                if _participant_is_admin(user):
                    summary["skipped"] += 1
                    continue
                if not force and not needs_check(chat_id, user_id):
                    summary["skipped"] += 1
                    continue

                display_name = _display_name(user)
                status, about = await fetch_about(bot, user)
                if status == STATUS_FLOOD:
                    # سرویس محدودیت گذاشته؛ اسکن بعدی دیرتر.
                    _last_flood_until = time.time() + 300
                    summary["status"] = STATUS_FLOOD
                    break
                if status in {STATUS_ERROR, STATUS_NO_ACCESS}:
                    summary["failed"] += 1
                    bio_link_storage.set_user(
                        chat_id, user_id, link=False, label=None,
                        status=status, bio_len=0, name=display_name,
                    )
                else:
                    has, label = (
                        bio_link.analyze(about, detect_username=detect_username)
                        if status == STATUS_OK else (False, None)
                    )
                    exempt = local_exemption(
                        chat_id, user_id, getattr(user, "username", None)
                    )
                    if not exempt and has:
                        exempt = _participant_role_name(
                            getattr(user, "participant", None)
                        )
                    bio_link_storage.set_user(
                        chat_id, user_id, link=has, label=label,
                        status=STATUS_OK, bio_len=len(about or ""),
                        name=display_name, exempt=exempt,
                    )
                    summary["checked"] += 1
                    if has:
                        summary["found"] += 1
                delay = rpc_delay()
                if delay:
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            bio_link_storage.flush()
            raise
        except Exception as error:
            summary["status"] = _classify_error(error)
            _log(
                bot,
                f"SCAN chat_id={chat_id} failed error={type(error).__name__}",
                error=True,
            )

        bio_link_storage.flush()
        bio_link_storage.mark_scan(
            chat_id,
            checked=summary["checked"],
            found=summary["found"],
            skipped=summary["skipped"],
            failed=summary["failed"],
            status=summary["status"],
        )
        _log(
            bot,
            "SCAN DONE "
            f"chat_id={chat_id} checked={summary['checked']} "
            f"found={summary['found']} skipped={summary['skipped']} "
            f"failed={summary['failed']} status={summary['status']}",
        )
        return summary


# ---------------------------------------------------------------------------
# ناظر دوره‌ای
# ---------------------------------------------------------------------------

def active_groups():
    """شناسهٔ گروه‌های فعالِ ثبت‌شده در ربات."""
    try:
        from modules import group_storage
        data = group_storage.load_groups()
    except Exception:
        return []
    groups = []
    for key, record in (data or {}).items():
        if not isinstance(record, dict):
            continue
        if not record.get("active"):
            continue
        value = _safe_int(key)
        if value:
            groups.append(value)
    return groups


async def watcher(bot, logger=None):
    """اسکن دوره‌ای گروه‌های فعال — سبک، با فاصله و بدون حلقهٔ تنگ."""
    initial = start_delay()
    if initial:
        await asyncio.sleep(initial)
    while True:
        try:
            if not auto_scan_members():
                # سیاست پیش‌فرض: «فقط کسانی که پیام می‌دهند». اسکن کامل اعضا
                # فقط با دستور «بررسی بیو» یا BIO_LINK_AUTO_SCAN_MEMBERS=1.
                await asyncio.sleep(scan_interval())
                continue
            groups = active_groups()
            for chat_id in groups:
                try:
                    if not bio_link_storage.is_enabled(chat_id):
                        continue
                    await scan_group(bot, chat_id, logger=logger)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _log(bot, f"WATCHER group={chat_id} error={type(error).__name__}", error=True)
                await asyncio.sleep(max(5.0, rpc_delay()))
            try:
                bio_link_storage.flush()
            except Exception:
                pass
        except asyncio.CancelledError:
            bio_link_storage.flush()
            raise
        except Exception as error:
            _log(bot, f"WATCHER LOOP error={type(error).__name__}", error=True)
        await asyncio.sleep(scan_interval())


def reset_runtime_state():
    """پاک کردن state درون‌حافظه — فقط برای تست."""
    global _semaphore, _last_flood_until
    _pending.clear()
    _inflight.clear()
    _retry_after.clear()
    _group_locks.clear()
    _semaphore = None
    _last_flood_until = 0.0

# ---------------------------------------------------------------------------
# دستورهای متنی (فقط ادمین/مالک گروه؛ بررسی مجوز در هندلر انجام می‌شود)
# ---------------------------------------------------------------------------

# ترتیب مهم است: عبارت‌های بلندتر اول تا «لغو فیلتر لینک بیو» با
# «فیلتر لینک بیو» جابه‌جا نشود.
_COMMANDS = (
    ("لغو فیلتر لینک بیو", "disable"),
    ("فیلتر لینک بیو", "enable"),
    ("وضعیت لینک بیو", "status"),
    ("وضعیت فیلتر لینک بیو", "status"),
    ("بررسی بیو", "scan"),
    ("اسکن بیو", "scan"),
    ("لیست لینک بیو", "list"),
    ("پاک لیست لینک بیو", "clear"),
    ("فیلتر آیدی بیو", "username_on"),
    ("لغو فیلتر آیدی بیو", "username_off"),
)


def normalize_text(text: str) -> str:
    """متن دستور را برای مقایسه یکدست می‌کند (پیشوند ! و / و . حذف می‌شود)."""
    value = str(text or "").strip()
    while value[:1] in {"!", "/", "."}:
        value = value[1:].lstrip()
    # نیم‌فاصله و فاصله‌های تکراری
    value = value.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    return " ".join(value.split())


def match_command(text: str) -> Optional[str]:
    """تشخیص دستور این قابلیت؛ خروجی نام عمل یا ``None``."""
    value = normalize_text(text)
    if not value:
        return None
    for phrase, action in _COMMANDS:
        if value == phrase:
            return action
    return None


def is_command(text: str) -> bool:
    """``True`` اگر متن یکی از دستورهای این قابلیت باشد."""
    return match_command(text) is not None
