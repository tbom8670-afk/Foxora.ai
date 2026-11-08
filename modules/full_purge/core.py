# -*- coding: utf-8 -*-
"""🧬 «پاکسازی کامل» — هستهٔ حذفِ پیوستهٔ پیام‌های یک گروه.

این ماژول هیچ‌کار با صف‌های حذفِ موجود (``message_delete_queue``،
``fair_delete_queue``، «پاک عدد»، پاکسازیِ خودکار و …) ندارد: صفِ اختصاصیِ
خودش را دارد، قفلِ per-chat خودش را دارد و هر گروه کارگرِ مستقل خودش را.

قواعدِ واقعیِ API (بررسی‌شده روی splusthon 1.1.4):

* ``client.get_messages(entity, limit=n, max_id=…)`` → ``limit`` فقط ظرفیتِ
  هر *درخواست* است، پس با ``max_id`` صفحه‌به‌صفحه از جدید به قدیم می‌خوانیم
  و هیچ سقفی برای «تعداد کل» نداریم.
* ``client.delete_messages(entity, ids)`` یک RPC است؛ سقفِ امنِ دسته
  ``BATCH_SIZE = 100`` شناسه (همان عددی که پروژه برای «پاک عدد» استفاده
  می‌کند). بیشتر از این، سرور سروش درخواست را رد می‌کند.
* حذفِ شناسه‌ای که قبلاً حذف شده خطا نمی‌دهد (بی‌اثر است)، پس شمارندهٔ ما
  فقط «حذف‌های موفقِ RPC» را می‌شمارد.
* ``iter_messages`` هم دارد ولی یک‌بارمصرف است؛ برای ادامهٔ قابل‌کنترل
  (وضعیت‌گذاری بین دسته‌ها) از حلقهٔ ``max_id`` استفاده می‌کنیم.

نتیجه: پاکسازی هیچ سقفِ مصنوعی ندارد و تا وقتی صفحهٔ بعدی پیام دارد ادامه
می‌دهد؛ فقط اگر خودِ API دسترسی به بخشی از تاریخچه را ندهد (گروه‌هایی که
تاریخچه برای ربات مخفی است) همان‌جا با گزارشِ صریح تمام می‌شود.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

from modules.full_purge import texts

# ---------------------------------------------------------------------------
# ثابت‌های اجرایی
# ---------------------------------------------------------------------------
BATCH_SIZE = 100            # سقفِ شناسه در یک RPC حذف (سرور سروش)
PAGE_SIZE = 100             # ظرفیتِ هر درخواست خواندنِ تاریخچه
READ_PARALLEL = 2           # خواندنِ چند صفحه هم‌زمان (فقط بعد از صفحهٔ اول)
DELETE_PARALLEL = 4         # چند RPC حذف هم‌زمان برای همین گروه
RPC_TIMEOUT = 10.0          # سقفِ انتظار هر RPC
MAX_RETRY = 3               # تلاشِ مجدد برای خطاهای موقت
FLOOD_MAX_WAIT = 60.0       # FloodWaitهای کوتاه را صبر می‌کنیم، بلندتر را نه
STATUS_MIN_INTERVAL = 2.0   # کمترین فاصلهٔ دو ویرایشِ پیام وضعیت
STATUS_MIN_DELTA = 25       # کمترین رشدِ شمارنده برای ویرایشِ بعدی
MAX_STATUS_FAILURES = 3     # بعد از این تعداد، ویرایشِ وضعیت کنار گذاشته می‌شود
MAX_PAGES = 100000          # فقط برای جلوگیری از حلقهٔ بی‌نهایت در بدترین حالت

#: قفل‌های مستقلِ هر گروه (Per-Chat Lock).
_ACTIVE: "OrderedDict[str, dict]" = OrderedDict()
_LOCK_MAX_ENTRIES = 512


def _key(chat_id):
    return str(chat_id)


def is_running(chat_id) -> bool:
    state = _ACTIVE.get(_key(chat_id))
    return bool(state and state.get("running"))


def active_chats():
    return [key for key, state in list(_ACTIVE.items()) if state.get("running")]


def _register(chat_id, state):
    key = _key(chat_id)
    _ACTIVE[key] = state
    _ACTIVE.move_to_end(key)
    while len(_ACTIVE) > _LOCK_MAX_ENTRIES:
        oldest_key, oldest = next(iter(_ACTIVE.items()))
        if oldest.get("running"):
            _ACTIVE.move_to_end(oldest_key)
            break
        _ACTIVE.popitem(last=False)


def _unregister(chat_id):
    _ACTIVE.pop(_key(chat_id), None)


def reset_all(chat_id=None):
    """آزادسازیِ state درون‌حافظه (تست/ری‌استارت)."""
    if chat_id is None:
        _ACTIVE.clear()
        return
    _ACTIVE.pop(_key(chat_id), None)


def flood_seconds(error):
    """اگر خطا FloodWait بود، ثانیهٔ انتظار را برمی‌گرداند؛ وگرنه ``None``."""
    name = error.__class__.__name__.lower()
    text = ""
    try:
        text = str(error).lower()
    except Exception:
        text = ""
    if "flood" not in name and "flood" not in text:
        return None
    for attr in ("seconds", "value"):
        raw = getattr(error, attr, None)
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                break
    return 1.0


def _is_transient(error):
    if flood_seconds(error) is not None:
        return True
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    name = error.__class__.__name__.lower()
    return any(marker in name for marker in ("timeout", "connection", "server", "internal"))


def _message_id(message):
    value = getattr(message, "id", None)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _peer(bot, chat_id):
    """همان ``InputPeer`` کش‌شدهٔ گروه؛ بدونِ آن شناسهٔ خام (مثل بقیهٔ حذف‌ها)."""
    cache = getattr(bot, "reply_input_peer_cache", None)
    if cache:
        try:
            peer = cache.get(chat_id)
        except Exception:
            peer = None
        if peer is not None:
            return peer
        wanted = str(chat_id)
        try:
            for cached_id, cached_peer in list(cache.items()):
                if cached_peer is not None and str(cached_id) == wanted:
                    return cached_peer
        except Exception:
            pass
    return chat_id


async def _rpc(coro, *, label, chat_id, logger):
    """اجرای یک RPC با timeout؛ خطا را برمی‌گرداند به‌جای پرتاب."""
    try:
        return await asyncio.wait_for(coro, timeout=RPC_TIMEOUT), None
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if logger is not None:
            try:
                logger.log_error(
                    f"FULL PURGE RPC ERROR label={label} chat_id={chat_id} "
                    f"error={error!r}"
                )
            except Exception:
                pass
        return None, error


async def fetch_page(bot, chat_id, *, max_id=0, page_size=PAGE_SIZE, logger=None):
    """یک صفحهٔ تاریخچه از جدید به قدیم (``id < max_id``).

    خروجی ``(ids, error)``؛ ``ids`` بدونِ تکرار و نزولی است. ``min_id`` برای
    صفحه‌بندی لازم نیست چون خودمان ``max_id`` را عقب می‌بریم.
    """
    peer = _peer(bot, chat_id)
    kwargs = {"limit": page_size}
    if max_id:
        kwargs["max_id"] = int(max_id)
    result, error = await _rpc(
        bot.client.get_messages(peer, **kwargs),
        label="get_messages", chat_id=chat_id, logger=logger,
    )
    if result is None:
        return [], error
    rows = result if isinstance(result, (list, tuple)) else [result]
    ids = []
    for message in rows:
        if message is None:
            continue
        message_id = _message_id(message)
        if message_id and message_id > 0 and message_id not in ids:
            ids.append(message_id)
    ids.sort(reverse=True)
    return ids, None


async def delete_batch(bot, chat_id, ids, *, logger=None):
    """حذفِ یک دسته با Retry/Backoff. خروجی ``(deleted_count, failed_ids)``."""
    ids = [int(value) for value in ids if value]
    if not ids:
        return 0, []
    peer = _peer(bot, chat_id)
    attempt = 1
    while attempt <= MAX_RETRY:
        result, error = await _rpc(
            bot.client.delete_messages(peer, list(ids)),
            label="delete_messages", chat_id=chat_id, logger=logger,
        )
        if error is None:
            return len(ids), []
        wait = flood_seconds(error)
        if wait is not None:
            if wait <= FLOOD_MAX_WAIT and attempt < MAX_RETRY:
                await asyncio.sleep(wait + 0.5)
                attempt += 1
                continue
            return 0, ids
        if _is_transient(error) and attempt < MAX_RETRY:
            await asyncio.sleep(min(2.0, 0.25 * (2 ** (attempt - 1))))
            attempt += 1
            continue
        return 0, ids
    return 0, ids


class StatusEditor:
    """ویرایشِ هوشمندِ *همان* پیام وضعیت (بدونِ پیام جدید).

    ویرایش در تسکِ جدا انجام می‌شود و نتیجه‌اش هرگز مسیرِ حذف را قفل نمی‌کند؛
    اگر ویرایش شکست بخورد فقط لاگ می‌شود و پاکسازی ادامه می‌یابد.
    """

    def __init__(self, bot, chat_id, message, *, logger=None):
        self.bot = bot
        self.chat_id = chat_id
        self.message = message
        self.logger = logger
        self.last_text = None
        self.last_at = 0.0
        self.last_count = 0
        self.failures = 0
        self.edits = 0

    @property
    def available(self) -> bool:
        return self.message is not None and self.failures < MAX_STATUS_FAILURES

    async def maybe_update(self, *, deleted, duration, state="running",
                           remaining=None, total=None, lock_released=False,
                           force=False):
        if not self.available:
            return False
        now = time.monotonic()
        if not force:
            if now - self.last_at < STATUS_MIN_INTERVAL:
                return False
            if deleted - self.last_count < STATUS_MIN_DELTA:
                return False
        text = texts.status_text(state=state, deleted=deleted,
                                 duration=duration, remaining=remaining,
                                 total=total, lock_released=lock_released)
        if text == self.last_text:
            return False
        entities = texts.status_entities(text)
        try:
            await asyncio.wait_for(
                self.bot.client.edit_message(
                    self.message, text, formatting_entities=entities),
                timeout=RPC_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failures += 1
            if self.logger is not None:
                try:
                    self.logger.log_error(
                        "FULL PURGE STATUS EDIT FAILED "
                        f"chat_id={self.chat_id} failures={self.failures} "
                        f"error={error!r}"
                    )
                except Exception:
                    pass
            return False
        self.last_text = text
        self.last_at = now
        self.last_count = deleted
        self.edits += 1
        return True


# ---------------------------------------------------------------------------
# اجرای کاملِ یک گروه
# ---------------------------------------------------------------------------
async def run(bot, chat_id, *, status: StatusEditor | None = None,
              logger=None, clock=time.monotonic, on_lock=None,
              on_unlock=None) -> dict:
    """پاکسازیِ کاملِ یک گروه تا آخرین پیامِ قابل‌حذف.

    ``on_lock``/``on_unlock`` قلاب‌های قفل/بازکردن گروه‌اند تا این ماژول به
    لایهٔ مدیریتیِ گروه وابسته نشود (تست‌پذیرِ خالص).

    خروجی: ``{"deleted": int, "scanned": int, "pages": int, "failed": int,
    "cancelled": bool, "reason": str|None, "elapsed": float}``
    """
    started = clock()
    state = {"running": True, "started_at": started, "deleted": 0}
    _register(chat_id, state)

    deleted = 0
    scanned = 0
    pages = 0
    failed_total = 0
    cancelled = False
    reason = None
    locked = False
    pending = []                # شناسه‌هایی که حذفشان ناموفق بود
    retried_failures = 0
    seen_failed = set()

    async def publish(force=False, state_name="running", **extra):
        if status is None:
            return
        await status.maybe_update(
            deleted=deleted, duration=clock() - started, state=state_name,
            force=force, **extra,
        )

    try:
        if on_lock is not None:
            locked = bool(await on_lock())
        if status is not None:
            await status.maybe_update(deleted=0, duration=0.0,
                                      state="locked", force=True)
        await publish(force=True)

        max_id = 0
        while pages < MAX_PAGES:
            ids, error = await fetch_page(
                bot, chat_id, max_id=max_id, logger=logger)
            if error is not None:
                # صفحهٔ اولِ ناموفق یعنی دسترسی به تاریخچه نداریم؛ ادامه بی‌معناست.
                if not scanned and not deleted:
                    reason = "history_unavailable"
                break
            if not ids:
                break
            pages += 1
            scanned += len(ids)
            max_id = min(ids)          # صفحهٔ بعدی: قدیمی‌تر از این‌ها

            batches = [ids[start:start + BATCH_SIZE]
                       for start in range(0, len(ids), BATCH_SIZE)]
            semaphore = asyncio.Semaphore(DELETE_PARALLEL)

            async def _worker(batch):
                nonlocal deleted, failed_total
                async with semaphore:
                    ok, failed = await delete_batch(
                        bot, chat_id, batch, logger=logger)
                    deleted += ok
                    if failed:
                        failed_total += len(failed)
                        pending.extend(failed)

            await asyncio.gather(*[_worker(batch) for batch in batches],
                                 return_exceptions=True)
            state["deleted"] = deleted
            await publish(total=scanned, remaining=len(pending))

        # موجِ پایانی: تلاشِ دوباره برای همان شناسه‌هایی که سرور نپذیرفت.
        if pending:
            leftovers = [value for value in dict.fromkeys(pending)]
            pending = []
            for start in range(0, len(leftovers), BATCH_SIZE):
                batch = leftovers[start:start + BATCH_SIZE]
                ok, failed = await delete_batch(bot, chat_id, batch, logger=logger)
                deleted += ok
                retried_failures += len(failed)
                if failed:
                    pending.extend(failed)
                await publish(total=scanned, remaining=len(pending))
            seen_failed = set(pending)
    except asyncio.CancelledError:
        cancelled = True
        reason = "cancelled"
    except Exception as error:                       # noqa: BLE001
        reason = f"error:{error.__class__.__name__}"
        if logger is not None:
            try:
                logger.log_error(
                    f"FULL PURGE CRASHED chat_id={chat_id} error={error!r}")
            except Exception:
                pass
    finally:
        elapsed = clock() - started
        released = False
        if locked and on_unlock is not None:
            try:
                released = bool(await on_unlock())
            except Exception as unlock_error:        # noqa: BLE001
                if logger is not None:
                    try:
                        logger.log_error(
                            "FULL PURGE UNLOCK FAILED "
                            f"chat_id={chat_id} error={unlock_error!r}")
                    except Exception:
                        pass
        if status is not None:
            final_state = "running"
            if cancelled or reason == "cancelled":
                final_state = "failed"
            elif reason == "history_unavailable":
                final_state = "failed"
            elif reason:
                final_state = "failed"
            else:
                final_state = "done"
            await status.maybe_update(
                deleted=deleted, duration=elapsed, state=final_state,
                remaining=len(pending), lock_released=released, force=True)
        state["running"] = False
        _unregister(chat_id)

    return {
        "deleted": deleted,
        "scanned": scanned,
        "pages": pages,
        "failed": retried_failures,
        "unreachable": len(seen_failed),
        "cancelled": cancelled,
        "reason": reason,
        "elapsed": elapsed,
        "lock_released": released,
    }
