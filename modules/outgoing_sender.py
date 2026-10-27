"""Outgoing sender queue - decouples GroupDispatcher workers from RPC.

GroupDispatcher workers must never `await send_message` / `reply`:
they only enqueue a send request and free immediately for the next
normal message.  This queue has its own per-chat workers, completely
separate from `MessageDeleteQueue` (deletes) and `ModerationQueue`
(bans/mutes) and `NoticeCleanup` (expiry deletes), so a heavy
delete/cleanup burst cannot head-block a short user reply.

All `client.send_message` and `event.reply` that happen inside a
GroupDispatcher worker are automatically enqueued when this module is
installed via :func:`install`.  Outside a worker (e.g. in
ModerationQueue callbacks, NoticeCleanup, reminder loops) the original
RPC is executed directly.

Priority: 0 = admin/moderation/urgent, 1 = normal chat.
"""
import asyncio
import contextvars
import inspect
import os
import time

from modules.rpc_governor import RpcGovernor, classify_request, is_keepalive_request
from modules.rpc_resilience import install_rpc_resilience

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
    """Return a hashable key for chat_id. Never use InputPeer directly as dict key."""
    if chat_id is None:
        return "0"
    # Try to get numeric peer id via attributes (InputPeerChannel etc.)
    # InputPeerChannel is unhashable, so we must extract its id.
    for attr in ("channel_id", "chat_id", "user_id", "id"):
        try:
            val = getattr(chat_id, attr, None)
            if isinstance(val, int):
                # Use normalize to handle -100... and channel offset consistently
                if _HAS_NORMALIZE:
                    return normalize_group_id(val)
                return str(val)
            if val is not None:
                # Try int conversion
                try:
                    ival = int(val)
                    if _HAS_NORMALIZE:
                        return normalize_group_id(ival)
                    return str(ival)
                except Exception:
                    return str(val)
        except Exception:
            continue
    # Try direct int conversion
    try:
        ival = int(chat_id)
        if _HAS_NORMALIZE:
            return normalize_group_id(ival)
        return str(ival)
    except Exception:
        pass
    # Fallback: try get_peer_id via utils if available
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
    # Last resort: string representation (always hashable)
    try:
        return str(chat_id)
    except Exception:
        return "0"

def _key_for_dict(chat_id):
    # Alias for clarity
    return _chat_key(chat_id)

# Set while a GroupDispatcher worker is running its factory.
# Value is priority (0=admin, 1=command, 2=normal) or False when not in dispatch.
# The patched send_message/reply uses this to decide enqueue vs direct and to set send priority.
_DISPATCH_ACTIVE = contextvars.ContextVar("outgoing_dispatch_active", default=False)
# Priority of the currently executing OutgoingSender job.  The low-level
# ``_call`` gate cannot otherwise tell an urgent priority-0 moderation notice
# from an ordinary SendMessageRequest.
_SEND_PRIORITY = contextvars.ContextVar("outgoing_send_priority", default=None)
# Which send lane owns the current job.  Cosmetic auto-notices are a separate
# lane: their own per-chat queue, their own gate and their own governor bucket.
# The low-level _call wrapper reads this to pick the right gate/bucket.
_SEND_CHANNEL = contextvars.ContextVar("outgoing_send_channel", default="main")

# Each client gets its own OutgoingSender via client._outgoing_sender
_SENDER_ATTR = "_outgoing_sender"
_PATCHED_ATTR = "_outgoing_sender_patched"

# Per-chat gate for low-priority sends to prevent sender_pending explosion per group.
# Each chat has its own gate with limit 2, so 32+ groups do not share a global lock.
# High-priority (admin/moderation, priority 0) bypasses the gate.
class _LowGate:
    """Small FIFO semaphore used only by ordinary sends of one chat."""

    def __init__(self, limit=1):
        self.limit = int(limit)
        self.inflight = 0
        self._waiters = []

    async def acquire(self):
        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        if self.inflight < self.limit and not self._waiters:
            self.inflight += 1
            return (time.perf_counter() - started) * 1000

        future = loop.create_future()
        self._waiters.append(future)
        try:
            # release() transfers an existing slot to this waiter, so the
            # resumed task must not increment inflight a second time.
            await future
        except asyncio.CancelledError:
            if future in self._waiters:
                self._waiters.remove(future)
            elif future.done() and not future.cancelled():
                # Cancellation landed after slot transfer but before resume.
                self.release()
            raise
        return (time.perf_counter() - started) * 1000

    def release(self):
        while self._waiters:
            future = self._waiters.pop(0)
            if future.done():
                continue
            # Keep inflight unchanged: the slot moves to this waiter.
            future.set_result(True)
            return
        if self.inflight > 0:
            self.inflight -= 1

    def idle(self):
        return self.inflight == 0 and not self._waiters


# Per-chat gates: chat_key -> _LowGate, so no global lock between groups.
# Idle gates are removed after use to prevent one permanent dict row per chat.
_CHAT_GATES = {}
def _gate_for_chat(chat_id):
    key = _chat_key(chat_id)
    gate = _CHAT_GATES.get(key)
    if gate is None:
        gate = _LowGate(limit=2)
        _CHAT_GATES[key] = gate
    return gate


def _release_chat_gate(chat_id, gate):
    gate.release()
    key = _chat_key(chat_id)
    if gate.idle() and _CHAT_GATES.get(key) is gate:
        _CHAT_GATES.pop(key, None)


# Dedicated gate table for cosmetic notices.  A stuck FloodWait on a notice
# can only occupy its own single slot; it can never consume either of the two
# ordinary-send slots of that chat.
_NOTICE_GATES = {}
_NOTICE_GATE_LIMIT = 1


def _gate_for_notice(chat_id):
    key = _chat_key(chat_id)
    gate = _NOTICE_GATES.get(key)
    if gate is None:
        gate = _LowGate(limit=_NOTICE_GATE_LIMIT)
        _NOTICE_GATES[key] = gate
    return gate


def _release_notice_gate(chat_id, gate):
    gate.release()
    key = _chat_key(chat_id)
    if gate.idle() and _NOTICE_GATES.get(key) is gate:
        _NOTICE_GATES.pop(key, None)


class OutgoingSender:
    """Per-chat queue for outgoing sends. Separate from delete queues."""

    def __init__(self, client, logger, *, max_per_chat=30,
                 normal_concurrency=None):
        self.client = client
        self.logger = logger
        self.max_per_chat = max(1, min(30, int(max_per_chat)))
        try:
            self.max_normal_pending = max(
                1, min(40, int(os.getenv("BOT_SEND_GLOBAL_NORMAL_PENDING", "40")))
            )
        except (TypeError, ValueError):
            self.max_normal_pending = 40
        if normal_concurrency is None:
            normal_concurrency = os.getenv(
                "BOT_SEND_NORMAL_WORKERS_PER_CHAT", "2"
            )
        try:
            self.normal_concurrency = min(
                4, max(1, int(normal_concurrency))
            )
        except (TypeError, ValueError):
            self.normal_concurrency = 2
        try:
            self.notice_concurrency = max(1, min(2, int(os.getenv(
                "BOT_SEND_NOTICE_WORKERS_PER_CHAT", "1"))))
        except (TypeError, ValueError):
            self.notice_concurrency = 1
        self.notice_max_per_chat = min(self.max_per_chat, 10)
        # Urgent notifications and normal replies remain separate. Normal
        # sends use both slots already allowed by the low-level per-chat gate;
        # the former single worker left the second safe slot idle.
        self._queues = {}
        self._workers = {}  # (chat_key, kind) -> list[Task]
        self._seq = 0
        self._closed = False
        self.stats = {"enqueued": 0, "sent": 0, "failed": 0, "dropped": 0}
        if self.logger:
            self.logger.log_info(
                "OUTGOING SENDER READY "
                f"normal_workers_per_chat={self.normal_concurrency} "
                "notification_workers_per_chat=1"
            )

    def _queue_key(self, chat_id, priority, channel="main"):
        try:
            pri = int(priority)
        except Exception:
            pri = 1
        if channel == "notice":
            return (_chat_key(chat_id), "notice")
        if pri <= 0:
            kind = "notif"
        elif pri == 1:
            kind = "command"
        else:
            kind = "normal"
        return (_chat_key(chat_id), kind)

    def _normal_pending(self):
        return sum(queue.qsize() for (_chat, kind), queue in self._queues.items()
                   if kind in ("command", "normal"))

    def _queue_for(self, chat_id, priority=1, channel="main"):
        key = self._queue_key(chat_id, priority, channel)
        q = self._queues.get(key)
        if q is None:
            q = asyncio.PriorityQueue()
            self._queues[key] = q
        return q

    def _worker_limit(self, qkey):
        if qkey[1] == "notice":
            return self.notice_concurrency
        return 1 if qkey[1] == "notif" else self.normal_concurrency

    def _start_worker_if_needed(self, qkey, chat_id, queue):
        workers = self._workers.get(qkey)
        if workers is None:
            workers = []
            self._workers[qkey] = workers
        workers[:] = [worker for worker in workers if not worker.done()]
        if queue.empty() or len(workers) >= self._worker_limit(qkey):
            return False
        workers.append(asyncio.create_task(
            self._worker(qkey, chat_id, queue)
        ))
        return True

    def enqueue(self, chat_id, coro_factory, *, priority=1, on_done=None,
                channel="main"):
        """Enqueue a send. Never awaits. Returns True if accepted.

        ``channel="notice"`` is the dedicated cosmetic-notice lane; callers
        should use :meth:`enqueue_notice` / :meth:`enqueue_reply_notice`.
        """
        if self._closed:
            return False
        if chat_id is None:
            chat_id = 0
        # Normalize priority: 0 urgent/admin, 1 command, 2 normal/game
        try:
            pri = int(priority)
        except Exception:
            pri = 1
        channel = "notice" if channel == "notice" else "main"
        qkey = self._queue_key(chat_id, pri, channel)
        # Background/game sends (pri >= 2) are shed when normal backlog is saturated
        if pri >= 2 and self._normal_pending() >= self.max_normal_pending:
            self.stats["dropped"] += 1
            try:
                from modules.observability import MetricsCollector
                MetricsCollector.get_instance().record_overflow("outgoing_sender_shed", chat_id)
            except Exception:
                pass
            if self.logger:
                self.logger.log_info(f"OUTGOING SEND SHED chat_id={chat_id} pri={pri} pending={self._normal_pending()}")
            return False
        q = self._queue_for(chat_id, pri, channel)
        queue_cap = (
            self.notice_max_per_chat
            if qkey[1] == "notice" else self.max_per_chat
        )
        if q.qsize() >= queue_cap:
            self.stats["dropped"] += 1
            try:
                from modules.observability import MetricsCollector
                MetricsCollector.get_instance().record_overflow("outgoing_sender_drop", chat_id)
            except Exception:
                pass
            if self.logger:
                self.logger.log_info(f"OUTGOING SEND DROP chat_id={chat_id} qsize={q.qsize()} kind={qkey[1]}")
            return False
        self._seq += 1
        q.put_nowait((pri, self._seq, chat_id, coro_factory, on_done,
                      time.perf_counter(), channel))
        self.stats["enqueued"] += 1
        self._start_worker_if_needed(qkey, chat_id, q)
        return True

    def enqueue_notice(self, chat_id, coro_factory, *, priority=1,
                       on_done=None):
        """Cosmetic auto-notice: own lane, own gate, own governor bucket."""
        return self.enqueue(chat_id, coro_factory, priority=priority,
                            on_done=on_done, channel="notice")

    def enqueue_reply(self, event, text, *, priority=1, on_done=None,
                      channel="main", **kwargs):
        chat_id = getattr(event, "chat_id", None)
        # Capture event and args at enqueue time
        def factory():
            # event.reply may be patched itself, but we call the original
            # via this closure which will be executed inside the sender worker
            # (where DISPATCH_ACTIVE is False), so it will do the real RPC.
            return event.reply(text, **kwargs)
        # Wrap factory to ensure it runs outside dispatch context
        def wrapped():
            token = _DISPATCH_ACTIVE.set(False)
            try:
                return factory()
            finally:
                _DISPATCH_ACTIVE.reset(token)
        return self.enqueue(chat_id, wrapped, priority=priority,
                            on_done=on_done, channel=channel)

    def enqueue_reply_notice(self, event, text, *, priority=1, on_done=None,
                             **kwargs):
        """Convenience wrapper for the many cosmetic moderation notices."""
        return self.enqueue_reply(event, text, priority=priority,
                                  on_done=on_done, channel="notice", **kwargs)

    def enqueue_send(self, chat_id, text, *, priority=1, on_done=None,
                     channel="main", **kwargs):
        def factory():
            return self.client.send_message(chat_id, text, **kwargs)
        def wrapped():
            token = _DISPATCH_ACTIVE.set(False)
            try:
                return factory()
            finally:
                _DISPATCH_ACTIVE.reset(token)
        return self.enqueue(chat_id, wrapped, priority=priority,
                            on_done=on_done, channel=channel)

    async def _worker(self, qkey, chat_id, queue):
        key = qkey
        if self.logger:
            self.logger.log_info(f"OUTGOING SEND WORKER START chat_id={chat_id} key={key} kind={'notif' if 'notif' in str(key) else 'normal'}")
        try:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    # 5-tuple (legacy tests), 6-tuple (chat_id) and 7-tuple
                    # (chat_id + channel) are all accepted.
                    channel = "main"
                    if len(item) == 7:
                        (priority, seq, orig_chat_id, factory, on_done,
                         enqueued_at, channel) = item
                        channel = "notice" if channel == "notice" else "main"
                    elif len(item) == 6:
                        priority, seq, orig_chat_id, factory, on_done, enqueued_at = item
                    else:
                        priority, seq, factory, on_done, enqueued_at = item
                        orig_chat_id = None
                    if len(item) >= 6 and orig_chat_id is not None:
                        # Use orig_chat_id for logging if different from worker's chat_id
                        chat_id = orig_chat_id
                except asyncio.CancelledError:
                    raise
                queue_wait_ms = (time.perf_counter() - enqueued_at) * 1000
                try:
                    from modules.observability import MetricsCollector
                    MetricsCollector.get_instance().record_queue_wait("outgoing_sender", queue_wait_ms)
                except Exception:
                    pass
                if queue_wait_ms >= 50 and self.logger:
                    self.logger.log_info(f"OUTGOING SEND QUEUE_WAIT chat_id={chat_id} queue_wait_ms={queue_wait_ms:.1f} priority={priority}")
                started = time.perf_counter()
                result = None
                # The low-level request wrapper is the single owner of the
                # per-chat RPC gate.  Acquiring the same gate here as well made
                # one normal send consume both slots and forced urgent
                # priority-0 notices to wait behind it.
                gate_wait_ms = 0
                phase_token = None
                try:
                    from modules.outgoing_profiler import add_rpc_phase, begin_rpc_phases
                    phase_token, _phases = begin_rpc_phases("send_message")
                    add_rpc_phase("queue_wait_ms", queue_wait_ms)
                except Exception:
                    phase_token = None
                try:
                    # ⚠️ همیشه خارج از حالت dispatch اجرا شود: worker با
                    # create_task از داخل کانتکست dispatcher ساخته می‌شود و
                    # _DISPATCH_ACTIVE را به ارث می‌برد؛ در نتیجه factoryهای
                    # خامی که client.send_message پچ‌شده را صدا می‌زدند،
                    # دوباره صف می‌شدند و None برمی‌گرداندند — on_done با
                    # sent=None اجرا می‌شد و اعلان هرگز برای پاکسازی ۶۰
                    # ثانیه ثبت نمی‌شد (خطای NOTICE CLEANUP ID MISSING).
                    _token = _DISPATCH_ACTIVE.set(False)
                    _priority_token = _SEND_PRIORITY.set(int(priority))
                    _channel_token = _SEND_CHANNEL.set(channel)
                    try:
                        coro = factory()
                        if inspect.isawaitable(coro):
                            result = await coro
                        else:
                            result = coro
                    finally:
                        _SEND_CHANNEL.reset(_channel_token)
                        _SEND_PRIORITY.reset(_priority_token)
                        _DISPATCH_ACTIVE.reset(_token)
                    self.stats["sent"] += 1
                    if on_done:
                        try:
                            cb = on_done(result)
                            if inspect.isawaitable(cb):
                                await cb
                        except Exception as e:
                            if self.logger:
                                self.logger.log_error(f"OUTGOING SEND on_done FAILED chat_id={chat_id} error={e!r}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.stats["failed"] += 1
                    # Governor rejection is intentional backpressure, not an
                    # operational error worth flooding the terminal with.
                    if self.logger and e.__class__.__name__ != "RpcOverloadError":
                        self.logger.log_error(f"OUTGOING SEND FAILED chat_id={chat_id} error={e!r}")
                finally:
                    queue.task_done()
                    if phase_token is not None:
                        try:
                            from modules.outgoing_profiler import end_rpc_phases
                            end_rpc_phases(phase_token)
                        except Exception:
                            pass
                    if self.logger:
                        elapsed = (time.perf_counter() - started) * 1000
                        if elapsed >= 200:
                            self.logger.log_info(f"OUTGOING SEND TIME chat_id={chat_id} priority={priority} send_ms={elapsed:.1f} gate_wait_ms={gate_wait_ms:.1f}")
                if queue.empty():
                    await asyncio.sleep(0)
                    if queue.empty():
                        return
        finally:
            workers = self._workers.get(qkey, [])
            current = asyncio.current_task()
            workers[:] = [
                worker for worker in workers
                if worker is not current and not worker.done()
            ]
            if not workers:
                self._workers.pop(qkey, None)
            if not self._closed and not queue.empty():
                self._start_worker_if_needed(qkey, chat_id, queue)
            elif queue.empty() and qkey not in self._workers:
                self._queues.pop(qkey, None)

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

    def reset_for_tests(self):
        self._queues.clear()
        self._workers.clear()
        self._seq = 0
        self._closed = False
        for k in self.stats:
            self.stats[k] = 0

def _wrap_send_message(client, sender):
    orig = getattr(client, "send_message", None)
    if orig is None or getattr(orig, _PATCHED_ATTR, False):
        return False
    import functools

    @functools.wraps(orig)
    async def wrapped(*args, **kwargs):
        dispatch_prio = _DISPATCH_ACTIVE.get()
        # dispatch_prio is False when not in dispatch, or 0/1/2 when in dispatch
        if dispatch_prio is not False and dispatch_prio is not None:
            # Inside GroupDispatcher: enqueue and return immediately
            chat_id = kwargs.get("entity") or kwargs.get("chat_id")
            if chat_id is None and args:
                first = args[0]
                if isinstance(first, int):
                    chat_id = first
                else:
                    chat_id = getattr(first, "id", first)
            # Map dispatch priority to send priority: admin(0)->0, command(1)->1, normal(2)->2
            if isinstance(dispatch_prio, int):
                if dispatch_prio <= 0:
                    default_prio = 0
                elif dispatch_prio == 1:
                    default_prio = 1
                else:
                    default_prio = 2
            else:
                default_prio = 1
            priority = kwargs.pop("priority", default_prio)
            on_done = kwargs.pop("on_done", None)
            # Capture args at call time
            _args = args
            _kwargs = kwargs.copy()
            def factory():
                token = _DISPATCH_ACTIVE.set(False)
                try:
                    return orig(*_args, **_kwargs)
                finally:
                    _DISPATCH_ACTIVE.reset(token)
            # Enqueue and return a dummy future that completes immediately after enqueue,
            # so the dispatcher's `await` is only queue time (~0.1ms), not RPC time (150-400ms).
            # For callers that need the sent id (e.g., capture_sent), they should use
            # sender.enqueue with on_done instead of awaiting. But to not break existing
            # code that does `sent = await client.send_message`, we return a future that
            # will be completed with the real sent value *asynchronously*, but the dispatch
            # worker's await will still wait for it. To avoid that, we return None immediately
            # when inside dispatch, and log that sent is not available.
            # This is a trade-off: callers that need sent must be updated to use on_done.
            # For now, we enqueue and return None (or a minimal dummy) so handler doesn't block.
            # The real send will happen in background and capture will be handled via on_done if provided.
            enqueued = sender.enqueue(chat_id, factory, priority=priority, on_done=on_done)
            if not enqueued:
                # Backpressure is intentional: never turn a full background
                # queue into a direct RPC on the dispatcher hot path.
                return None
            # Return a dummy sent that looks like a message id? Many callers ignore the return.
            # For those that do `sent = await ...; capture_sent(..., sent)`, capture will get None
            # and thus not schedule cleanup. To handle that, we need to make those callers use on_done.
            # As a temporary compatibility, we return a small object that will be truthy but not a real id.
            # The caller should be updated to use sender.enqueue with on_done.
            return None
        else:
            return await orig(*args, **kwargs)

    wrapped.__dict__[_PATCHED_ATTR] = True
    try:
        client.send_message = wrapped
        return True
    except (AttributeError, TypeError):
        return False

def _wrap_event_reply(sender):
    # We patch the Event class's reply method globally if possible.
    # Since we don't have the Event class directly, we patch instance method
    # via install_event. For now, we handle per-event patching in instrument_event.
    pass

def install_event_wrapper(event, sender):
    """Patch a single event's reply to be non-blocking inside dispatch."""
    orig = getattr(event, "reply", None)
    if orig is None or getattr(orig, _PATCHED_ATTR, False):
        return False
    import functools

    @functools.wraps(orig)
    async def wrapped(*args, **kwargs):
        dispatch_prio = _DISPATCH_ACTIVE.get()
        if dispatch_prio is not False and dispatch_prio is not None:
            chat_id = getattr(event, "chat_id", None)
            if isinstance(dispatch_prio, int):
                if dispatch_prio <= 0:
                    default_prio = 0
                elif dispatch_prio == 1:
                    default_prio = 1
                else:
                    default_prio = 2
            else:
                default_prio = 1
            priority = kwargs.pop("priority", default_prio)
            on_done = kwargs.pop("on_done", None)
            # Also check for formatting_entities etc. - keep them
            _args = args
            _kwargs = kwargs.copy()
            def factory():
                token = _DISPATCH_ACTIVE.set(False)
                try:
                    return orig(*_args, **_kwargs)
                finally:
                    _DISPATCH_ACTIVE.reset(token)
            # If caller needs on_done (e.g., capture_sent), it should have passed it.
            # We also handle the case where the original call was `sent = await event.reply`
            # and then `capture_sent` - we can auto-handle capture by checking if the
            # text looks like a moderation notice? For now, we just enqueue.
            enqueued = sender.enqueue(chat_id, factory, priority=priority, on_done=on_done)
            if not enqueued:
                return None
            return None
        else:
            return await orig(*args, **kwargs)

    wrapped.__dict__[_PATCHED_ATTR] = True
    try:
        event.reply = wrapped
        return True
    except (AttributeError, TypeError):
        return False

def _wrap_call_with_gate(client, logger, governor=None):
    """Install the outermost per-chat gate and global RPC governor.

    ``orig`` already contains the profiler and the per-request deadline from
    ``RpcPolicy``.  Waiting here therefore cannot consume that deadline. No
    request is dropped: every waiter either receives a permit or is cancelled
    by its own caller.
    """
    orig = getattr(client, "_call", None)
    if orig is None or getattr(orig, "_patched_gate", False):
        return False
    import functools

    _LOW = {
        "SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest",
        "ForwardMessagesRequest", "SendInlineBotResultRequest",
    }

    def _req_name(req):
        try:
            current = req
            seen = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                inner = getattr(current, "query", None)
                if inner is None:
                    break
                current = inner
            return type(current).__name__
        except Exception:
            return type(req).__name__

    def _request_chat(req):
        for attr in ("peer", "channel", "entity", "chat_id"):
            value = getattr(req, attr, None)
            if value is None:
                continue
            for nested in ("channel_id", "chat_id", "user_id", "id"):
                found = getattr(value, nested, None)
                if found is not None:
                    return found
            if isinstance(value, (int, str)):
                return value
        return "global"

    @functools.wraps(orig)
    async def wrapped(sender, request, ordered=False, flood_sleep_threshold=None):
        name = _req_name(request)
        if governor is not None:
            try:
                governor._observe_sender = sender
            except Exception:
                pass
        send_priority = _SEND_PRIORITY.get()
        dispatch_priority = _DISPATCH_ACTIVE.get()
        channel = _SEND_CHANNEL.get() or "main"
        if channel != "notice":
            channel = "main"
        urgent_send = send_priority == 0
        critical_context = bool(
            dispatch_priority is not False
            and dispatch_priority is not None
            and not isinstance(dispatch_priority, bool)
            and int(dispatch_priority) == 0
        )
        # Notice lane always takes its own dedicated single-slot gate.  Main
        # lane keeps the historical priority-0 bypass.  Either way a notice
        # can never take a slot from a user-facing reply.
        is_low = name in _LOW and (
            channel == "notice" or not urgent_send
        )
        chat_for_gate = _request_chat(request)
        gate_wait = 0.0
        gate = None
        gate_acquired = False
        permit = None
        t_call_start = time.perf_counter()
        call_error = False
        phase_token = None
        try:
            from modules.outgoing_profiler import (
                add_rpc_phase,
                begin_rpc_phases,
                current_rpc_phases,
            )
            if current_rpc_phases() is None:
                phase_token, _ = begin_rpc_phases(name)
        except Exception:
            phase_token = None

        try:
            if is_low:
                gate = (
                    _gate_for_notice(chat_for_gate)
                    if channel == "notice"
                    else _gate_for_chat(chat_for_gate)
                )
                gate_wait = await gate.acquire()
                gate_acquired = True
                try:
                    add_rpc_phase("sender_wait_ms", gate_wait)
                except Exception:
                    pass
                if gate_wait >= 20 and logger:
                    logger.log_info(
                        "OUTGOING RPC GATE "
                        f"request={name} wait_ms={gate_wait:.1f} "
                        f"inflight_low={gate.inflight}"
                    )

            if (
                governor is not None
                and (governor.enabled or governor.shadow)
                and not is_keepalive_request(request)
            ):
                admission = classify_request(
                    request,
                    urgent_send=urgent_send,
                    critical_context=critical_context,
                    channel=channel,
                )
                gov_started = time.perf_counter()
                permit = await governor.acquire(admission)
                try:
                    add_rpc_phase(
                        "governor_wait_ms",
                        (time.perf_counter() - gov_started) * 1000.0,
                    )
                except Exception:
                    pass

            try:
                return await orig(
                    sender,
                    request,
                    ordered=ordered,
                    flood_sleep_threshold=flood_sleep_threshold,
                )
            except Exception:
                call_error = True
                raise
        finally:
            call_elapsed_ms = (time.perf_counter() - t_call_start) * 1000
            try:
                from modules.observability import MetricsCollector
                cat_name = "low_send" if is_low else name
                MetricsCollector.get_instance().record_rpc(cat_name, call_elapsed_ms, is_error=call_error)
            except Exception:
                pass
            if permit is not None:
                permit.release()
            if is_low and gate is not None and gate_acquired:
                if channel == "notice":
                    _release_notice_gate(chat_for_gate, gate)
                else:
                    _release_chat_gate(chat_for_gate, gate)
            if phase_token is not None:
                try:
                    from modules.outgoing_profiler import end_rpc_phases
                    end_rpc_phases(phase_token)
                except Exception:
                    pass

    wrapped._patched_gate = True
    wrapped._rpc_governor = governor
    try:
        client._call = wrapped
        return True
    except Exception:
        return False

def install(client, bot, logger=None):
    """Install per-chat sending plus one shared bot-level RPC governor."""
    if getattr(client, _SENDER_ATTR, None) is not None:
        return getattr(client, _SENDER_ATTR)

    effective_logger = logger or getattr(bot, "logger", None)
    governor = getattr(bot, "rpc_governor", None)
    if governor is None:
        governor = RpcGovernor.from_environment(effective_logger)
        try:
            bot.rpc_governor = governor
        except Exception:
            pass
        if effective_logger:
            effective_logger.log_info(
                "RPC GOVERNOR READY "
                f"mode={governor.mode_label()} total={governor.total_limit} "
                f"noncritical={governor.noncritical_limit} "
                f"delete={governor.class_limits['delete']} "
                f"send={governor.class_limits['send']} "
                f"notice={governor.class_limits['notice']} "
                f"heavy={governor.class_limits['heavy']} "
                f"critical_reserve={governor.critical_reserve}"
            )

    sender = OutgoingSender(client, effective_logger)
    try:
        client._outgoing_sender = sender
        bot.outgoing_sender = sender
    except Exception:
        pass
    _wrap_send_message(client, sender)
    _wrap_call_with_gate(client, effective_logger, governor)
    # OUTERMOST wrapper on purpose: on FloodWait it retries by re-entering the
    # gate/governor wrapper, so no permit is held during the backoff sleep.
    controller = install_rpc_resilience(
        client,
        logger=effective_logger,
        policy=getattr(bot, "rpc_policy", None),
        controller=getattr(bot, "rpc_flood_controller", None),
    )
    try:
        bot.rpc_flood_controller = controller
    except Exception:
        pass
    # Also store bot reference for event patching
    try:
        client._outgoing_sender_bot = bot
    except Exception:
        pass
    if effective_logger:
        effective_logger.log_info(
            "OUTGOING SENDER installed (per-chat queues + fair global RPC admission)"
        )
    return sender

def dispatch_active():
    return bool(_DISPATCH_ACTIVE.get())

def set_dispatch_active(active: bool):
    return _DISPATCH_ACTIVE.set(bool(active))

# For GroupDispatcher to use
DISPATCH_ACTIVE_VAR = _DISPATCH_ACTIVE
