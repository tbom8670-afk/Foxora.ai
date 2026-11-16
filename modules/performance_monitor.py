"""Low-overhead, owner-only monitoring for slow message handlers.

The hot path only updates small in-memory aggregates.  It never logs each slow
message, writes state, resolves peers, or makes an RPC.  This is deliberate:
monitoring must not become the source of the latency it observes.

Owner delivery is throttled separately from internal aggregation: at most one
owner message per cooldown window, and only for paths that are actually severe
(>=1000ms) or repeatedly slow.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from typing import Any, Dict, Optional

from modules.owner_private import resolve_private_owner_peer
from modules.time_utils import now_local

MIN_THRESHOLD_MS = 150.0
DEFAULT_ALERT_THRESHOLD_MS = 1000.0
DEFAULT_ALERT_INTERVAL_SECONDS = 15 * 60.0
DEFAULT_BATCH_INTERVAL_SECONDS = 15 * 60.0
DEFAULT_OWNER_COOLDOWN_SECONDS = 15 * 60.0
DEFAULT_RPC_PRESSURE_LIMIT = 8
DEFAULT_QUEUE_SIZE = 2
REPEAT_THRESHOLD = 3
NOISY_STAGE_REPEAT_MS = 500.0
NOISY_STAGES = ("receive", "spam_check")
MAX_BATCH_KEYS = 500


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, float(default))


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def _safe_handler(value: Any) -> str:
    text = "_".join(str(value or "unknown_handler").strip().split())
    return text[:120] or "unknown_handler"


def stage_name(handler: Any) -> str:
    text = str(handler or "").strip().lower()
    if ":" in text:
        return text.rsplit(":", 1)[-1].strip()
    return text


def is_noisy_stage(handler: Any) -> bool:
    return stage_name(handler) in NOISY_STAGES


def owner_worthy_event(event: Optional[Dict[str, Any]]) -> bool:
    """Whether a path belongs in an owner-facing slowness report.

    Internal aggregation still records every event above MIN_THRESHOLD_MS.
    Owner delivery is limited to:
    - paths whose max duration is at least 1000ms, or
    - paths that were slow several times in the window (count >= 3).
    receive / spam_check under 500ms are omitted unless they actually repeat.
    """
    if not isinstance(event, dict):
        return False
    try:
        max_ms = float(event.get("max_ms") or event.get("total_ms") or 0.0)
    except (TypeError, ValueError):
        max_ms = 0.0
    try:
        count = int(event.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    noisy = is_noisy_stage(event.get("handler"))
    if noisy and max_ms < NOISY_STAGE_REPEAT_MS and count < REPEAT_THRESHOLD:
        return False
    if max_ms >= DEFAULT_ALERT_THRESHOLD_MS:
        return True
    if count >= REPEAT_THRESHOLD:
        return True
    return False


class SlowProcessMonitor:
    """Aggregate slow events; send at most one owner report per cooldown window."""

    def __init__(
        self,
        client: Any,
        logger: Any,
        *,
        alert_threshold_ms: Optional[float] = None,
        alert_interval_seconds: Optional[float] = None,
        batch_interval_seconds: Optional[float] = None,
        rpc_pressure_limit: Optional[int] = None,
        queue_size: Optional[int] = None,
        state_path: Optional[os.PathLike] = None,
        send_timeout: float = 30.0,
        # Compatibility-only names from the previous monitor API.
        cooldown_seconds: Optional[float] = None,
        global_min_interval_seconds: Optional[float] = None,
        owner_notify_threshold_ms: Optional[float] = None,
    ):
        del state_path
        self.threshold_ms = MIN_THRESHOLD_MS
        requested_alert = (
            owner_notify_threshold_ms
            if alert_threshold_ms is None and owner_notify_threshold_ms is not None
            else alert_threshold_ms
        )
        requested_cooldown = (
            global_min_interval_seconds
            if global_min_interval_seconds is not None
            else cooldown_seconds
        )
        self.alert_threshold_ms = (
            _env_float(
                "WATCHDOG_SLOW_ALERT_THRESHOLD_MS",
                DEFAULT_ALERT_THRESHOLD_MS,
                MIN_THRESHOLD_MS,
            ) if requested_alert is None else max(MIN_THRESHOLD_MS, float(requested_alert))
        )
        self.alert_interval_seconds = (
            _env_float(
                "WATCHDOG_SLOW_ALERT_INTERVAL_SECONDS",
                DEFAULT_ALERT_INTERVAL_SECONDS,
            ) if alert_interval_seconds is None else max(0.0, float(alert_interval_seconds))
        )
        self.batch_interval_seconds = (
            _env_float(
                "WATCHDOG_SLOW_BATCH_INTERVAL_SECONDS",
                DEFAULT_BATCH_INTERVAL_SECONDS,
                minimum=1.0,
            ) if batch_interval_seconds is None else max(0.01, float(batch_interval_seconds))
        )
        self.owner_cooldown_seconds = (
            _env_float(
                "WATCHDOG_SLOW_OWNER_COOLDOWN_SECONDS",
                DEFAULT_OWNER_COOLDOWN_SECONDS,
            ) if requested_cooldown is None else max(0.0, float(requested_cooldown))
        )
        self.rpc_pressure_limit = (
            _env_int("WATCHDOG_SLOW_RPC_PRESSURE_LIMIT", DEFAULT_RPC_PRESSURE_LIMIT)
            if rpc_pressure_limit is None else max(1, int(rpc_pressure_limit))
        )
        selected_queue_size = (
            _env_int("WATCHDOG_SLOW_QUEUE_SIZE", DEFAULT_QUEUE_SIZE)
            if queue_size is None else max(1, int(queue_size))
        )
        self.client = client
        self.logger = logger
        self.send_timeout = max(1.0, float(send_timeout))
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=selected_queue_size)
        self._alert_task: Optional[asyncio.Task] = None
        self._batch_task: Optional[asyncio.Task] = None
        self._closed = False
        self._last_alert = 0.0
        self._last_owner_send = 0.0
        # key -> count/max/latest event. Aggregating prevents a busy group from
        # retaining an unbounded list in memory while preserving all slow paths.
        self._batch: Dict[str, Dict[str, Any]] = {}
        self._quiet_until = 0.0

    def quiet_for(self, seconds: float) -> None:
        """Ignore slow events for a while (restart warm-up is expected to be slow)."""
        try:
            self._quiet_until = time.monotonic() + max(0.0, float(seconds))
        except (TypeError, ValueError):
            self._quiet_until = 0.0

    def _log_error(self, message: str) -> None:
        method = getattr(self.logger, "log_error", None)
        if callable(method):
            method(message)

    def start(self) -> bool:
        if self._closed:
            return False
        if self._alert_task is None or self._alert_task.done():
            self._alert_task = asyncio.create_task(
                self._alert_worker(), name="slow-process-severe-reporter"
            )
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(
                self._batch_worker(), name="slow-process-summary-reporter"
            )
        return True

    def update_client(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def _key(handler: str, chat_id: Any) -> str:
        return f"{handler}|{chat_id}"

    def _owner_cooldown_ok(self, now: float) -> bool:
        if self._last_owner_send <= 0:
            return True
        return (now - self._last_owner_send) >= self.owner_cooldown_seconds

    def _reserve_owner_send(self, now: float) -> bool:
        if not self._owner_cooldown_ok(now):
            return False
        self._last_owner_send = now
        self._last_alert = now
        return True

    def record(
        self,
        *,
        total_ms: float,
        chat_id: Any,
        message_id: Any,
        handler: str,
        timestamp: Optional[str] = None,
        now_epoch: Optional[float] = None,
        notes: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record in memory only; return True only when a severe alert queued.

        ``notes`` زمانِ زیرمرحله‌های نام‌دار همان پیام است (مثلاً
        ``bio_check=812.3`` یا ``sender_fetch=940.1``)؛ سه موردِ برتر در
        گزارشِ مالک نمایش داده می‌شود تا «کندی از کدام مسیر است» بدونِ لاگِ
        runtime هم قابلِ دیدن باشد.
        """
        try:
            elapsed = float(total_ms)
        except (TypeError, ValueError):
            return False
        if elapsed <= self.threshold_ms or self._closed:
            return False
        if self._quiet_until and time.monotonic() < self._quiet_until:
            return False
        if self._alert_task is None or self._alert_task.done():
            try:
                self.start()
            except RuntimeError:
                return False

        clean_notes: Dict[str, float] = {}
        if isinstance(notes, dict):
            for name, value in notes.items():
                try:
                    ms = float(value)
                except (TypeError, ValueError):
                    continue
                if ms > 0:
                    clean_notes[str(name)] = round(ms, 1)
            clean_notes = dict(
                sorted(clean_notes.items(), key=lambda kv: -kv[1])[:3]
            )
        event = {
            "total_ms": round(elapsed, 3),
            "chat_id": chat_id,
            "message_id": message_id,
            "handler": _safe_handler(handler),
            "timestamp": timestamp or now_local().isoformat(),
            "notes": clean_notes,
        }
        key = self._key(event["handler"], chat_id)
        aggregate = self._batch.get(key)
        if aggregate is None:
            aggregate = {**event, "count": 1, "max_ms": event["total_ms"]}
            self._batch[key] = aggregate
        else:
            aggregate["count"] += 1
            aggregate["max_ms"] = max(float(aggregate["max_ms"]), event["total_ms"])
            aggregate.update(event)
        self.trim_batch()

        real_now = time.time()
        now = real_now if now_epoch is None else float(now_epoch)
        if elapsed < self.alert_threshold_ms:
            return False
        if not owner_worthy_event(aggregate):
            return False
        if not self._owner_cooldown_ok(real_now):
            return False
        if self._last_alert > 0 and now - self._last_alert < self.alert_interval_seconds:
            return False
        try:
            self.queue.put_nowait(dict(event))
        except asyncio.QueueFull:
            return False
        # Reserve before the network task so bursts cannot enqueue multiple RPCs.
        self._last_alert = now
        self._last_owner_send = real_now
        return True

    @staticmethod
    def _size_of(value: Any) -> int:
        try:
            return len(value)
        except (TypeError, AttributeError):
            return 0

    def _rpc_pressure(self) -> int:
        sender = getattr(self.client, "_sender", None)
        if sender is None:
            return 0
        return max(
            self._size_of(getattr(sender, "_pending_state", None)),
            self._size_of(getattr(sender, "_pending", None)),
            self._size_of(getattr(sender, "_send_queue", None)),
        )

    async def _send(self, text: str, context: str) -> bool:
        if self._rpc_pressure() >= self.rpc_pressure_limit:
            return False
        try:
            target = await resolve_private_owner_peer(
                self.client, logger=None, context=context
            )
            result = self.client.send_message(target, text)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=self.send_timeout)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log_error(f"SLOW_PROCESS {context} FAILED error={error!r}")
            return False

    @staticmethod
    def _notes_text(event: Dict[str, Any]) -> str:
        """«زیرمرحله‌ها: bio_check=812.3ms, …» — مسیرِ دقیقِ کندی."""
        notes = event.get("notes") or {}
        if not isinstance(notes, dict) or not notes:
            return ""
        pairs = []
        for name, value in notes.items():
            try:
                ms = float(value)
            except (TypeError, ValueError):
                continue
            if ms > 0:
                pairs.append((str(name), ms))
        pairs.sort(key=lambda kv: -kv[1])
        return "، ".join(f"{name}={ms:.1f}ms" for name, ms in pairs[:3])

    @staticmethod
    def format_alert(event: Dict[str, Any]) -> str:
        text = (
            "⚠️ گزارش کندی شدید ربات\n\n"
            f"زمان کل پردازش: {float(event['total_ms']):.1f} ms\n"
            f"chat_id: {event.get('chat_id')}\n"
            f"message_id: {event.get('message_id')}\n"
            f"handler: {event.get('handler')}\n"
            f"timestamp: {event.get('timestamp')}"
        )
        notes_text = SlowProcessMonitor._notes_text(event)
        if notes_text:
            text += f"\nزیرمرحله‌ها: {notes_text}"
        return text

    @staticmethod
    def format_batch(events: list[Dict[str, Any]]) -> str:
        lines = ["📊 گزارش تجمیعی کندی ربات", "", f"تعداد مسیرهای کند: {len(events)}", ""]
        for event in events:
            line = (
                f"• {event['handler']} | chat={event['chat_id']} | "
                f"تعداد={event['count']} | آخرین={float(event['total_ms']):.1f}ms | "
                f"بیشینه={float(event['max_ms']):.1f}ms | msg={event['message_id']}"
            )
            notes_text = SlowProcessMonitor._notes_text(event)
            if notes_text:
                line += f" | زیرمرحله‌ها: {notes_text}"
            lines.append(line)
        return "\n".join(lines[:250])

    def _owner_snapshot(self) -> list[Dict[str, Any]]:
        return [item for item in self._batch.values() if owner_worthy_event(item)]

    def trim_batch(self, max_keys: int = MAX_BATCH_KEYS) -> int:
        """Drop oldest non-worthy aggregates first so unique chats cannot leak RAM."""
        limit = max(1, int(max_keys))
        dropped = 0
        if len(self._batch) <= limit:
            return 0
        extras = [
            key for key, event in list(self._batch.items())
            if not owner_worthy_event(event)
        ]
        for key in extras:
            if len(self._batch) <= limit:
                break
            if self._batch.pop(key, None) is not None:
                dropped += 1
        while len(self._batch) > limit:
            self._batch.pop(next(iter(self._batch)), None)
            dropped += 1
        return dropped

    async def flush_batch(self) -> bool:
        if not self._batch or self._rpc_pressure() >= self.rpc_pressure_limit:
            return False
        snapshot = self._owner_snapshot()
        if not snapshot:
            return False
        now = time.time()
        previous_send = self._last_owner_send
        previous_alert = self._last_alert
        if not self._reserve_owner_send(now):
            return False
        # Keep entries until the send succeeds, so a reconnect cannot lose them.
        if await self._send(self.format_batch(snapshot), "SLOW_PROCESS_BATCH"):
            for event in snapshot:
                key = self._key(event["handler"], event["chat_id"])
                if self._batch.get(key) is event:
                    self._batch.pop(key, None)
            return True
        self._last_owner_send = previous_send
        self._last_alert = previous_alert
        return False

    async def _alert_worker(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._send(self.format_alert(event), "SLOW_PROCESS_ALERT")
            finally:
                self.queue.task_done()

    async def _batch_worker(self) -> None:
        while True:
            await asyncio.sleep(self.batch_interval_seconds)
            await self.flush_batch()

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in (self._alert_task, self._batch_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._alert_task = self._batch_task = None
