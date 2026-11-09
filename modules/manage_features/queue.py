# -*- coding: utf-8 -*-
"""🧵 صف اختصاصیِ هر قابلیت با کارگرِ مستقل به‌ازای هر گروه.

سه نمونهٔ مستقل ساخته می‌شود (``exclusive_access_queue``،
``timed_message_queue``، ``user_report_queue``) و هیچ‌کدام داده یا کارگر
مشترک ندارند؛ کندی/خطای یکی روی بقیه اثر نمی‌گذارد.

ویژگی‌ها (مطابق الزامات ماموریت):

* **کارگر مستقل برای هر گروه** — عملیاتِ سنگینِ یک گروه بقیه را قفل نمی‌کند.
* **ظرفیت محدود**: سقفِ در انتظار به‌ازای گروه، سقف کل و سقف تعداد گروه‌های
  فعال؛ سرریز ⇒ ``QueueFull`` و ثبتِ خطا (بدون انفجار حافظه).
* **جلوگیری از اجرای تکراری**: ``serialize=True`` یعنی کارهای یک کلید
  (گروه یا گروه+کاربر) پشت سر هم اجرا می‌شوند؛ ``dedupe_key`` یعنی اگر کاری
  با همان کلید در صف است، دوباره صف نمی‌شود.
* **تایم‌اوت** هر کار با ``asyncio.wait_for`` (ساعت یکنواخت).
* **خطا مدیریت‌شده**: استثنای یک کار فقط لاگ می‌شود؛ کارگر و بقیهٔ صف زنده.
* **وضعیت پایدار**: خودِ صف در حافظه است ولی «درخواست‌های بازیابی» در
  storage هر قابلیت ذخیره می‌شوند و موتور همان قابلیت در استارتاپ آن‌ها را
  دوباره صف می‌کند (نقطهٔ بازیابی صریح در ``recover()``).
"""
from __future__ import annotations

import asyncio
import inspect
import os
from collections import OrderedDict, deque


class QueueFull(Exception):
    """ظرفیت صف پر است (نه خطای برنامه‌نویسی)."""


def _env_int(name, default, minimum=1, maximum=100000):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


class PerGroupQueue:
    """صفِ FIFO با کارگرِ مستقل به‌ازای هر کلید و محدودیت ظرفیت."""

    def __init__(self, name, *, max_pending_per_key=None,
                 max_pending_total=None, max_keys=None, task_timeout=120.0):
        self.name = name
        self.max_pending_per_key = (
            max_pending_per_key
            or _env_int(f"FOXORA_{name.upper()}_PER_KEY", 32, 1, 512)
        )
        self.max_pending_total = (
            max_pending_total
            or _env_int(f"FOXORA_{name.upper()}_TOTAL", 512, 1, 8192)
        )
        self.max_keys = max_keys or _env_int(
            f"FOXORA_{name.upper()}_KEYS", 64, 1, 1024)
        self.task_timeout = float(task_timeout)

        self._queues: "OrderedDict[str, deque]" = OrderedDict()
        self._workers: dict = {}
        self._pending: dict = {}
        self._total_pending = 0
        self._serial_locks: dict = {}
        self._dedupe: set = set()
        self._closed = False
        self._stats = {"submitted": 0, "done": 0, "failed": 0,
                       "rejected": 0, "timed_out": 0}

    # ------------------------------------------------------------------
    #  ثبت کار
    # ------------------------------------------------------------------
    async def submit(self, key, job, *, serialize=True, dedupe_key=None,
                     logger=None, error_callback=None):
        """اجرای ``job`` (async یا sync) در کارگرِ همان ``key``."""
        key = str(key)
        if self._closed:
            raise QueueFull(f"{self.name}: queue is closed")

        dedupe_token = None
        if dedupe_key is not None:
            dedupe_token = f"{key}|{dedupe_key}"
            if dedupe_token in self._dedupe:
                self._stats["rejected"] += 1
                raise QueueFull(
                    f"{self.name}: duplicate job for {dedupe_token}")

        if len(self._queues) >= self.max_keys and key not in self._queues:
            self._stats["rejected"] += 1
            raise QueueFull(f"{self.name}: too many active keys")
        pending_key = self._pending.get(key, 0)
        if pending_key >= self.max_pending_per_key:
            self._stats["rejected"] += 1
            raise QueueFull(f"{self.name}: per-key capacity reached ({key})")
        if self._total_pending >= self.max_pending_total:
            self._stats["rejected"] += 1
            raise QueueFull(f"{self.name}: total capacity reached")

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        queue = self._queues.get(key)
        if queue is None:
            queue = deque()
            self._queues[key] = queue
        queue.append((job, future, serialize, error_callback, logger,
                      dedupe_token))
        self._pending[key] = pending_key + 1
        self._total_pending += 1
        self._stats["submitted"] += 1
        if dedupe_token is not None:
            self._dedupe.add(dedupe_token)

        worker = self._workers.get(key)
        if worker is None or worker.done():
            self._workers[key] = asyncio.create_task(
                self._worker(key), name=f"{self.name}:{key}")
        return future

    async def run(self, key, job, *, timeout=None, **kwargs):
        """ثبت کار و انتظار برای نتیجه (با تایم‌اوت)."""
        future = await self.submit(key, job, **kwargs)
        return await asyncio.wait_for(future, timeout or self.task_timeout)

    # ------------------------------------------------------------------
    #  کارگر
    # ------------------------------------------------------------------
    async def _worker(self, key):
        queue = self._queues.get(key)
        while queue:
            job, future, serialize, error_callback, logger, dedupe_token = \
                queue.popleft()
            self._pending[key] -= 1
            self._total_pending -= 1
            try:
                if serialize:
                    result = await self._execute_serial(key, job)
                else:
                    result = await self._execute(job)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except asyncio.TimeoutError:
                self._stats["timed_out"] += 1
                self._log_error(logger, f"{self.name} TIMEOUT key={key}")
                if error_callback is not None:
                    await self._call(error_callback, asyncio.TimeoutError(
                        f"{self.name} task timed out"))
                if not future.done():
                    future.set_exception(
                        asyncio.TimeoutError(f"{self.name} task timed out"))
            except Exception as error:               # noqa: BLE001
                self._stats["failed"] += 1
                self._log_error(
                    logger, f"{self.name} JOB FAILED key={key} error={error!r}")
                if error_callback is not None:
                    await self._call(error_callback, error)
                if not future.done():
                    future.set_exception(error)
            else:
                self._stats["done"] += 1
                if not future.done():
                    future.set_result(result)
            finally:
                if dedupe_token is not None:
                    self._dedupe.discard(dedupe_token)
        self._workers.pop(key, None)
        self._queues.pop(key, None)
        self._pending.pop(key, None)

    async def _execute(self, job):
        result = job()
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, self.task_timeout)
        return result

    async def _execute_serial(self, key, job):
        lock = self._serial_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._serial_locks[key] = lock
        async with lock:
            return await self._execute(job)

    @staticmethod
    async def _call(callback, error):
        try:
            result = callback(error)
            if inspect.isawaitable(result):
                await result
        except Exception:                            # noqa: BLE001
            pass

    @staticmethod
    def _log_error(logger, message):
        if logger is None:
            return
        try:
            logger.log_error(message)
        except Exception:                            # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    #  مدیریت/پایش
    # ------------------------------------------------------------------
    @property
    def total_pending(self):
        return self._total_pending

    def stats(self):
        return dict(self._stats, pending=self._total_pending,
                    keys=len(self._queues))

    def busy(self, key):
        key = str(key)
        return bool(self._pending.get(key)) or key in self._workers

    async def drain(self, timeout=5.0):
        """انتظار تا خالی شدن صف (برای تست/خاموشی تمیز)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while (self._total_pending or any(
                not worker.done() for worker in self._workers.values())):
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.005)
        return True

    async def close(self):
        self._closed = True
        for worker in list(self._workers.values()):
            worker.cancel()
        self._workers.clear()
        for queue in self._queues.values():
            while queue:
                _job, future, *_rest = queue.popleft()
                if not future.done():
                    future.cancel()
        self._queues.clear()
        self._pending.clear()
        self._total_pending = 0
        self._dedupe.clear()

    def reopen(self):
        self._closed = False


def reset_queue(queue):
    """بازگردانی یک صف به حالت اولیه (همگام؛ برای تست)."""
    queue._closed = False
    queue._queues.clear()
    queue._workers.clear()
    queue._pending.clear()
    queue._total_pending = 0
    queue._serial_locks.clear()
    queue._dedupe.clear()
    queue._stats = {"submitted": 0, "done": 0, "failed": 0,
                    "rejected": 0, "timed_out": 0}
