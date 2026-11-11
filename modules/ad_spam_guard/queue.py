"""صف اختصاصی حذفِ پیام‌های ربات‌های تبلیغاتی.

این صف **مستقل** از صف‌های اصلی ربات (``modules.delete_queue``،
``modules.fair_delete_queue``، ``modules.fair_queue``) کار می‌کند تا:

* هیچ‌وقت صف حذفِ عادیِ گروه را اشغال نکند،
* اگر این قابلیت خطا کرد، مسیر حذفِ بقیهٔ ربات دست‌نخورده بماند،
* بارِ حذفِ اسپم قابل مشاهده و محدود باشد (سقف در انتظار + سقف per-group).

ساختار ساده و تک‌نخی است: یک صف asyncio + یک worker. هر گروه سقف مستقل
دارد و پیام‌های تکراریِ همان (گروه، پیام) دوباره صف نمی‌شوند.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


class AdSpamDeleteQueue:
    """صف bounded و تک‌worker برای حذف پیام ربات‌های تبلیغاتی."""

    def __init__(
        self,
        delete_message,
        *,
        max_pending: int = 500,
        max_pending_per_group: int = 100,
        max_attempts: int = 3,
    ) -> None:
        if max_pending < 1 or max_pending_per_group < 1:
            raise ValueError("ad-spam queue bounds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._delete_message = delete_message
        self.max_pending = max_pending
        self.max_pending_per_group = max_pending_per_group
        self.max_attempts = max_attempts
        self._queue: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue()
        self._pending: "OrderedDict[tuple[str, str], int]" = OrderedDict()
        self._group_pending: dict[str, int] = {}
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._failure_log: "OrderedDict[str, float]" = OrderedDict()
        self._stats = {"deleted": 0, "failed": 0, "dropped": 0}

    # -- چرخهٔ عمر ---------------------------------------------------------

    def start(self) -> None:
        """worker را بالا می‌آورد (idempotent)."""
        if self._worker is None or self._worker.done():
            self._closing = False
            self._worker = asyncio.create_task(
                self._run(), name="foxbot:ad-spam-delete"
            )

    async def stop(self, *, drain: bool = True) -> None:
        """worker را متوقف می‌کند. با ``drain=False`` صرفِ ناتمام رها می‌شود."""
        self._closing = True
        task = self._worker
        if task is None:
            return
        if not drain:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._worker is task:
                self._worker = None
        if not drain:
            self._pending.clear()
            self._group_pending.clear()

    @property
    def pending(self) -> int:
        return len(self._pending)

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # -- ثبت کار -----------------------------------------------------------

    def submit(self, chat_id, message_id) -> bool:
        """یک پیام را برای حذف صف می‌کند. اگر تکراری/پُر باشد False."""
        if self._closing:
            return False
        group = str(chat_id or "").strip()
        mid = str(message_id or "").strip()
        if not group or not mid or mid == "0":
            return False
        key = (group, mid)
        if key in self._pending:
            return False
        if len(self._pending) >= self.max_pending:
            self._log_failure(group, "queue_capacity")
            self._stats["dropped"] += 1
            return False
        if self._group_pending.get(group, 0) >= self.max_pending_per_group:
            self._log_failure(group, "group_capacity")
            self._stats["dropped"] += 1
            return False
        self._pending[key] = 0
        self._group_pending[group] = self._group_pending.get(group, 0) + 1
        self._queue.put_nowait((group, mid, 0))
        return True

    # -- worker -------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            group, mid, attempts = await self._queue.get()
            key = (group, mid)
            try:
                deleted = False
                try:
                    deleted = bool(await self._delete_message(group, mid))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - هر گروه مستقل است
                    self._log_failure(group, type(exc).__name__)
                if not deleted and attempts + 1 < self.max_attempts and not self._closing:
                    await asyncio.sleep(min(0.05 * (2**attempts), 0.5))
                    self._pending[key] = attempts + 1
                    self._queue.put_nowait((group, mid, attempts + 1))
                    continue
                if deleted:
                    self._stats["deleted"] += 1
                else:
                    self._stats["failed"] += 1
                    self._log_failure(group, "delete_not_confirmed")
            finally:
                if self._pending.pop(key, None) is not None:
                    left = self._group_pending.get(group, 1) - 1
                    if left > 0:
                        self._group_pending[group] = left
                    else:
                        self._group_pending.pop(group, None)
                self._queue.task_done()

    def _log_failure(self, group: str, code: str) -> None:
        now = time.monotonic()
        previous = self._failure_log.get(group)
        if previous is not None and now - previous < 30.0:
            return
        self._failure_log[group] = now
        self._failure_log.move_to_end(group)
        while len(self._failure_log) > 512:
            self._failure_log.popitem(last=False)
        logger.warning(
            "ad-spam delete queue incomplete group=%s reason=%s", group, code
        )
