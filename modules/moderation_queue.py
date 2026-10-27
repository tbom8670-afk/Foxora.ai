"""صف moderation با هم‌زمانی محدود برای کاربران مستقل هر گروه."""
import asyncio
import os
try:
    from modules.group_id import normalize_group_id
    _HAS_NORMALIZE = True
except ImportError:
    _HAS_NORMALIZE = False
    def normalize_group_id(v):
        try:
            return str(int(v))
        except Exception:
            return str(v)

def _chat_key(chat_id):
    """Hashable key for chat. Never use InputPeer directly."""
    if chat_id is None:
        return "0"
    for attr in ("channel_id", "chat_id", "user_id", "id"):
        try:
            val = getattr(chat_id, attr, None)
            if isinstance(val, int):
                return normalize_group_id(val) if _HAS_NORMALIZE else str(val)
            if val is not None:
                try:
                    ival = int(val)
                    return normalize_group_id(ival) if _HAS_NORMALIZE else str(ival)
                except Exception:
                    return str(val)
        except Exception:
            continue
    try:
        ival = int(chat_id)
        return normalize_group_id(ival) if _HAS_NORMALIZE else str(ival)
    except Exception:
        pass
    try:
        from splusthon import utils as _sutils
        peer = _sutils.get_peer_id(chat_id)
        if peer is not None:
            try:
                return normalize_group_id(peer) if _HAS_NORMALIZE else str(int(peer))
            except Exception:
                return str(peer)
    except Exception:
        pass
    try:
        return str(chat_id)
    except Exception:
        return "0"

import inspect
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


_DEFAULT_TIMEOUT_SECONDS = 20
_MAX_FLOOD_WAIT_RETRIES = 1


def _per_chat_limit(value=None):
    if value is None:
        value = os.getenv("BOT_MODERATION_PER_CHAT_LIMIT", "3")
    try:
        return min(3, max(1, int(value)))
    except (TypeError, ValueError):
        return 3


def flood_wait_seconds(error):
    """مدت FloodWait را بدون وابستگی مستقیم به نسخهٔ SPlusthon استخراج می‌کند."""
    name = error.__class__.__name__.lower()
    text = str(error)
    if "flood" not in name and "wait" not in name and "wait of" not in text.lower():
        return None
    for attribute in ("seconds", "value", "wait_seconds"):
        value = getattr(error, attribute, None)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    match = re.search(r"(?:wait of|wait)\s+(\d+(?:\.\d+)?)\s+seconds", text, re.I)
    return float(match.group(1)) if match else None


@dataclass
class ModerationJob:
    action: str
    user_id: object
    operation: Callable[[], Awaitable]
    enqueued_at: float
    timeout_seconds: float
    on_success: Optional[Callable[[object], Awaitable]] = None
    on_failure: Optional[Callable[[BaseException], Awaitable]] = None


class ModerationQueue:
    """Moderation مستقل کاربران را محدود و هم‌زمان اجرا می‌کند.

    حداکثر سه کاربر از یک گروه هم‌زمان‌اند؛ کارهای یک کاربر همچنان سریال
    می‌مانند. Governor سقف سراسری اتصال را جداگانه حفظ می‌کند.
    """

    def __init__(self, logger, *, per_chat_limit=None):
        self.logger = logger
        self.per_chat_limit = _per_chat_limit(per_chat_limit)
        self._queues = {}
        # chat key -> list[Task]
        self._workers = {}
        self._user_locks = {}
        self._pending_keys = set()
        # Automatic spam bans are low-value background work compared with a
        # human admin's mute. Serialise them globally so many noisy groups
        # cannot saturate the one shared Soroush connection.
        self._automatic_actions = asyncio.Semaphore(1)
        self._closed = False
        self._sequence = 0
        self._completed = 0
        self._queue_wait_total_ms = 0.0
        self._rpc_total_ms = 0.0
        if self.logger is not None:
            self.logger.log_info(
                "MODERATION QUEUE READY "
                f"per_chat_limit={self.per_chat_limit}"
            )

    def enqueue(
        self,
        chat_id,
        action,
        operation,
        *,
        user_id=None,
        timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        on_success=None,
        on_failure=None,
    ):
        """job را فوری ثبت می‌کند؛ هرگز منتظر RPC نمی‌ماند.

        مقدار False یعنی همان action برای همان کاربر از قبل در صف یا در حال اجراست.
        """
        if self._closed:
            raise RuntimeError("moderation queue is closed")
        key = (_chat_key(chat_id), user_id, action)
        if key in self._pending_keys:
            self.logger.log_info(
                "MODERATION QUEUE DUPLICATE SKIPPED "
                f"chat_id={chat_id} action={action} user_id={user_id}"
            )
            return False

        k = _chat_key(chat_id)
        queue = self._queues.get(k)
        if queue is None:
            queue = asyncio.PriorityQueue()
            self._queues[k] = queue
        self._pending_keys.add(key)
        job = ModerationJob(
            action=action,
            user_id=user_id,
            operation=operation,
            enqueued_at=time.perf_counter(),
            timeout_seconds=float(timeout_seconds),
            on_success=on_success,
            on_failure=on_failure,
        )
        # Manual commands use ``mute``/``unmute``/lock actions and must jump
        # ahead of automatic spam punishments already queued for this chat.
        # An RPC already in flight is never cancelled, but no new automatic
        # ban may start before the administrator's urgent action.
        priority = 0 if action in {"mute", "unmute", "lock", "unlock"} else 1
        self._sequence += 1
        queue.put_nowait((priority, self._sequence, job))
        workers = self._workers.get(k)
        if workers is None:
            workers = []
            self._workers[k] = workers
        workers[:] = [worker for worker in workers if not worker.done()]
        self.logger.log_info(
            "MODERATION QUEUE ENQUEUED "
            f"chat_id={chat_id} action={action} user_id={user_id} "
            f"pending={queue.qsize()} active_workers={len(workers)} "
            f"per_chat_limit={self.per_chat_limit}"
        )
        # One new worker per accepted job, up to the strict per-chat cap.
        # This removes the old 1–2 second wait behind an unrelated user while
        # never allowing one noisy group to consume the whole connection.
        if len(workers) < self.per_chat_limit:
            workers.append(asyncio.create_task(
                self._worker(chat_id, queue)
            ))
        return True

    def _start_worker_if_needed(self, chat_id, queue):
        key = _chat_key(chat_id)
        workers = self._workers.get(key)
        if workers is None:
            workers = []
            self._workers[key] = workers
        workers[:] = [worker for worker in workers if not worker.done()]
        if queue.empty() or len(workers) >= self.per_chat_limit:
            return False
        workers.append(asyncio.create_task(self._worker(chat_id, queue)))
        return True

    async def _worker(self, chat_id, queue):
        self.logger.log_info(f"MODERATION WORKER START chat_id={chat_id}")
        key = _chat_key(chat_id)
        try:
            while True:
                try:
                    _priority, _sequence, job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                # Different users may run concurrently, but two actions for
                # the same user must retain the old serialization guarantee.
                user_key = (key, str(job.user_id))
                user_lock = self._user_locks.get(user_key)
                if user_lock is None:
                    user_lock = self._user_locks[user_key] = asyncio.Lock()
                try:
                    async with user_lock:
                        await self._execute_job(chat_id, job)
                finally:
                    self._pending_keys.discard(
                        (key, job.user_id, job.action)
                    )
                    queue.task_done()
                    if (
                        not user_lock.locked()
                        and not any(
                            pending[0] == key and str(pending[1]) == str(job.user_id)
                            for pending in self._pending_keys
                        )
                    ):
                        self._user_locks.pop(user_key, None)
        finally:
            workers = self._workers.get(key, [])
            current = asyncio.current_task()
            workers[:] = [
                worker for worker in workers
                if worker is not current and not worker.done()
            ]
            if not workers:
                self._workers.pop(key, None)
            # Close the enqueue/worker-exit race: if a job arrived after this
            # worker observed an empty queue, immediately hand it a new worker.
            if not self._closed and not queue.empty():
                self._start_worker_if_needed(chat_id, queue)
            elif queue.empty() and key not in self._workers:
                self._queues.pop(key, None)

    async def _execute_job(self, chat_id, job):
        rpc_started_at = time.perf_counter()
        queue_wait_ms = (rpc_started_at - job.enqueued_at) * 1000
        try:
            from modules.observability import MetricsCollector
            MetricsCollector.get_instance().record_queue_wait("moderation_queue", queue_wait_ms)
        except Exception:
            pass
        started_wall = time.time()
        result = "failed"
        try:
            self.logger.log_info(
                "MODERATION RPC START "
                f"chat_id={chat_id} action={job.action} user_id={job.user_id} "
                f"queue_wait_ms={queue_wait_ms:.2f} rpc_started_at={started_wall:.3f}"
            )
            if job.action in {"ban", "punish", "kick", "auto_mute"}:
                async with self._automatic_actions:
                    value = await self._run_job(chat_id, job)
            else:
                value = await self._run_job(chat_id, job)
            if value is False:
                raise RuntimeError("moderation operation returned False")
            result = "success"
            if job.on_success:
                # Cleanup/notifications can involve slow delete/send RPCs.
                asyncio.create_task(
                    self._run_callback(
                        job.on_success, value, chat_id, job.action
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as caught:
            result = (
                "timeout" if isinstance(caught, asyncio.TimeoutError)
                else "failed"
            )
            if job.on_failure:
                asyncio.create_task(
                    self._run_callback(
                        job.on_failure, caught, chat_id, job.action
                    )
                )
            self.logger.log_error(
                "MODERATION RPC FAILED "
                f"chat_id={chat_id} action={job.action} user_id={job.user_id} "
                f"error={caught!r}"
            )
        finally:
            finished_wall = time.time()
            rpc_ms = (time.perf_counter() - rpc_started_at) * 1000
            self._record_completed(queue_wait_ms, rpc_ms)
            self.logger.log_info(
                "MODERATION RPC FINISHED "
                f"chat_id={chat_id} action={job.action} user_id={job.user_id} "
                f"queue_wait_ms={queue_wait_ms:.2f} "
                f"rpc_started_at={started_wall:.3f} "
                f"rpc_finished_at={finished_wall:.3f} "
                f"rpc_ms={rpc_ms:.2f} result={result} "
                f"avg_queue_wait_ms={self._queue_wait_total_ms / self._completed:.2f} "
                f"avg_rpc_ms={self._rpc_total_ms / self._completed:.2f}"
            )

    async def _run_job(self, chat_id, job):
        """deadline هر کوشش و تنها یک retry پس از FloodWait در همان worker."""
        for attempt in range(_MAX_FLOOD_WAIT_RETRIES + 1):
            try:
                # Ban/mute RPCs often contain entity resolution plus two
                # permission calls; the old 20s outer deadline cancelled a
                # still-running punishment and left only the warning.
                if job.action in {"mute", "auto_mute"}:
                    # A manual mute needs a bounded answer, not a 45-second
                    # hung RPC that blocks the administrator's next command.
                    deadline = min(15.0, max(5.0, job.timeout_seconds))
                elif job.action in {"punish", "ban"}:
                    deadline = max(job.timeout_seconds, 20.0)
                else:
                    deadline = job.timeout_seconds
                # This worker is created from a command dispatcher task and
                # inherits its contextvars.  Never let that inheritance turn
                # entity/admin reads into P0 critical RPCs; only the TL
                # EditBanned request itself is intrinsically critical.
                try:
                    from modules.outgoing_sender import DISPATCH_ACTIVE_VAR
                    token = DISPATCH_ACTIVE_VAR.set(False)
                except Exception:
                    DISPATCH_ACTIVE_VAR = None
                    token = None
                try:
                    return await asyncio.wait_for(job.operation(), timeout=deadline)
                finally:
                    if DISPATCH_ACTIVE_VAR is not None:
                        DISPATCH_ACTIVE_VAR.reset(token)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                wait_seconds = flood_wait_seconds(error)
                # One shared cap across all layers (rpc_resilience retries
                # first; this is only the final, per-worker safety net).
                from modules.rpc_policy import RpcPolicy
                cap = RpcPolicy.from_environment().flood_wait_cap_s
                if (wait_seconds is None
                        or attempt >= _MAX_FLOOD_WAIT_RETRIES
                        or wait_seconds > cap):
                    raise
                self.logger.log_info(
                    "MODERATION FLOOD WAIT "
                    f"chat_id={chat_id} action={job.action} user_id={job.user_id} "
                    f"wait_seconds={wait_seconds:.2f} attempt={attempt + 1}"
                )
                # این sleep فقط worker همین گروه را متوقف می‌کند.
                await asyncio.sleep(wait_seconds)
        raise RuntimeError("unreachable moderation retry state")

    def _record_completed(self, queue_wait_ms, rpc_ms):
        self._completed += 1
        self._queue_wait_total_ms += queue_wait_ms
        self._rpc_total_ms += rpc_ms

    async def _run_callback(self, callback, argument, chat_id, action):
        # Completion callbacks send cosmetic notices/history updates. They are
        # created by a worker that originally inherited an admin dispatcher
        # context; clear it so those sends are P2, never P0 beside manual mute.
        try:
            from modules.outgoing_sender import DISPATCH_ACTIVE_VAR, _SEND_PRIORITY
            # Let patched event.reply enqueue this callback's cosmetic output
            # as a normal P2 job; it must not issue a direct RPC here.
            dispatch_token = DISPATCH_ACTIVE_VAR.set(2)
            send_token = _SEND_PRIORITY.set(1)
        except Exception:
            DISPATCH_ACTIVE_VAR = _SEND_PRIORITY = None
            dispatch_token = send_token = None
        try:
            result = callback(argument)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=10)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.logger.log_error(
                "MODERATION QUEUE CALLBACK FAILED "
                f"chat_id={chat_id} action={action} error={error!r}"
            )
        finally:
            if _SEND_PRIORITY is not None:
                _SEND_PRIORITY.reset(send_token)
            if DISPATCH_ACTIVE_VAR is not None:
                DISPATCH_ACTIVE_VAR.reset(dispatch_token)

    async def close(self):
        self._closed = True
        workers = [
            worker
            for group in self._workers.values()
            for worker in group
        ]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._queues.clear()
        self._user_locks.clear()
        self._pending_keys.clear()
