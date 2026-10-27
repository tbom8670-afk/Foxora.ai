"""Instrumentation سبک RPCهای خروجی Soroush Plus، بدون تغییر ترتیب ارسال.

Splits each traced RPC into:

* ``connection_wait_ms`` — queued until the request is written to the wire
* ``rpc_wait_ms`` — wire write until SPlusthon's ``_call`` returns
* ``post_rpc_ms`` — leftover time in the high-level wrapper after ``_call``
* ``total_rpc_ms`` — full high-level or ``_call`` span
"""
import asyncio
import contextvars
import functools
import os
import sys
import time

from modules.rpc_policy import (
    RPC_ATTEMPT,
    RPC_REQUEST_ID,
    RpcPolicy,
    classify_outcome,
    new_request_id,
)


# The single policy object shared by tracing, deadlines and the watchdog.
# ``configure_rpc_policy`` is called once at bot startup (after .env is loaded);
# the lazy fallback keeps standalone tests and tools working.
_POLICY = None


def configure_rpc_policy(policy):
    global _POLICY
    _POLICY = policy or RpcPolicy.from_environment()


def _rpc_policy():
    global _POLICY
    if _POLICY is None:
        _POLICY = RpcPolicy.from_environment()
    return _POLICY


# request_id -> the dedicated inner task created by connection_guard's
# wait_for.  Cancelling THIS task (never the caller's worker task) is safe.
_CANCEL_TARGETS = {}


def attach_cancel_target(request_id, task):
    if request_id:
        _CANCEL_TARGETS[request_id] = task


def detach_cancel_target(request_id):
    if request_id:
        _CANCEL_TARGETS.pop(request_id, None)


def cancel_target(request_id):
    task = _CANCEL_TARGETS.get(request_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


_RESPONSE_RPC_MS = contextvars.ContextVar("response_rpc_ms", default=0.0)
_RPC_DEPTH = contextvars.ContextVar("outgoing_rpc_depth", default=0)
_OP_STATE = contextvars.ContextVar("outgoing_op_state", default=None)
_CALL_STATE = contextvars.ContextVar("outgoing_call_state", default=None)
_RPC_PHASES = contextvars.ContextVar("outgoing_rpc_phases", default=None)
_INFLIGHT_RPCS = {}
_RPC_DEBUG = os.getenv("BOT_RPC_DEBUG", "").strip() == "1"
# 400–800ms is normal successful Soroush server RTT, not a local failure.
_RPC_SLOW_WARNING_MS = float(os.getenv("BOT_RPC_SLOW_WARNING_MS", "1500"))
_RPC_SLOW_MS = float(os.getenv("BOT_RPC_SLOW_MS", "500"))
_RPC_CRITICAL_MS = float(os.getenv("BOT_RPC_CRITICAL_MS", "2000"))

_TRACED_OPS = {
    "send_message",
    "reply",
    "delete_message",
    "ban",
    "mute",
    "moderation",
}

_REQUEST_OPS = {
    "SendMessageRequest": "send_message",
    "DeleteMessagesRequest": "delete_message",
    "EditBannedRequest": "moderation",
    "EditChatDefaultBannedRightsRequest": "moderation",
}

_SYNC_TRACE_REQUESTS = frozenset({
    "GetDifferenceRequest",
    "GetChannelDifferenceRequest",
})
_RECONNECT_GEN = 0
_LAST_RECONNECT = {
    "started_at": None,
    "ended_at": None,
    "elapsed_ms": 0.0,
    "pending_before": (),
    "reason": None,
}
# Missed keepalive pong is not by itself a dead socket. Reconnect only when a
# live RPC has been unanswered for several seconds and no real response has
# arrived recently. A short network delay or a busy GetChannelDifference must
# not tear the websocket down.
PONG_RECONNECT_STUCK_SECONDS = float(os.getenv("BOT_PONG_RECONNECT_STUCK_SECONDS", "4"))
PONG_RECENT_RESPONSE_SECONDS = float(os.getenv("BOT_PONG_RECENT_RESPONSE_SECONDS", "5"))
_LAST_RPC_OK_AT = None
_LAST_RPC_ACTIVITY_AT = None


def begin_response_measurement():
    """برای هر handler یک context مستقلِ زمان پاسخ ایجاد می‌کند."""
    return _RESPONSE_RPC_MS.set(0.0)


def response_rpc_ms():
    return _RESPONSE_RPC_MS.get()


def end_response_measurement(token):
    _RESPONSE_RPC_MS.reset(token)


def mark_rpc_on_wire():
    """Stamp the moment this RPC is actually written to the connection.

    Called from send-path hooks (``connection.send`` / ``writer.drain``)
    and from tests. Does nothing when no ``_call`` is in progress.
    A later write on the same await is a reconnect replay, not a new RPC.
    """
    call = _CALL_STATE.get()
    if call is None:
        return False
    now = time.perf_counter()
    logger = call.get("logger")
    name = call.get("request_name") or "unknown"
    request_id = call.get("request_id") or "-"
    if call.get("send_started") is None:
        call["send_started"] = now
        queued = call.get("queued") or now
        if _should_trace(name):
            _log_conn_trace(
                logger,
                "SOCKET SEND",
                request=name,
                request_id=request_id,
                queue_to_wire_ms=(now - queued) * 1000.0,
            )
        return True
    call["resend_count"] = int(call.get("resend_count") or 0) + 1
    call["last_resend"] = now
    _log_conn_trace(
        logger,
        "SOCKET RESEND",
        request=name,
        request_id=request_id,
        resend_count=call["resend_count"],
        since_first_send_ms=(now - call["send_started"]) * 1000.0,
    )
    return True


def _log_conn_trace(logger, event, **fields):
    if logger is None:
        return
    parts = [f"CONN TRACE {event}"]
    for key, value in fields.items():
        if value is None:
            continue
        if key == "extra":
            parts.append(str(value))
        elif isinstance(value, float):
            parts.append(f"{key}={value:.1f}")
        else:
            parts.append(f"{key}={value}")
    try:
        logger.log_info(" ".join(parts))
    except Exception:
        pass


def _pending_trace(sender, limit=12):
    try:
        from modules.connection_guard import inspect_pending
        rows = inspect_pending(sender)
    except Exception:
        rows = []
    parts = []
    ids = []
    for row in rows[:limit]:
        ids.append(row.get("msg_id"))
        parts.append(
            f"{row.get('request_type')}:msg={row.get('msg_id')}"
            f":age_ms={float(row.get('age_ms') or 0):.0f}"
            f":done={int(bool(row.get('future_done')))}"
        )
    return {
        "count": len(rows),
        "ids": tuple(ids),
        "text": ",".join(parts) if parts else "-",
        "types": ",".join(
            str(row.get("request_type") or "?") for row in rows[:limit]
        ) or "-",
    }


def _snapshot_line(client):
    bot = getattr(client, "_outgoing_sender_bot", None) if client is not None else None
    if bot is None:
        bot = getattr(client, "_bot", None) if client is not None else None
    pending_tasks = 0
    try:
        from modules.runtime_snapshot import collect_sync, _task_counts, _rss_mb, _username_directory_size
        if bot is not None:
            snap = collect_sync(bot)
            return (
                f"pending_tasks={snap.get('pending_tasks')} "
                f"rpc_pending={snap.get('rpc_pending')} "
                f"sender_pending={snap.get('sender_pending')} "
                f"memory_mb={snap.get('memory_mb')} "
                f"username_directory_cache_size={snap.get('username_directory_cache_size')} "
                f"event_loop_lag_ms={snap.get('event_loop_lag_ms')}"
            )
        pending_tasks, _active = _task_counts()
        return (
            f"pending_tasks={pending_tasks} "
            f"rpc_pending={len(_INFLIGHT_RPCS)} "
            f"sender_pending=0 "
            f"memory_mb={_rss_mb():.1f} "
            f"username_directory_cache_size={_username_directory_size()} "
            f"event_loop_lag_ms=0.0"
        )
    except Exception:
        return (
            f"pending_tasks={pending_tasks} rpc_pending={len(_INFLIGHT_RPCS)} "
            "sender_pending=0 memory_mb=0.0 username_directory_cache_size=0 "
            "event_loop_lag_ms=0.0"
        )


def _is_sync_trace(request_name):
    return str(request_name or "") in _SYNC_TRACE_REQUESTS


def _should_trace(request_name):
    if _is_sync_trace(request_name):
        return True
    started = _LAST_RECONNECT.get("started_at")
    if started is not None and (time.perf_counter() - started) < 20.0:
        return True
    return False


def pending_rpc_snapshot(sender=None):
    """In-flight traced RPCs, plus SPlusthon ``_pending_state`` size if present."""
    rows = list(_INFLIGHT_RPCS.values())
    sender_pending = 0
    breakdown = {
        "keepalive": 0,
        "stale": 0,
        "live": 0,
        "oldest_age_ms": 0.0,
        "by_type": {},
        "rows": [],
    }
    if sender is not None:
        pending = getattr(sender, "_pending_state", None) or {}
        try:
            sender_pending = len(pending)
        except TypeError:
            sender_pending = 0
        try:
            from modules.connection_guard import pending_breakdown
            breakdown = pending_breakdown(sender)
            sender_pending = int(breakdown.get("count") or sender_pending)
        except Exception:
            pass
    return {
        "count": len(rows),
        "sender_pending": sender_pending,
        "sender_pending_keepalive": int(breakdown.get("keepalive") or 0),
        "sender_pending_stale": int(breakdown.get("stale") or 0),
        "sender_pending_live": int(breakdown.get("live") or 0),
        "sender_pending_oldest_age_ms": float(breakdown.get("oldest_age_ms") or 0.0),
        "sender_pending_by_type": dict(breakdown.get("by_type") or {}),
        "sender_pending_rows": list(breakdown.get("rows") or ()),
        "request_ids": [row.get("request_id") for row in rows if row.get("request_id")],
        "operations": [row.get("operation") for row in rows if row.get("operation")],
    }


def begin_rpc_phases(operation=None):
    """Start a timing bag that queue / governor / sender / _call all fill."""
    state = {
        "queue_wait_ms": 0.0,
        "governor_wait_ms": 0.0,
        "sender_wait_ms": 0.0,
        "rpc_await_ms": 0.0,
        "total_ms": 0.0,
        "operation": operation,
        "started": time.perf_counter(),
    }
    return _RPC_PHASES.set(state), state


def current_rpc_phases():
    return _RPC_PHASES.get()


def add_rpc_phase(name, milliseconds):
    state = _RPC_PHASES.get()
    if state is None:
        return None
    try:
        state[name] = float(state.get(name, 0.0) or 0.0) + float(milliseconds or 0.0)
    except (TypeError, ValueError):
        pass
    return state


def end_rpc_phases(token):
    if token is not None:
        try:
            _RPC_PHASES.reset(token)
        except Exception:
            pass


def _force_runtime_snapshot(owner, reason):
    bot = owner
    if owner is not None and not hasattr(owner, "runtime_snapshot"):
        bot = getattr(owner, "_outgoing_sender_bot", None)
    monitor = getattr(bot, "runtime_snapshot", None) if bot is not None else None
    request = getattr(monitor, "request_immediate", None)
    if callable(request):
        request(reason)


def _log_rpc_budget(logger, owner, operation, phases, extra=None):
    if logger is None or phases is None:
        return
    queue_wait = float(phases.get("queue_wait_ms") or 0.0)
    governor_wait = float(phases.get("governor_wait_ms") or 0.0)
    sender_wait = float(phases.get("sender_wait_ms") or 0.0)
    rpc_await = float(phases.get("rpc_await_ms") or 0.0)
    total = queue_wait + governor_wait + sender_wait + rpc_await
    phases["total_ms"] = total
    sender = getattr(owner, "_sender", None) if owner is not None else None
    snapshot = pending_rpc_snapshot(sender)
    sender_pending = int(snapshot.get("sender_pending") or 0)
    ping_age = 0.0
    for row in snapshot.get("sender_pending_rows") or ():
        name = str(row.get("request_type") or "")
        if row.get("is_keepalive") or "Ping" in name:
            ping_age = max(ping_age, float(row.get("age_ms") or 0.0))
    type_map = snapshot.get("sender_pending_by_type") or {}
    type_text = ",".join(
        f"{key}:{value}" for key, value in type_map.items() if value
    ) or "-"
    line = (
        f"operation={operation or phases.get('operation') or 'unknown'} "
        f"queue_wait_ms={queue_wait:.1f} "
        f"governor_wait_ms={governor_wait:.1f} "
        f"sender_wait_ms={sender_wait:.1f} "
        f"rpc_await_ms={rpc_await:.1f} "
        f"total_ms={total:.1f} "
        f"sender_pending={sender_pending} "
        f"sender_pending_live={int(snapshot.get('sender_pending_live') or 0)} "
        f"sender_pending_keepalive={int(snapshot.get('sender_pending_keepalive') or 0)} "
        f"ping_age_ms={ping_age:.0f} "
        f"pending_by_type={type_text}"
    )
    if extra:
        line = f"{line} {extra}"
    if total >= _RPC_CRITICAL_MS:
        logger.log_error(f"OUTGOING RPC CRITICAL {line}")
        _force_runtime_snapshot(owner, "rpc_critical")
    elif total >= _RPC_SLOW_MS:
        logger.log_info(f"OUTGOING RPC SLOW {line}")


def _format_request_ids(snapshot):
    ids = snapshot.get("request_ids") or ()
    return ",".join(str(item) for item in ids) if ids else "-"


def _format_operations(snapshot):
    ops = snapshot.get("operations") or ()
    return ",".join(str(item) for item in ops) if ops else "-"


def _note_rpc_activity():
    global _LAST_RPC_ACTIVITY_AT
    _LAST_RPC_ACTIVITY_AT = time.perf_counter()


def _note_rpc_ok():
    global _LAST_RPC_OK_AT, _LAST_RPC_ACTIVITY_AT
    now = time.perf_counter()
    _LAST_RPC_OK_AT = now
    _LAST_RPC_ACTIVITY_AT = now


def _event_loop_is_healthy():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return True
    if loop.is_closed() or not loop.is_running():
        return False
    return True


def _oldest_live_rpc_age_ms(sender, snapshot=None):
    oldest = 0.0
    if snapshot is None:
        snapshot = pending_rpc_snapshot(sender)
    for row in snapshot.get("sender_pending_rows") or ():
        if row.get("is_keepalive") or row.get("future_done"):
            continue
        oldest = max(oldest, float(row.get("age_ms") or 0.0))
    now = time.perf_counter()
    for record in _INFLIGHT_RPCS.values():
        started = record.get("started")
        if started is None:
            continue
        oldest = max(oldest, (now - float(started)) * 1000.0)
    return oldest


def _pong_timeout_decision(sender, snapshot):
    """Whether a missed keepalive pong should tear down the websocket."""
    now = time.perf_counter()
    oldest_ms = _oldest_live_rpc_age_ms(sender, snapshot)
    last_ok = _LAST_RPC_OK_AT
    last_ok_ms = None if last_ok is None else (now - last_ok) * 1000.0
    loop_ok = _event_loop_is_healthy()
    recent_response = (
        last_ok_ms is not None
        and last_ok_ms < (PONG_RECENT_RESPONSE_SECONDS * 1000.0)
    )
    stuck = oldest_ms >= (PONG_RECONNECT_STUCK_SECONDS * 1000.0)
    if not loop_ok:
        return False, "event_loop_unhealthy", oldest_ms, last_ok_ms
    if recent_response:
        return False, "recent_response", oldest_ms, last_ok_ms
    if not stuck:
        return False, "rpc_not_stuck", oldest_ms, last_ok_ms
    return True, "pending_rpc_stuck", oldest_ms, last_ok_ms


def _clear_outstanding_ping(sender):
    try:
        sender._ping = None
    except Exception:
        return False
    return True


def _register_inflight(request, record):
    if request is None:
        return None
    key = id(request)
    record.setdefault("orphan_reported", False)
    _INFLIGHT_RPCS[key] = record
    return key


def _unregister_inflight(key):
    if key is not None:
        _INFLIGHT_RPCS.pop(key, None)


# ---------------------------------------------------------------------------
#  Orphan-await watchdog
# ---------------------------------------------------------------------------
# The guarantee: every "CONN TRACE AWAIT START" is paired with exactly one
# terminal line.  If a terminal never arrives (wrapper missing, a swallowed
# CancelledError, a library bug), this loop reports it AFTER the request's own
# deadline + grace and cancels the inner RPC task.  It never cancels the
# caller's long-lived queue worker.
_WATCHDOG_TASK = None


async def _orphan_watchdog_loop(logger, policy):
    while True:
        await asyncio.sleep(policy.watchdog_interval_s)
        now = time.perf_counter()
        for record in list(_INFLIGHT_RPCS.values()):
            deadline = record.get("deadline")
            if deadline is None or now < deadline or record.get("orphan_reported"):
                continue
            record["orphan_reported"] = True
            request_id = record.get("request_id") or "-"
            age_ms = (now - float(record.get("started") or now)) * 1000.0
            line = (
                f"request={record.get('request') or 'unknown'} "
                f"request_id={request_id} "
                f"operation={record.get('operation') or '-'} "
                f"chat_id={record.get('chat_id')} "
                f"age_ms={age_ms:.0f} "
                f"deadline_s={record.get('timeout_s')} "
                f"inflight={len(_INFLIGHT_RPCS)}"
            )
            _log_conn_trace(logger, "ORPHAN", extra=line)
            try:
                if logger is not None:
                    logger.log_error(f"ORPHAN RPC AWAIT {line}")
            except Exception:
                pass
            cancelled = False
            if policy.watchdog_cancel:
                cancelled = cancel_target(request_id)
            if cancelled:
                _log_conn_trace(
                    logger, "ORPHAN CANCEL",
                    request=record.get("request") or "unknown",
                    request_id=request_id,
                    action="cancel_inner_task",
                )


def start_orphan_watchdog(logger, policy=None):
    """Start one process-wide watchdog task.  Safe to call twice."""
    global _WATCHDOG_TASK
    policy = policy or _rpc_policy()
    configure_rpc_policy(policy)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if (_WATCHDOG_TASK is not None and not _WATCHDOG_TASK.done()
            and _WATCHDOG_TASK.get_loop() is loop):
        return _WATCHDOG_TASK
    _WATCHDOG_TASK = loop.create_task(
        _orphan_watchdog_loop(logger, policy),
        name="rpc-orphan-watchdog",
    )
    return _WATCHDOG_TASK


def stop_orphan_watchdog():
    global _WATCHDOG_TASK
    task = _WATCHDOG_TASK
    _WATCHDOG_TASK = None
    if task is not None and not task.done():
        task.cancel()
    return task


def _chat_id(owner, args, kwargs):
    chat = kwargs.get("entity") or kwargs.get("chat_id")
    if chat is not None:
        return getattr(chat, "id", chat)
    if args and isinstance(args[0], int):
        return args[0]
    # ``delete_messages(InputPeerChannel(...), ids)`` آرگومان اولش یک شیء
    # peer است نه عدد؛ قبلاً اینجا None برمی‌گشت و در لاگِ
    # «OUTGOING RPC WARNING … delete_message» به‌صورت chat_id=None دیده
    # می‌شد. شناسهٔ عددی را از خود peer استخراج می‌کنیم.
    if args:
        first = args[0]
        for attr in ("channel_id", "chat_id", "user_id", "id"):
            try:
                value = getattr(first, attr, None)
            except Exception:
                value = None
            if isinstance(value, int):
                return value
    return getattr(owner, "chat_id", None)


def _request_chat_id(request):
    if request is None:
        return None
    for attr in ("peer", "channel", "entity", "chat_id"):
        value = getattr(request, attr, None)
        if value is None:
            continue
        for nested in ("channel_id", "chat_id", "user_id", "id"):
            found = getattr(value, nested, None)
            if found is not None:
                return found
        if isinstance(value, int):
            return value
    return None


def _request_name(request):
    return type(request).__name__ if request is not None else "unknown"


def _operation_from_request(request):
    return _REQUEST_OPS.get(_request_name(request))


def _extract_request(args, kwargs):
    request = kwargs.get("request")
    if request is not None:
        return request
    if len(args) >= 2 and not isinstance(args[1], (int, str, bytes)):
        return args[1]
    if args and not isinstance(args[0], (int, str, bytes)):
        maybe = args[0]
        # First positional of ``_call`` is usually the sender.
        if len(args) >= 2:
            return args[1]
        name = type(maybe).__name__
        if name.endswith("Request") or name in _REQUEST_OPS:
            return maybe
    return None


def _caller_source():
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename.replace("\\", "/")
        if "outgoing_profiler.py" not in filename:
            return frame.f_code.co_name or "unknown"
        frame = frame.f_back
    return "unknown"


def _format_trace(fields):
    parts = ["RPC TRACE"]
    for key in (
        "request_id",
        "operation",
        "chat_id",
        "connection_wait_ms",
        "rpc_wait_ms",
        "post_rpc_ms",
        "total_rpc_ms",
        "result",
        "request",
    ):
        if key not in fields or fields[key] is None:
            continue
        value = fields[key]
        if isinstance(value, float):
            parts.append(f"{key}={value:.2f}")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _log_trace(logger, fields):
    line = _format_trace(fields)
    if str(fields.get("result", "")).startswith("failed"):
        logger.log_error(line)
    else:
        logger.log_info(line)


def _hook_method(obj, name):
    original = getattr(obj, name, None)
    if original is None or getattr(original, "_outgoing_send_hook", False):
        return False

    def _stamp():
        if name == "drain" and not getattr(obj, "_pending", None):
            return
        mark_rpc_on_wire()

    if asyncio.iscoroutinefunction(original):
        async def hooked(*args, **kwargs):
            _stamp()
            return await original(*args, **kwargs)
    else:
        def hooked(*args, **kwargs):
            _stamp()
            return original(*args, **kwargs)

    hooked._outgoing_send_hook = True
    try:
        setattr(obj, name, hooked)
        return True
    except (AttributeError, TypeError):
        return False


def _ensure_send_hooks(client):
    """Hook the live sender/connection; re-hook after reconnect if objects change."""
    sender = getattr(client, "_sender", None)
    if sender is None:
        return
    conn = getattr(sender, "_connection", None)
    writer = getattr(conn, "_writer", None) if conn is not None else None
    ws = getattr(writer, "_ws", None) if writer is not None else None
    marker = (id(conn), id(writer), id(ws))
    if getattr(client, "_outgoing_send_hook_ids", None) == marker:
        return
    hooked = False
    if conn is not None:
        hooked = _hook_method(conn, "send") or hooked
        if writer is not None:
            hooked = _hook_method(writer, "drain") or hooked
            if ws is not None:
                hooked = _hook_method(ws, "send_bytes") or hooked
    try:
        client._outgoing_send_hook_ids = marker
        if hooked:
            client._outgoing_send_hooks = True
    except (AttributeError, TypeError):
        pass


def _wrap_existing(obj, name, factory):
    original = getattr(obj, name, None)
    if original is None or getattr(original, "_outgoing_reconnect_hook", False):
        return False
    hooked = factory(original)
    hooked._outgoing_reconnect_hook = True
    try:
        setattr(obj, name, hooked)
        return True
    except (AttributeError, TypeError):
        return False


def _ensure_reconnect_hooks(client, logger):
    """Log keepalive/reconnect; skip pong-timeout reconnect unless an RPC is stuck."""
    sender = getattr(client, "_sender", None)
    if sender is None:
        return
    if getattr(sender, "_outgoing_reconnect_hooks", False):
        _hook_websocket_reset(sender, logger)
        return

    def ping_factory(original):
        def hooked(rnd_id):
            try:
                from modules.connection_guard import (
                    complete_keepalive_pending,
                    drop_completed_pending,
                    live_keepalive_count,
                    log_ping_lifecycle,
                    log_unanswered_ping_states,
                    note_pending,
                    reclaim_dead_pending,
                    reclaim_superseded_keepalive,
                    stamp_keepalive_ping_id,
                    unanswered_keepalive_count,
                    unanswered_keepalive_ids,
                )
                drop_completed_pending(sender)
                note_pending(sender)
                reclaim_dead_pending(sender, logger=logger)
                # Extra pings only. Never wipe the last live PingRequest.
                if unanswered_keepalive_count(sender) > 1:
                    reclaim_superseded_keepalive(
                        sender, keep_newest=1, logger=logger
                    )
            except Exception:
                pass
            outstanding = getattr(sender, "_ping", None)
            snapshot = pending_rpc_snapshot(sender)
            try:
                from modules.connection_guard import (
                    live_keepalive_count,
                    log_ping_lifecycle,
                    log_unanswered_ping_states,
                    unanswered_keepalive_ids,
                )
                live_pings = live_keepalive_count(sender)
            except Exception:
                live_pings = 0
            if live_pings >= 1:
                logger.log_info(
                    "KEEPALIVE PING SKIPPED "
                    f"ping_id={rnd_id} live_pings={live_pings} "
                    f"pending_rpc={snapshot['count']} "
                    f"sender_pending={snapshot['sender_pending']}"
                )
                try:
                    log_unanswered_ping_states(
                        sender, logger, action="skip", next_ping_id=rnd_id,
                    )
                except Exception:
                    pass
                return None
            if outstanding is None:
                try:
                    from modules.connection_guard import log_ping_lifecycle
                    log_ping_lifecycle(
                        logger, rnd_id, "CREATED",
                        future_done=0, task_done=0, in_sender_pending=0,
                    )
                except Exception:
                    pass
                logger.log_info(
                    "KEEPALIVE PING SENT "
                    f"ping_id={rnd_id} pending_rpc={snapshot['count']} "
                    f"sender_pending={snapshot['sender_pending']}"
                )
                try:
                    result = original(rnd_id)
                except asyncio.CancelledError:
                    try:
                        from modules.connection_guard import (
                            complete_keepalive_pending,
                            log_ping_lifecycle,
                        )
                        log_ping_lifecycle(
                            logger, rnd_id, "CANCELLED", action="cancel",
                        )
                        complete_keepalive_pending(
                            sender, ping_id=rnd_id, logger=logger,
                            reason="CANCELLED",
                        )
                    except Exception:
                        pass
                    raise
                except Exception:
                    try:
                        from modules.connection_guard import (
                            complete_keepalive_pending,
                            log_ping_lifecycle,
                        )
                        log_ping_lifecycle(
                            logger, rnd_id, "EXCEPTION", action="cancel",
                        )
                        complete_keepalive_pending(
                            sender, ping_id=rnd_id, logger=logger,
                            reason="EXCEPTION",
                        )
                    except Exception:
                        pass
                    raise
                try:
                    from modules.connection_guard import (
                        log_ping_lifecycle,
                        note_pending,
                        stamp_keepalive_ping_id,
                        unanswered_keepalive_ids,
                    )
                    note_pending(sender)
                    stamp_keepalive_ping_id(sender, rnd_id)
                    queued_ids = unanswered_keepalive_ids(sender)
                    if queued_ids:
                        log_ping_lifecycle(
                            logger, rnd_id, "QUEUED",
                            msg_id=queued_ids[-1],
                            sender_pending=len(queued_ids),
                            future_done=0,
                            task_done=0,
                            in_sender_pending=1,
                        )
                    on_wire = getattr(sender, "_ping", None) is not None
                    if on_wire or queued_ids:
                        log_ping_lifecycle(
                            logger, rnd_id, "SENT",
                            on_wire=int(on_wire),
                            queued=int(bool(queued_ids)),
                            future_done=0,
                            task_done=0,
                            in_sender_pending=int(bool(queued_ids)),
                        )
                    # Do not reclaim after send: that cancelled the ping
                    # just placed on the wire (dropped=1 kept=0).
                except Exception:
                    pass
                return result
            reconnect, reason, oldest_ms, last_ok_ms = _pong_timeout_decision(
                sender, snapshot
            )
            last_ok_text = "-" if last_ok_ms is None else f"{last_ok_ms:.1f}"
            logger.log_info(
                "KEEPALIVE PONG TIMEOUT "
                f"ping_id={outstanding} next_ping_id={rnd_id} "
                f"pending_rpc={snapshot['count']} "
                f"sender_pending={snapshot['sender_pending']} "
                f"request_ids={_format_request_ids(snapshot)} "
                f"operations={_format_operations(snapshot)} "
                f"reconnect={int(reconnect)} reason={reason} "
                f"oldest_rpc_ms={oldest_ms:.1f} "
                f"last_response_ms={last_ok_text} "
                f"event_loop_ok={int(_event_loop_is_healthy())}"
            )
            if not reconnect:
                logger.log_info(
                    "KEEPALIVE PONG TIMEOUT IGNORED "
                    f"reason={reason} oldest_rpc_ms={oldest_ms:.1f} "
                    f"last_response_ms={last_ok_text} "
                    f"pending_rpc={snapshot['count']}"
                )
                _clear_outstanding_ping(sender)
                try:
                    from modules.connection_guard import (
                        complete_keepalive_pending,
                        log_ping_lifecycle,
                    )
                    log_ping_lifecycle(
                        logger, outstanding, "TIMEOUT", action="cancel",
                    )
                    complete_keepalive_pending(
                        sender, ping_id=outstanding, logger=logger,
                        reason="TIMEOUT",
                    )
                except Exception:
                    pass
            return original(rnd_id)
        return hooked

    def pong_factory(original):
        async def hooked(message):
            pong = getattr(message, "obj", message)
            logger.log_info(
                "KEEPALIVE PONG RECEIVED "
                f"ping_id={getattr(pong, 'ping_id', None)} "
                f"msg_id={getattr(pong, 'msg_id', None)}"
            )
            try:
                return await original(message)
            finally:
                # Pong is the PingRequest response even if SPlusthon left
                # future_done=0 in _pending_state.  Drop that row now so it
                # cannot occupy the one-live-ping slot.
                from modules.connection_guard import (
                    complete_keepalive_pending,
                    drop_completed_pending,
                    log_ping_lifecycle,
                )
                drop_completed_pending(sender)
                ping_id = getattr(pong, "ping_id", None)
                log_ping_lifecycle(logger, ping_id, "RESPONSE")
                complete_keepalive_pending(
                    sender, ping_id=ping_id, logger=logger, reason="RESPONSE",
                )
        if not asyncio.iscoroutinefunction(original):
            def sync_hooked(message):
                pong = getattr(message, "obj", message)
                logger.log_info(
                    "KEEPALIVE PONG RECEIVED "
                    f"ping_id={getattr(pong, 'ping_id', None)} "
                    f"msg_id={getattr(pong, 'msg_id', None)}"
                )
                try:
                    return original(message)
                finally:
                    from modules.connection_guard import (
                        complete_keepalive_pending,
                        drop_completed_pending,
                        log_ping_lifecycle,
                    )
                    drop_completed_pending(sender)
                    ping_id = getattr(pong, "ping_id", None)
                    log_ping_lifecycle(logger, ping_id, "RESPONSE")
                    complete_keepalive_pending(
                        sender, ping_id=ping_id, logger=logger, reason="RESPONSE",
                    )
            return sync_hooked
        return hooked

    def start_factory(original):
        def hooked(error):
            will_start = bool(
                getattr(sender, "_user_connected", False)
                and not getattr(sender, "_reconnecting", False)
            )
            if will_start:
                global _RECONNECT_GEN
                _RECONNECT_GEN += 1
                _clear_outstanding_ping(sender)
                try:
                    from modules.connection_guard import drop_keepalive_pending
                    drop_keepalive_pending(
                        sender, logger=logger, reason="CLEANED",
                    )
                except Exception:
                    pass
                pending = _pending_trace(sender)
                _LAST_RECONNECT["started_at"] = time.perf_counter()
                _LAST_RECONNECT["ended_at"] = None
                _LAST_RECONNECT["pending_before"] = pending["ids"]
                _LAST_RECONNECT["reason"] = repr(error)
                inflight = ",".join(
                    str(row.get("request") or row.get("operation") or "?")
                    for row in _INFLIGHT_RPCS.values()
                ) or "-"
                snapshot = pending_rpc_snapshot(sender)
                logger.log_info(
                    "RECONNECT START "
                    f"reason={error!r} pending_rpc={snapshot['count']} "
                    f"sender_pending={snapshot['sender_pending']} "
                    f"request_ids={_format_request_ids(snapshot)} "
                    f"operations={_format_operations(snapshot)}"
                )
                _log_conn_trace(
                    logger,
                    "RECONNECT START",
                    reason=repr(error),
                    gen=_RECONNECT_GEN,
                    inflight=inflight,
                    pending=pending["text"],
                    sender_pending=pending["count"],
                )
                _log_conn_trace(
                    logger,
                    "SNAPSHOT",
                    reason="reconnect_start",
                    extra=_snapshot_line(client),
                )
            return original(error)
        return hooked

    def reconnect_factory(original):
        async def hooked(last_error):
            started = time.perf_counter()
            snapshot = pending_rpc_snapshot(sender)
            try:
                result = await original(last_error)
            except Exception as error:
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.log_info(
                    "RECONNECT FAILED "
                    f"error={error!r} elapsed_ms={elapsed_ms:.1f} "
                    f"pending_rpc={snapshot['count']} "
                    f"request_ids={_format_request_ids(snapshot)}"
                )
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000
            connected = bool(getattr(sender, "_user_connected", False))
            label = "RECONNECT SUCCESS" if connected else "RECONNECT FAILED"
            after = _pending_trace(sender)
            before_ids = set(_LAST_RECONNECT.get("pending_before") or ())
            after_ids = set(after["ids"])
            replayed = sorted(before_ids.intersection(after_ids), key=str)
            _LAST_RECONNECT["ended_at"] = time.perf_counter()
            _LAST_RECONNECT["elapsed_ms"] = elapsed_ms
            logger.log_info(
                f"{label} elapsed_ms={elapsed_ms:.1f} "
                f"pending_rpc={snapshot['count']} "
                f"sender_pending={pending_rpc_snapshot(sender)['sender_pending']} "
                f"request_ids={_format_request_ids(snapshot)}"
            )
            _log_conn_trace(
                logger,
                label,
                elapsed_ms=elapsed_ms,
                replayed_count=len(replayed),
                replayed_ids=",".join(str(item) for item in replayed) or "-",
                pending=after["text"],
            )
            _log_conn_trace(
                logger,
                "SNAPSHOT",
                reason="reconnect_end",
                extra=_snapshot_line(client),
            )
            _ensure_send_hooks(client)
            _hook_ws_lifecycle(sender, logger)
            return result
        if not asyncio.iscoroutinefunction(original):
            def sync_hooked(last_error):
                started = time.perf_counter()
                snapshot = pending_rpc_snapshot(sender)
                try:
                    result = original(last_error)
                except Exception as error:
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    logger.log_info(
                        "RECONNECT FAILED "
                        f"error={error!r} elapsed_ms={elapsed_ms:.1f} "
                        f"pending_rpc={snapshot['count']} "
                        f"request_ids={_format_request_ids(snapshot)}"
                    )
                    raise
                elapsed_ms = (time.perf_counter() - started) * 1000
                connected = bool(getattr(sender, "_user_connected", False))
                label = "RECONNECT SUCCESS" if connected else "RECONNECT FAILED"
                logger.log_info(
                    f"{label} elapsed_ms={elapsed_ms:.1f} "
                    f"pending_rpc={snapshot['count']} "
                    f"request_ids={_format_request_ids(snapshot)}"
                )
                return result
            return sync_hooked
        return hooked

    original_pong = getattr(sender, "_handle_pong", None)
    _wrap_existing(sender, "_keepalive_ping", ping_factory)
    _wrap_existing(sender, "_handle_pong", pong_factory)
    hooked_pong = getattr(sender, "_handle_pong", None)
    handlers = getattr(sender, "_handlers", None)
    if (
        isinstance(handlers, dict)
        and original_pong is not None
        and hooked_pong is not None
        and hooked_pong is not original_pong
    ):
        orig_func = getattr(original_pong, "__func__", original_pong)
        for key, value in list(handlers.items()):
            if value is original_pong or getattr(value, "__func__", None) is orig_func:
                handlers[key] = hooked_pong
    _wrap_existing(sender, "_start_reconnect", start_factory)
    _wrap_existing(sender, "_reconnect", reconnect_factory)
    _hook_websocket_reset(sender, logger)
    _hook_ws_lifecycle(sender, logger)
    try:
        sender._outgoing_reconnect_hooks = True
    except (AttributeError, TypeError):
        pass


def _hook_ws_lifecycle(sender, logger):
    """Log WebSocket close/recv death without changing disconnect behavior."""
    conn = getattr(sender, "_connection", None)
    if conn is None:
        return

    def _wrap_close(obj, name, source):
        original = getattr(obj, name, None)
        if original is None or getattr(original, "_outgoing_ws_close_hook", False):
            return
        if asyncio.iscoroutinefunction(original):
            async def hooked(*args, **kwargs):
                pending = _pending_trace(sender)
                inflight = ",".join(
                    str(row.get("request") or row.get("operation") or "?")
                    for row in _INFLIGHT_RPCS.values()
                ) or "-"
                _log_conn_trace(
                    logger,
                    "WS CLOSE",
                    source=source,
                    inflight=inflight,
                    pending=pending["text"],
                    sender_pending=pending["count"],
                )
                return await original(*args, **kwargs)
        else:
            def hooked(*args, **kwargs):
                pending = _pending_trace(sender)
                inflight = ",".join(
                    str(row.get("request") or row.get("operation") or "?")
                    for row in _INFLIGHT_RPCS.values()
                ) or "-"
                _log_conn_trace(
                    logger,
                    "WS CLOSE",
                    source=source,
                    inflight=inflight,
                    pending=pending["text"],
                    sender_pending=pending["count"],
                )
                return original(*args, **kwargs)
        hooked._outgoing_ws_close_hook = True
        try:
            setattr(obj, name, hooked)
        except (AttributeError, TypeError):
            pass

    _wrap_close(conn, "disconnect", "connection.disconnect")
    _wrap_close(conn, "close", "connection.close")
    writer = getattr(conn, "_writer", None)
    ws = getattr(writer, "_ws", None) if writer is not None else None
    if ws is not None:
        _wrap_close(ws, "close", "websocket.close")


def _hook_websocket_reset(sender, logger):
    conn = getattr(sender, "_connection", None)
    if conn is None:
        return
    original = getattr(conn, "_connection_guard_reset_once", None)
    if original is None or getattr(original, "_outgoing_reconnect_hook", False):
        return

    async def hooked():
        snapshot = pending_rpc_snapshot(sender)
        logger.log_info(
            "WEBSOCKET RESET START reason=periodic "
            f"pending_rpc={snapshot['count']} "
            f"sender_pending={snapshot['sender_pending']} "
            f"request_ids={_format_request_ids(snapshot)}"
        )
        try:
            result = await original()
        except Exception as error:
            logger.log_info(f"WEBSOCKET RESET FAILED error={error!r}")
            raise
        logger.log_info("WEBSOCKET RESET SUCCESS")
        return result

    if not asyncio.iscoroutinefunction(original):
        return
    hooked._outgoing_reconnect_hook = True
    try:
        conn._connection_guard_reset_once = hooked
    except (AttributeError, TypeError):
        pass


def _wrap_connect(client, logger):
    original = getattr(client, "connect", None)
    if original is None or getattr(original, "_outgoing_reconnect_hook", False):
        return False

    async def hooked(*args, **kwargs):
        result = await original(*args, **kwargs)
        _ensure_send_hooks(client)
        _ensure_reconnect_hooks(client, logger)
        return result

    if not asyncio.iscoroutinefunction(original):
        def sync_hooked(*args, **kwargs):
            result = original(*args, **kwargs)
            _ensure_send_hooks(client)
            _ensure_reconnect_hooks(client, logger)
            return result
        sync_hooked._outgoing_reconnect_hook = True
        try:
            client.connect = sync_hooked
            return True
        except (AttributeError, TypeError):
            return False

    hooked._outgoing_reconnect_hook = True
    try:
        client.connect = hooked
        return True
    except (AttributeError, TypeError):
        return False


def _wrap_call(client, logger):
    original = getattr(client, "_call", None)
    if original is None or getattr(original, "_outgoing_profiled", False):
        return False

    @functools.wraps(original)
    async def measured(*args, **kwargs):
        _ensure_send_hooks(client)
        _ensure_reconnect_hooks(client, logger)
        request = _extract_request(args, kwargs)
        op_state = _OP_STATE.get()
        operation = None if op_state is None else op_state.get("operation")
        if not operation:
            operation = _operation_from_request(request)
        chat_id = None if op_state is None else op_state.get("chat_id")
        if chat_id is None:
            chat_id = _request_chat_id(request)
        # The resilience wrapper owns the id for each attempt; fall back to
        # the high-level operation id (or a fresh one) when it is not installed.
        request_id = RPC_REQUEST_ID.get()
        if not request_id:
            request_id = None if op_state is None else op_state.get("request_id")
        if not request_id:
            request_id = new_request_id()
        attempt = RPC_ATTEMPT.get()
        op_id = None if op_state is None else op_state.get("request_id")
        request_name = _request_name(request)
        policy = _rpc_policy()
        timeout_s = policy.timeout_for(request)
        gen_at_start = _RECONNECT_GEN
        started_at = time.perf_counter()
        call = {
            "send_started": None,
            "queued": started_at,
            "logger": logger,
            "request_name": request_name,
            "request_id": request_id,
            "resend_count": 0,
        }
        token = _CALL_STATE.set(call)
        inflight_key = _register_inflight(request, {
            "request_id": request_id,
            "operation": operation or request_name,
            "chat_id": chat_id,
            "request": request_name,
            "started": started_at,
            "timeout_s": timeout_s,
            "deadline": started_at + timeout_s + policy.watchdog_grace_s,
        })
        _note_rpc_activity()
        result = "success"
        outcome_error = None
        sender = None
        if args:
            sender = args[0]
        elif client is not None:
            sender = getattr(client, "_sender", None)
        try:
            _log_conn_trace(
                logger,
                "AWAIT START",
                request=request_name,
                request_id=request_id,
                chat_id=chat_id,
                attempt=attempt,
                op_id=op_id,
                timeout_s=timeout_s,
                reconnect_gen=gen_at_start,
                pending=_pending_trace(sender)["text"],
            )
            return await original(*args, **kwargs)
        except asyncio.CancelledError as error:
            outcome_error = error
            raise
        except Exception as error:
            outcome_error = error
            raise
        finally:
            returned = time.perf_counter()
            send_started = call.get("send_started")
            if send_started is None:
                connection_wait_ms = 0.0
                rpc_wait_ms = (returned - call["queued"]) * 1000
            else:
                connection_wait_ms = (send_started - call["queued"]) * 1000
                rpc_wait_ms = (returned - send_started) * 1000
            post_rpc_ms = (time.perf_counter() - returned) * 1000
            total_rpc_ms = (time.perf_counter() - call["queued"]) * 1000
            reconnects = max(0, _RECONNECT_GEN - gen_at_start)
            reconnect_ms = 0.0
            if reconnects and _LAST_RECONNECT.get("elapsed_ms"):
                reconnect_ms = float(_LAST_RECONNECT.get("elapsed_ms") or 0.0)
            # ROOT-CAUSE FIX (observability): previously a terminal line was
            # emitted only for GetDifference/GetChannelDifference, the 20s
            # window after a reconnect, or RPCs slower than 2s.  A successful
            # SendMessageRequest (70ms) or a fast FloodWaitError therefore
            # looked like an "orphan await" forever, even when nothing hung.
            # Now EVERY AWAIT START gets exactly one terminal line, carrying
            # the same request_id, its outcome and its timings.
            event, result = classify_outcome(outcome_error)
            trace_needed = (
                policy.trace_all
                or _should_trace(request_name)
                or reconnects
                or rpc_wait_ms >= 2000
                or event != "RESPONSE"
            )
            if trace_needed:
                _log_conn_trace(
                    logger,
                    event,
                    request=request_name,
                    request_id=request_id,
                    result=result,
                    attempt=attempt,
                    op_id=op_id,
                    await_ms=total_rpc_ms,
                    socket_wait_ms=connection_wait_ms,
                    rpc_await_ms=rpc_wait_ms,
                    reconnects=reconnects,
                    reconnect_ms=reconnect_ms,
                    resend_count=int(call.get("resend_count") or 0),
                    socket_sent=int(send_started is not None),
                )
            _CALL_STATE.reset(token)
            detach_cancel_target(request_id)
            _unregister_inflight(inflight_key)
            if result == "success":
                _note_rpc_ok()
            record = {
                "request_id": request_id,
                "operation": operation or _request_name(request),
                "chat_id": chat_id,
                "connection_wait_ms": connection_wait_ms,
                "rpc_wait_ms": rpc_wait_ms,
                "post_rpc_ms": post_rpc_ms,
                "total_rpc_ms": total_rpc_ms,
                "result": result,
                "request": _request_name(request),
            }
            if op_state is not None:
                op_state.setdefault("calls", []).append(record)
                op_state["last_return"] = time.perf_counter()
            phases = current_rpc_phases()
            if phases is not None:
                phases["rpc_await_ms"] = float(phases.get("rpc_await_ms") or 0.0) + rpc_wait_ms
                phases["sender_wait_ms"] = float(phases.get("sender_wait_ms") or 0.0) + connection_wait_ms
                if not phases.get("operation"):
                    phases["operation"] = operation or _request_name(request)
            should_log = (
                (operation in _TRACED_OPS)
                or _RPC_DEBUG
                or _operation_from_request(request) in _TRACED_OPS
            )
            if should_log and "RpcOverloadError" not in result:
                _log_trace(logger, record)
            if "RpcOverloadError" not in result:
                _log_rpc_budget(
                    logger,
                    client,
                    operation or _request_name(request),
                    phases if phases is not None else {
                        "queue_wait_ms": 0.0,
                        "governor_wait_ms": 0.0,
                        "sender_wait_ms": connection_wait_ms,
                        "rpc_await_ms": rpc_wait_ms,
                        "started": call["queued"],
                    },
                    extra=f"result={result}",
                )

    measured._outgoing_profiled = True
    try:
        client._call = measured
        return True
    except (AttributeError, TypeError):
        return False


def _wrap(owner, attribute, operation, logger):
    original = getattr(owner, attribute, None)
    if original is None or getattr(original, "_outgoing_profiled", False):
        return False

    @functools.wraps(original)
    async def measured(*args, **kwargs):
        request_id = f"{id(asyncio.current_task()):x}-{time.monotonic_ns():x}"
        chat_id = _chat_id(owner, args, kwargs)
        started_wall = time.time()
        started = time.perf_counter()
        depth = _RPC_DEPTH.get()
        depth_token = _RPC_DEPTH.set(depth + 1)
        state = {
            "request_id": request_id,
            "operation": operation,
            "chat_id": chat_id,
            "calls": [],
            "last_return": None,
        }
        op_token = _OP_STATE.set(state)
        source = _caller_source()
        if _RPC_DEBUG and operation in {"send_message", "reply"}:
            logger.log_info(
                f"SEND START source={source} chat_id={chat_id}"
            )
        if _RPC_DEBUG:
            logger.log_info(
                "OUTGOING RPC START "
                f"request_id={request_id} operation={operation} chat_id={chat_id} "
                f"started_at={started_wall:.3f}"
            )
        result = "success"
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        except Exception as error:
            result = f"failed:{error.__class__.__name__}"
            raise
        finally:
            ended = time.perf_counter()
            elapsed_ms = (ended - started) * 1000
            calls = state.get("calls") or ()
            connection_wait_ms = sum(item["connection_wait_ms"] for item in calls)
            rpc_wait_ms = sum(item["rpc_wait_ms"] for item in calls)
            last_return = state.get("last_return")
            if last_return is None:
                post_rpc_ms = 0.0 if calls else elapsed_ms
            else:
                post_rpc_ms = max(0.0, (ended - last_return) * 1000)
            if depth == 0:
                _RESPONSE_RPC_MS.set(_RESPONSE_RPC_MS.get() + elapsed_ms)
            _OP_STATE.reset(op_token)
            _RPC_DEPTH.reset(depth_token)
            if operation in _TRACED_OPS and "RpcOverloadError" not in result:
                _log_trace(logger, {
                    "request_id": request_id,
                    "operation": operation,
                    "chat_id": chat_id,
                    "connection_wait_ms": connection_wait_ms,
                    "rpc_wait_ms": rpc_wait_ms,
                    "post_rpc_ms": post_rpc_ms,
                    "total_rpc_ms": elapsed_ms,
                    "result": result,
                })
            if _RPC_DEBUG:
                logger.log_info(
                    "OUTGOING RPC FINISHED "
                    f"request_id={request_id} operation={operation} chat_id={chat_id} "
                    f"started_at={started_wall:.3f} finished_at={time.time():.3f} "
                    f"rpc_ms={elapsed_ms:.2f} result={result} nested={depth > 0}"
                )
            if _RPC_DEBUG and operation in {"send_message", "reply"}:
                logger.log_info(
                    f"SEND END source={source} ms={elapsed_ms:.0f}"
                )
            if elapsed_ms >= 250:
                logger.log_info(
                    "RPC TIME "
                    f"operation={operation} chat_id={chat_id} "
                    f"rpc_ms={elapsed_ms:.1f} result={result}"
                )
            if elapsed_ms > _RPC_SLOW_WARNING_MS:
                line = (
                    "OUTGOING RPC WARNING "
                    f"request_id={request_id} operation={operation} "
                    f"chat_id={chat_id} rpc_ms={elapsed_ms:.2f} result={result}"
                )
                if result.startswith("failed"):
                    logger.log_error(line)
                else:
                    # A slow success is diagnostic, not an ERROR.  This keeps
                    # genuine failures visually distinct in production logs.
                    logger.log_info(line)

    measured._outgoing_profiled = True
    try:
        setattr(owner, attribute, measured)
        return True
    except (AttributeError, TypeError):
        return False


def instrument_client(client, logger, policy=None):
    """یک بار پس از ساخت client فراخوانی می‌شود.

    ``policy`` از سمت core (بعد از load شدن .env) پاس داده می‌شود تا تمام
    لایه‌ها یک مهلت/سیاست واحد داشته باشند؛ در تست‌ها همان fallback
    محیطی استفاده می‌شود.
    """
    if getattr(client, "_outgoing_profiler_installed", False):
        return
    if policy is not None:
        configure_rpc_policy(policy)
    _wrap_call(client, logger)
    for attribute, operation in (
        ("send_message", "send_message"),
        ("edit_message", "edit_message"),
        ("delete_messages", "delete_message"),
        ("edit_permissions", "moderation"),
        ("kick_participant", "ban"),
    ):
        _wrap(client, attribute, operation, logger)
    _ensure_send_hooks(client)
    _ensure_reconnect_hooks(client, logger)
    try:
        client._outgoing_profiler_installed = True
    except (AttributeError, TypeError):
        pass


def instrument_event(event, logger):
    """reply/delete رویداد را جدا از RPC داخلی client هم اندازه می‌گیرد."""
    if getattr(event, "_outgoing_profiler_installed", False):
        return
    _wrap(event, "reply", "reply", logger)
    _wrap(event, "delete", "delete_message", logger)
    try:
        event._outgoing_profiler_installed = True
    except (AttributeError, TypeError):
        pass
