"""Per-group asynchronous queue for automatic message deletions."""
import asyncio
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

import time

# Automatic cleanup must never hold the shared Soroush connection for a minute.
_DELETE_RPC_TIMEOUT_SECONDS = 8.0
_AUTOMATIC_DELETE_COOLDOWN_SECONDS = 60.0


def _error_name(error):
    return error.__class__.__name__.lower()


def _error_text(error):
    try:
        return str(error).lower()
    except Exception:
        return ""


def _entity_resolution_error(error):
    """Failures that cannot be repaired by retrying each message ID."""
    name = _error_name(error)
    text = _error_text(error)
    compact_text = text.replace("_", "").replace(" ", "")
    if isinstance(error, (IndexError, KeyError, TypeError, ValueError)):
        return True
    return (
        any(marker in name for marker in (
            "channelprivate", "channelinvalid", "peeridinvalid",
            "entity", "chatidinvalid", "channelpublicgroupna",
        ))
        # A positive Soroush group ID can be misread as a user by
        # SPlusthon. Its implicit GetUsers lookup then returns 404. Retrying
        # the identical delete only repeats that expensive global lookup.
        or (
            "notfound" in name
            and any(request in compact_text for request in (
                "getusersrequest", "getchannelsrequest",
            ))
        )
        or "could not find the input entity" in text
        or "cannot find any entity" in text
        or "list index out of range" in text
    )


def _id_specific_error(error):
    """A batch may contain one bad ID; only these errors justify isolation."""
    name = _error_name(error)
    text = _error_text(error)
    compact_text = text.replace(" ", "_").replace("-", "_")
    return (
        "messageidinvalid" in name
        or "msgidinvalid" in name
        or "message_id_invalid" in compact_text
        or "msg_id_invalid" in compact_text
    )


def _flood_wait_seconds(error):
    name = _error_name(error)
    text = _error_text(error)
    if "flood" not in name and "flood" not in text:
        return None
    value = getattr(error, "seconds", None)
    if value is None:
        value = getattr(error, "value", None)
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return None


class MessageDeleteQueue:
    """Keep delete RPCs out of incoming-message handlers.

    Each chat has an independent priority worker.  There is no global delete
    semaphore: a flood in group A occupies only that group's worker.  Other
    chats keep deleting/replying in parallel.
    """
    def __init__(self, client, logger, *, batch_size=15, max_concurrent=None,
                 inter_batch_delay=0.0, micro_buffer_seconds=0.02, peer_cache=None):
        self.client = client
        self.logger = logger
        self.batch_size = batch_size
        self.inter_batch_delay = inter_batch_delay
        self.micro_buffer_seconds = float(micro_buffer_seconds)
        # Shared bot cache of resolved Soroush InputPeers. Keeping the stable
        # numeric chat ID as the queue key still isolates groups, while the RPC
        # uses the access-hash-bearing peer and skips implicit GetUsers lookup.
        self.peer_cache = peer_cache
        self._queues = {}
        self._workers = {}
        self._pending_ids = set()
        # A failing Soroush delete endpoint can retain calls for tens of
        # seconds. Pause only automatic cleanup after that for the failing chat;
        # other chats continue normally, and manual priority-0 deletes remain
        # available to administrators.
        self._automatic_pause_until = {}
        # Kept only so older callers that pass max_concurrent still construct.
        self._rpc_slots = None
        self._seq = 0

    def cleanup_expired(self, now=None):
        now = time.monotonic() if now is None else now
        self._automatic_pause_until = {
            k: v for k, v in self._automatic_pause_until.items() if v > now
        }

    async def close(self):
        workers = [w for w in self._workers.values() if w is not None and not w.done()]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._queues.clear()
        self._pending_ids.clear()

    def _cached_rpc_peer(self, chat_id):
        cache = self.peer_cache
        if not cache:
            return None
        try:
            direct = cache.get(chat_id)
        except Exception:
            direct = None
        if direct is not None:
            return direct
        wanted = _chat_key(chat_id)
        try:
            rows = list(cache.items())
        except Exception:
            return None
        for cached_id, peer in rows:
            if peer is not None and _chat_key(cached_id) == wanted:
                return peer
        return None

    def enqueue(self, chat_id, message_ids, *, priority=1, rpc_peer=None):
        """Schedule unique IDs and return a Future of ``(deleted, remaining)``.

        ``priority=0`` is for admin/manual deletes and jumps ahead of automatic
        spam/link cleanup in the same chat. ``rpc_peer`` may carry a resolved
        InputPeer while ``chat_id`` remains the stable per-chat queue key.
        """
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 1
        requested_ids = [message_id for message_id in message_ids
                         if isinstance(message_id, int) and message_id > 0]
        loop = asyncio.get_running_loop()
        k_chat = _chat_key(chat_id)
        pause_until = self._automatic_pause_until.get(k_chat, 0.0)
        if priority > 0 and time.monotonic() < pause_until:
            result = loop.create_future()
            result.set_result((0, requested_ids))
            return result
        ids = []
        for message_id in requested_ids:
            if not isinstance(message_id, int) or message_id <= 0:
                continue
            key = (_chat_key(chat_id), message_id)
            if key in self._pending_ids:
                continue
            self._pending_ids.add(key)
            ids.append(message_id)
        result = loop.create_future()
        if not ids:
            result.set_result((0, []))
            return result

        if rpc_peer is None:
            rpc_peer = self._cached_rpc_peer(chat_id)

        queue = self._queues.get(_chat_key(chat_id))
        if queue is None:
            queue = asyncio.PriorityQueue()
            self._queues[_chat_key(chat_id)] = queue
        self._seq += 1
        queue.put_nowait((
            int(priority), self._seq, ids, result, time.perf_counter(), rpc_peer,
        ))
        if queue.qsize() > 1 or int(priority) == 0:
            self.logger.log_info(
                "DELETE QUEUE SIZE "
                f"chat_id={chat_id} queued_ids={len(ids)} pending={queue.qsize()} "
                f"priority={priority}"
            )
        worker = self._workers.get(_chat_key(chat_id))
        if worker is None or worker.done():
            self._workers[_chat_key(chat_id)] = asyncio.create_task(
                self._worker(chat_id, queue)
            )
        return result

    async def _delete_ids(self, chat_id, ids):
        deleted = 0
        remaining = []
        try:
            from modules.cache_manager import PermissionCircuitBreaker
            cb = getattr(self, "circuit_breaker", None) or PermissionCircuitBreaker.get_default(self.logger)
        except Exception:
            cb = None

        if cb is not None and not cb.can_execute(chat_id, "delete"):
            if self.logger:
                self.logger.log_info(f"DELETE SKIPPED circuit breaker open for chat_id={chat_id}")
            return 0, list(ids)

        for start in range(0, len(ids), self.batch_size):
            batch = ids[start:start + self.batch_size]
            # Yield so a pending admin reply can take the shared sender
            # before this chat's next delete batch.
            await asyncio.sleep(0)
            self.logger.log_info(
                f"BATCH DELETE START chat_id={chat_id} count={len(batch)}"
            )
            succeeded = False
            last_error = None
            # A failed automatic delete is disposable; retrying it for tens of
            # seconds blocks every command behind the shared sender.
            for attempt in range(1, 3):
                started = time.perf_counter()
                try:
                    await asyncio.wait_for(
                        self.client.delete_messages(chat_id, batch),
                        timeout=_DELETE_RPC_TIMEOUT_SECONDS,
                    )
                    deleted += len(batch)
                    succeeded = True
                    k_chat = _chat_key(chat_id)
                    self._automatic_pause_until.pop(k_chat, None)
                    if cb is not None:
                        cb.record_success(chat_id)
                    self.logger.log_info(
                        "DELETE MESSAGE RPC TIME "
                        f"chat_id={chat_id} count={len(batch)} attempt={attempt} "
                        f"rpc_ms={(time.perf_counter() - started) * 1000:.2f}"
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    last_error = error
                    err_name = error.__class__.__name__.lower()
                    from modules.cache_manager import is_permission_error
                    if is_permission_error(error) or "admin" in err_name or "permission" in err_name:
                        if cb is not None:
                            cb.record_failure(chat_id, error)
                        k_chat = _chat_key(chat_id)
                        self._automatic_pause_until[k_chat] = max(
                            self._automatic_pause_until.get(k_chat, 0.0),
                            time.monotonic() + _AUTOMATIC_DELETE_COOLDOWN_SECONDS,
                        )
                    self.logger.log_error(
                        "DELETE MESSAGE RPC FAILED "
                        f"chat_id={chat_id} count={len(batch)} attempt={attempt} "
                        f"error={error!r}"
                    )
                    # Entity/channel resolution errors affect the whole RPC.
                    # Retrying the batch and then every ID turned one IndexError
                    # into hundreds of useless requests under a spam wave.
                    if _entity_resolution_error(error):
                        k_chat = _chat_key(chat_id)
                        self._automatic_pause_until[k_chat] = max(
                            self._automatic_pause_until.get(k_chat, 0.0),
                            time.monotonic() + _AUTOMATIC_DELETE_COOLDOWN_SECONDS,
                        )
                        break
                    # Retrying the unchanged batch cannot repair one invalid
                    # message ID; go directly to the one-pass isolation below.
                    if _id_specific_error(error):
                        break
                    flood_wait = _flood_wait_seconds(error)
                    if flood_wait is not None:
                        if flood_wait <= 3.0 and attempt == 1:
                            await asyncio.sleep(flood_wait)
                            continue
                        break
                    if isinstance(error, (TimeoutError, asyncio.TimeoutError, ConnectionError)) and attempt == 1:
                        await asyncio.sleep(0.05)
                        continue
                    break
            if succeeded:
                self.logger.log_info(
                    f"BATCH DELETE FINISHED chat_id={chat_id} count={len(batch)}"
                )
                if self.inter_batch_delay:
                    await asyncio.sleep(self.inter_batch_delay)
                else:
                    await asyncio.sleep(0)
                continue

            # When a batch fails completely across all retry attempts, pause automatic cleanup for this chat
            k_chat = _chat_key(chat_id)
            self._automatic_pause_until[k_chat] = max(
                self._automatic_pause_until.get(k_chat, 0.0),
                time.monotonic() + _AUTOMATIC_DELETE_COOLDOWN_SECONDS,
            )

            # Only an ID-specific server error justifies splitting a failed
            # batch. Connection/entity/flood errors would fail every item and
            # create maximum pressure precisely when the connection is weak.
            if last_error is None or not _id_specific_error(last_error):
                remaining.extend(batch)
                self.logger.log_error(
                    "DELETE BATCH UNRESOLVED "
                    f"chat_id={chat_id} count={len(batch)} "
                    f"error={last_error!r} isolation_skipped=True"
                )
                continue

            for message_id in batch:
                item_ok = False
                item_error = None
                # One individual call identifies the invalid ID. Transient
                # errors were already retried at batch level.
                started = time.perf_counter()
                try:
                    await asyncio.wait_for(
                        self.client.delete_messages(chat_id, [message_id]),
                        timeout=_DELETE_RPC_TIMEOUT_SECONDS,
                    )
                    deleted += 1
                    item_ok = True
                    self.logger.log_info(
                        "DELETE MESSAGE RPC TIME "
                        f"chat_id={chat_id} count=1 attempt=1 "
                        f"rpc_ms={(time.perf_counter() - started) * 1000:.2f}"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    item_error = error
                    k_chat = _chat_key(chat_id)
                    self._automatic_pause_until[k_chat] = max(
                        self._automatic_pause_until.get(k_chat, 0.0),
                        time.monotonic() + _AUTOMATIC_DELETE_COOLDOWN_SECONDS,
                    )
                if not item_ok:
                    remaining.append(message_id)
                    self.logger.log_error(
                        "DELETE MESSAGE UNRESOLVED "
                        f"chat_id={chat_id} message_id={message_id} "
                        f"error={item_error!r}"
                    )
        return deleted, remaining

    async def _worker(self, chat_id, queue):
        self.logger.log_info(f"DELETE TASK START chat_id={chat_id}")

        def _unpack(item):
            rpc_peer = None
            if len(item) == 6:
                _priority, _seq, ids, result, enqueued_at, rpc_peer = item
            elif len(item) == 5:
                _priority, _seq, ids, result, enqueued_at = item
            else:
                _priority, _seq, ids, result = item
                enqueued_at = time.perf_counter()
            return _priority, ids, result, enqueued_at, rpc_peer

        # 🧲 سقف ادغام در هر چرخه: ۱۰ batch پانزده‌تایی.
        merge_cap = max(self.batch_size * 10, self.batch_size)

        try:
            while True:
                item = await queue.get()
                jobs = [_unpack(item)]
                merged = len(jobs[0][1])

                # Micro-buffering window (50ms - 80ms) for automatic spam deletes (priority > 0).
                # Priority 0 (admin manual deletes) bypasses this to execute immediately with 0 delay.
                if jobs[0][0] > 0 and self.micro_buffer_seconds > 0:
                    await asyncio.sleep(self.micro_buffer_seconds)

                # ادغام کارهای حذفِ همین گروه که الان در صف منتظرند:
                # قبلاً هر کارِ تک‌پیامی یک RPC جدا (~۲۰۰-۴۰۰ms) می‌خورد و
                # با سیل کارهای ids=1، صفِ حذفِ گروه ده‌ها ثانیه عقب
                # می‌افتاد (queue_wait_ms=32000 در لاگ) و دستور «پاک» هم
                # پشت همان صف می‌ماند. حالا صد کار تکی در چند RPC
                # پانزده‌تایی یک‌جا حذف می‌شوند.
                while merged < merge_cap:
                    try:
                        extra = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    job = _unpack(extra)
                    jobs.append(job)
                    merged += len(job[1])

                oldest = min(job[3] for job in jobs)
                queue_wait_ms = (time.perf_counter() - oldest) * 1000
                try:
                    from modules.observability import MetricsCollector
                    MetricsCollector.get_instance().record_queue_wait("delete_queue", queue_wait_ms)
                except Exception:
                    pass
                if queue_wait_ms >= 50:
                    self.logger.log_info(
                        "QUEUE WAIT TIME "
                        f"chat_id={chat_id} lane=delete priority={jobs[0][0]} "
                        f"queue_wait_ms={queue_wait_ms:.1f} "
                        f"ids={merged} merged_jobs={len(jobs)}"
                    )

                all_ids = list(dict.fromkeys(
                    message_id for _p, ids, _r, _e, _peer in jobs
                    for message_id in ids
                ))
                rpc_target = next(
                    (peer for _p, _ids, _r, _e, peer in reversed(jobs)
                     if peer is not None),
                    None,
                )
                if rpc_target is None:
                    # The event handler may have warmed the shared peer cache
                    # after this job was queued but before its worker ran.
                    cached_peer = self._cached_rpc_peer(chat_id)
                    rpc_target = chat_id if cached_peer is None else cached_peer
                try:
                    deleted, remaining = await self._delete_ids(
                        rpc_target, all_ids)
                    remaining_set = set(remaining or ())
                    for _p, ids, result, _e, _peer in jobs:
                        job_remaining = [
                            i for i in ids if i in remaining_set]
                        if not result.done():
                            result.set_result(
                                (len(ids) - len(job_remaining),
                                 job_remaining))
                except asyncio.CancelledError:
                    for _p, _ids, result, _e, _peer in jobs:
                        if not result.done():
                            result.cancel()
                    raise
                except Exception as error:
                    self.logger.log_error(
                        f"DELETE TASK FAILED chat_id={chat_id} error={error!r}"
                    )
                    for _p, ids, result, _e, _peer in jobs:
                        if not result.done():
                            result.set_result((0, ids))
                finally:
                    for _p, ids, _r, _e, _peer in jobs:
                        for message_id in ids:
                            self._pending_ids.discard(
                                (_chat_key(chat_id), message_id))
                        queue.task_done()
                if queue.empty():
                    return
        finally:
            k = _chat_key(chat_id)
            try:
                cur_task = asyncio.current_task()
            except RuntimeError:
                cur_task = None
            if cur_task is not None and self._workers.get(k) is cur_task:
                self._workers.pop(k, None)
            if not queue.empty():
                try:
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        self._workers[k] = loop.create_task(
                            self._worker(chat_id, queue)
                        )
                except RuntimeError:
                    pass
            else:
                self._queues.pop(k, None)
