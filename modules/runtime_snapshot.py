"""دوره‌ای، فقط خواندنی: علت کند شدن تدریجی ربات را در لاگ مشخص می‌کند.

هیچ صف، Governor، concurrency یا cache ای را تغییر نمی‌دهد. فقط متریک
می‌خواند، با مقدار قبلی مقایسه می‌کند و رشد/انباشت را لاگ می‌کند.

هر ۵ دقیقه یک «ACCUMULATION REPORT» جدا از PERFORMANCE SNAPSHOT ۳۰–۶۰ثانیه‌ای
ثبت می‌شود تا رشد RAM بعد از چند ده دقیقه دیده شود.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional


METRIC_KEYS = (
    "event_loop_lag_ms",
    "pending_tasks",
    "active_tasks",
    "event_handler_tasks",
    "task_kind_counts",
    "moderation_queue_pending",
    "delete_queue_pending",
    "normal_queue_pending",
    "rpc_pending",
    "rpc_governor_wait_ms",
    "sender_pending",
    "active_auto_notice_timers",
    "username_directory_cache_size",
    "economy_cache_size",
    "peer_cache_size",
    "memory_mb",
)

ACCUMULATION_KEYS = METRIC_KEYS + (
    "governor_waiting",
    "governor_waiting_by_bucket",
    "governor_active_by_bucket",
    "inflight_rpc_count",
    "leftover_done_workers",
    "dispatcher_jobs",
    "dispatcher_workers",
    "sender_jobs",
    "sender_workers",
    "delete_jobs",
    "delete_workers",
    "moderation_jobs",
    "moderation_workers",
    "bot_sent_messages_size",
    "performance_batch_size",
    "circuit_breaker_tracked",
    "tracker_rows",
    "spam_history_rows",
    "long_map_total",
    "pending_task_names",
)

# Size-like metrics: a rising value over time is the signal we want.
GROWTH_METRICS = (
    "pending_tasks",
    "active_tasks",
    "moderation_queue_pending",
    "delete_queue_pending",
    "normal_queue_pending",
    "rpc_pending",
    "rpc_governor_wait_ms",
    "sender_pending",
    "active_auto_notice_timers",
    "username_directory_cache_size",
    "economy_cache_size",
    "peer_cache_size",
    "memory_mb",
)

QUEUE_METRICS = (
    "moderation_queue_pending",
    "delete_queue_pending",
    "normal_queue_pending",
)

CACHE_METRICS = (
    "username_directory_cache_size",
    "economy_cache_size",
    "peer_cache_size",
)

ACCUMULATION_GROWTH_METRICS = (
    "pending_tasks",
    "rpc_pending",
    "sender_pending",
    "governor_waiting",
    "memory_mb",
    "username_directory_cache_size",
    "economy_cache_size",
    "peer_cache_size",
    "bot_sent_messages_size",
    "performance_batch_size",
    "inflight_rpc_count",
    "leftover_done_workers",
    "dispatcher_jobs",
    "sender_jobs",
    "delete_jobs",
    "moderation_jobs",
    "tracker_rows",
    "spam_history_rows",
    "long_map_total",
    "circuit_breaker_tracked",
)

_LONG_MAP_ATTRS = (
    "reply_input_peer_cache",
    "bot_sent_messages",
    "punished_users",
    "spam_lock",
    "flood_messages",
    "repeat_messages",
    "user_messages",
    "group_timer_tasks",
    "spam_burst_messages",
    "rejoin_spam_state",
    "forward_spam_counts",
    "_temporary_state_touched",
    "spammer_messages",
    "native_group_admin_cache",
    "native_group_admin_ids_cache",
    "_forward_albums",
    "_big_spam_incidents",
    "_spam_cleanup_incidents",
    "moderation_notification_guard",
)

_BUCKET_ORDER = ("critical", "delete", "send", "heavy", "other")

MILESTONES_SECONDS = (30 * 60, 60 * 60, 90 * 60)

DEFAULT_INTERVAL_SECONDS = 45.0
DEFAULT_ACCUMULATION_SECONDS = 300.0
LAG_PROBE_SECONDS = 0.05
LAG_WARN_MS = 50.0
TASK_GROWTH_DELTA = 20
TASK_GROWTH_ABS = 150
QUEUE_BACKLOG = 5
RPC_BACKLOG = 10
NOTICE_GROWTH_DELTA = 5
CACHE_GROWTH_DELTA = 10
MEMORY_GROWTH_MB = 8.0
ACCUMULATION_MEMORY_GROWTH_MB = 4.0
ACCUMULATION_ROWS_DELTA = 500
BOT_SENT_MESSAGES_MAX = 2000
PEER_CACHE_MAX = 500


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except (TypeError, AttributeError):
        return 0


def _qsize_sum(queues: Any) -> int:
    total = 0
    try:
        values = queues.values() if hasattr(queues, "values") else queues
    except Exception:
        return 0
    for queue in values or ():
        try:
            total += int(queue.qsize())
        except Exception:
            continue
    return total


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    try:
        import resource
        return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0
    except Exception:
        return 0.0


def _task_counts() -> tuple[int, int]:
    try:
        tasks = [task for task in asyncio.all_tasks() if not task.done()]
    except Exception:
        return 0, 0
    active = len(tasks)
    current = asyncio.current_task()
    pending = sum(1 for task in tasks if task is not current)
    return pending, active


def _pending_task_names(limit: int = 8) -> Dict[str, int]:
    try:
        tasks = [task for task in asyncio.all_tasks() if not task.done()]
    except Exception:
        return {}
    counts: Dict[str, int] = {}
    for task in tasks:
        try:
            name = task.get_name() if hasattr(task, "get_name") else "unnamed"
        except Exception:
            name = "unnamed"
        name = str(name or "unnamed")[:80]
        counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[: max(1, int(limit))])


_TASK_KINDS = (
    "dispatch", "reply", "delete", "warn", "owner_peer",
    "bg", "web", "queue_worker", "snapshot", "other",
)

_KIND_PREFIXES = (
    ("reply:", "reply"),
    ("delete:", "delete"),
    ("warn:", "warn"),
    ("owner-peer:", "owner_peer"),
    ("bg:", "bg"),
    ("web:", "web"),
    ("notice-cleanup-", "queue_worker"),
)


def _event_handler_task_set(bot: Any):
    """SPlusthon's set of in-flight update dispatch tasks (read-only).

    ``client._event_handler_tasks`` holds one entry per update whose
    ``_dispatch_update`` task has not finished yet.  Reading its length
    tells how many of the live tasks are library-level update dispatches.
    """
    client = getattr(bot, "client", None)
    if client is None:
        return frozenset()
    value = getattr(client, "_event_handler_tasks", None)
    if isinstance(value, (set, frozenset)):
        return value
    if isinstance(value, (list, tuple)):
        return frozenset(value)
    return frozenset()


def _task_kind_counts(eh_tasks: Any) -> Dict[str, int]:
    """Classify all live tasks by diagnostic kind.  Read-only, bounded output.

    Kinds:
      dispatch     — update dispatch tasks (in ``_event_handler_tasks`` or
                     coroutine ``_dispatch_update``)
      reply        — fire-and-forget reply tasks (name ``reply:<chat>``)
      delete       — fire-and-forget delete tasks (name ``delete:<what>``)
      warn         — fire-and-forget warning tasks (name ``warn:<what>``)
      owner_peer   — owner peer cache tasks (name ``owner-peer:<what>``)
      bg           — background bookkeeping tasks (name ``bg:<what>``)
      web          — web search answer tasks (name ``web:<what>``)
      queue_worker — per-chat queue workers (coroutine ``..._worker`` or
                     name ``notice-cleanup-<chat>``)
      snapshot     — this monitor's own loop
      other        — everything else (permanent loops, game timers,
                     library internals, unnamed tasks)
    """
    counts: Dict[str, int] = {kind: 0 for kind in _TASK_KINDS}
    try:
        tasks = [task for task in asyncio.all_tasks() if not task.done()]
    except Exception:
        return counts
    for task in tasks:
        kind = "other"
        try:
            if task in eh_tasks:
                kind = "dispatch"
            else:
                name = str(task.get_name() or "")
                if name == "runtime-performance-snapshot":
                    kind = "snapshot"
                else:
                    for prefix, mapped in _KIND_PREFIXES:
                        if name.startswith(prefix):
                            kind = mapped
                            break
                    if kind == "other":
                        coro = task.get_coro()
                        qual = str(getattr(coro, "__qualname__", "") or "")
                        if qual == "_dispatch_update":
                            kind = "dispatch"
                        elif qual.endswith("._worker"):
                            kind = "queue_worker"
        except Exception:
            kind = "other"
        counts[kind] += 1
    return counts


async def _event_loop_lag_ms(probe_seconds: float = LAG_PROBE_SECONDS) -> float:
    expected = max(0.0, float(probe_seconds))
    started = time.perf_counter()
    await asyncio.sleep(expected)
    return max(0.0, (time.perf_counter() - started - expected) * 1000.0)


def _governor_wait_ms(governor: Any) -> float:
    if governor is None:
        return 0.0
    now = time.perf_counter()
    longest = 0.0
    try:
        for groups in getattr(governor, "_queues", {}).values():
            for waiters in groups.values():
                for waiter in waiters:
                    future = getattr(waiter, "future", None)
                    if future is not None and getattr(future, "done", lambda: True)():
                        continue
                    enqueued = float(getattr(waiter, "enqueued_at", now) or now)
                    longest = max(longest, (now - enqueued) * 1000.0)
    except Exception:
        return 0.0
    return longest


def _empty_buckets() -> Dict[str, int]:
    return {name: 0 for name in _BUCKET_ORDER}


def _governor_waiting_by_bucket(governor: Any) -> Dict[str, int]:
    buckets = _empty_buckets()
    if governor is None:
        return buckets
    try:
        snapshot = governor.snapshot()
        raw = snapshot.get("waiting_by_bucket") if isinstance(snapshot, dict) else None
        if isinstance(raw, dict) and raw:
            for name in _BUCKET_ORDER:
                buckets[name] = int(raw.get(name, 0) or 0)
            for name, value in raw.items():
                if name not in buckets:
                    buckets[str(name)] = int(value or 0)
            return buckets
    except Exception:
        pass
    try:
        for groups in getattr(governor, "_queues", {}).values():
            for waiters in groups.values():
                for waiter in waiters:
                    future = getattr(waiter, "future", None)
                    if future is not None and getattr(future, "done", lambda: True)():
                        continue
                    bucket = str(getattr(waiter, "bucket", "other") or "other")
                    if bucket not in buckets:
                        buckets[bucket] = 0
                    buckets[bucket] += 1
    except Exception:
        return _empty_buckets()
    return buckets


def _governor_active_by_bucket(governor: Any) -> Dict[str, int]:
    buckets = _empty_buckets()
    if governor is None:
        return buckets
    try:
        snapshot = governor.snapshot()
        raw = snapshot.get("active_by_bucket") if isinstance(snapshot, dict) else None
        if isinstance(raw, dict):
            for name, value in raw.items():
                buckets[str(name)] = int(value or 0)
            return buckets
    except Exception:
        pass
    try:
        raw = dict(getattr(governor, "_active_by_bucket", {}) or {})
        for name, value in raw.items():
            buckets[str(name)] = int(value or 0)
    except Exception:
        return _empty_buckets()
    return buckets


def _username_directory_size() -> int:
    try:
        from economy import storage
        cache = getattr(storage, "_cache", None)
        if not isinstance(cache, dict):
            return 0
        books = cache.get("usernames") or {}
        if not isinstance(books, dict):
            return 0
        return sum(len(book) if isinstance(book, dict) else 1 for book in books.values())
    except Exception:
        return 0


def _economy_cache_size() -> int:
    try:
        from economy import storage
        cache = getattr(storage, "_cache", None)
        if not isinstance(cache, dict):
            return 0
        users = cache.get("users") or {}
        return len(users) if isinstance(users, dict) else 0
    except Exception:
        return 0


def _notice_timer_count(bot: Any) -> int:
    notice = getattr(bot, "notice_cleanup", None)
    if notice is None:
        return 0
    items = getattr(notice, "_items", {}) or {}
    try:
        return sum(len(rows) for rows in items.values())
    except Exception:
        return _safe_len(items)


def _normal_queue_pending(bot: Any) -> int:
    dispatcher = getattr(bot, "group_dispatcher", None)
    if dispatcher is None:
        return 0
    pending_map = getattr(dispatcher, "_normal_pending", None) or {}
    mapped = 0
    try:
        mapped = sum(int(value) for value in pending_map.values())
    except Exception:
        mapped = 0
    queued = 0
    try:
        from modules.group_dispatch import LANE_NORMAL
        for (chat, lane), queue in getattr(dispatcher, "_queues", {}).items():
            if lane == LANE_NORMAL:
                queued += int(queue.qsize())
    except Exception:
        queued = 0
    return max(mapped, queued)


def _worker_counts(mapping: Any) -> tuple[int, int]:
    """Return (alive, leftover_done) for a worker map of tasks or lists of tasks."""
    alive = leftover = 0
    if not mapping:
        return 0, 0
    try:
        values = mapping.values() if hasattr(mapping, "values") else mapping
    except Exception:
        return 0, 0
    for item in values or ():
        tasks = item if isinstance(item, (list, tuple, set)) else (item,)
        for task in tasks:
            if task is None:
                continue
            done = getattr(task, "done", None)
            try:
                is_done = bool(done()) if callable(done) else False
            except Exception:
                continue
            if is_done:
                leftover += 1
            else:
                alive += 1
    return alive, leftover


def _history_row_count(mapping: Any) -> int:
    if not mapping:
        return 0
    try:
        return sum(_safe_len(rows) for rows in mapping.values())
    except Exception:
        return _safe_len(mapping)


def _long_map_sizes(bot: Any) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    for name in _LONG_MAP_ATTRS:
        sizes[name] = _safe_len(getattr(bot, name, None))
    return sizes


def _fmt_map(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    parts = []
    for key, item in value.items():
        if item in (0, None, "", {}, []):
            continue
        parts.append(f"{key}:{item}")
    return ",".join(parts) if parts else "-"


def _fmt_value(key: str, value: Any) -> str:
    if isinstance(value, dict):
        return f"{key}={_fmt_map(value)}"
    if isinstance(value, float):
        if key.endswith("_ms") or key == "memory_mb":
            return f"{key}={value:.1f}"
        return f"{key}={value}"
    return f"{key}={value}"


def prune_unbounded_maps(bot: Any) -> Dict[str, int]:
    """Bound leftover RAM maps. Does not touch Queue/Governor/RPC behavior."""
    trimmed: Dict[str, int] = {}
    sent = getattr(bot, "bot_sent_messages", None)
    if isinstance(sent, list) and len(sent) > BOT_SENT_MESSAGES_MAX:
        dropped = len(sent) - (BOT_SENT_MESSAGES_MAX // 2)
        del sent[:dropped]
        trimmed["bot_sent_messages"] = dropped
    cache = getattr(bot, "reply_input_peer_cache", None)
    if isinstance(cache, dict):
        dropped = 0
        while len(cache) > PEER_CACHE_MAX:
            cache.pop(next(iter(cache)), None)
            dropped += 1
        if dropped:
            trimmed["reply_input_peer_cache"] = dropped
    monitor = getattr(bot, "performance_monitor", None)
    batch = getattr(monitor, "_batch", None) if monitor is not None else None
    trim_batch = getattr(monitor, "trim_batch", None) if monitor is not None else None
    if callable(trim_batch):
        dropped = int(trim_batch() or 0)
        if dropped:
            trimmed["performance_batch"] = dropped
    elif isinstance(batch, dict) and len(batch) > 500:
        dropped = 0
        while len(batch) > 500:
            batch.pop(next(iter(batch)), None)
            dropped += 1
        if dropped:
            trimmed["performance_batch"] = dropped
    try:
        from modules.cache_manager import PermissionCircuitBreaker
        breaker = PermissionCircuitBreaker.get_default()
        cleanup = getattr(breaker, "cleanup_expired", None)
        if callable(cleanup):
            dropped = int(cleanup() or 0)
            if dropped:
                trimmed["circuit_breaker"] = dropped
    except Exception:
        pass
    return trimmed


def collect_sync(bot: Any) -> Dict[str, Any]:
    """Cheap, non-awaiting counters. Loop lag is filled in by collect()."""
    started_at = float(getattr(bot, "started_at", time.time()) or time.time())
    uptime = max(0, int(time.time() - started_at))

    pending_tasks, active_tasks = _task_counts()
    eh_tasks = _event_handler_task_set(bot)

    moderation = getattr(bot, "moderation_queue", None)
    moderation_pending = _safe_len(getattr(moderation, "_pending_keys", None))
    if moderation_pending == 0 and moderation is not None:
        moderation_pending = _qsize_sum(getattr(moderation, "_queues", {}))
    moderation_jobs = _qsize_sum(getattr(moderation, "_queues", {}) if moderation else {})
    moderation_workers, moderation_leftover = _worker_counts(
        getattr(moderation, "_workers", {}) if moderation is not None else {}
    )

    delete_queue = getattr(bot, "message_delete_queue", None)
    delete_pending = _qsize_sum(getattr(delete_queue, "_queues", {}) if delete_queue else {})
    if delete_pending == 0 and delete_queue is not None:
        delete_pending = _safe_len(getattr(delete_queue, "_pending_ids", None))
    delete_jobs = _qsize_sum(getattr(delete_queue, "_queues", {}) if delete_queue else {})
    delete_workers, delete_leftover = _worker_counts(
        getattr(delete_queue, "_workers", {}) if delete_queue is not None else {}
    )

    rpc_pending = 0
    sender_pending = 0
    sender_keepalive = 0
    sender_stale = 0
    sender_oldest = 0.0
    sender_by_type = {}
    inflight_rpc_count = 0
    try:
        from modules.outgoing_profiler import pending_rpc_snapshot
        sender = getattr(getattr(bot, "client", None), "_sender", None)
        snap = pending_rpc_snapshot(sender)
        rpc_pending = int(snap.get("count") or 0)
        sender_pending = int(snap.get("sender_pending") or 0)
        sender_keepalive = int(snap.get("sender_pending_keepalive") or 0)
        sender_stale = int(snap.get("sender_pending_stale") or 0)
        sender_oldest = float(snap.get("sender_pending_oldest_age_ms") or 0.0)
        sender_by_type = dict(snap.get("sender_pending_by_type") or {})
        inflight_rpc_count = int(snap.get("inflight") or snap.get("count") or 0)
    except Exception:
        pass
    if inflight_rpc_count == 0:
        inflight_rpc_count = int(rpc_pending)

    governor = getattr(bot, "rpc_governor", None)
    governor_wait_ms = _governor_wait_ms(governor)
    waiting_by_bucket = _governor_waiting_by_bucket(governor)
    active_by_bucket = _governor_active_by_bucket(governor)
    governor_waiting = sum(int(value) for value in waiting_by_bucket.values())
    try:
        snapshot = governor.snapshot() if governor is not None else {}
        if isinstance(snapshot, dict) and snapshot.get("waiting") is not None:
            governor_waiting = int(snapshot.get("waiting") or governor_waiting)
    except Exception:
        pass

    peer_cache = getattr(bot, "reply_input_peer_cache", None) or {}
    outgoing = getattr(bot, "outgoing_sender", None)
    sender_jobs = _qsize_sum(getattr(outgoing, "_queues", {}) if outgoing else {})
    sender_workers, sender_leftover = _worker_counts(
        getattr(outgoing, "_workers", {}) if outgoing is not None else {}
    )

    dispatcher = getattr(bot, "group_dispatcher", None)
    dispatcher_jobs = _qsize_sum(getattr(dispatcher, "_queues", {}) if dispatcher else {})
    dispatcher_workers, dispatcher_leftover = _worker_counts(
        getattr(dispatcher, "_workers", {}) if dispatcher is not None else {}
    )

    leftover_done_workers = (
        moderation_leftover + delete_leftover + sender_leftover + dispatcher_leftover
    )

    tracker_rows = 0
    spam_history_rows = 0
    try:
        from modules import message_tracker
        tracker_rows = _history_row_count(getattr(message_tracker, "_HISTORY", {}))
    except Exception:
        tracker_rows = 0
    try:
        from modules import spam_history
        spam_history_rows = _history_row_count(getattr(spam_history, "MESSAGE_HISTORY", {}))
    except Exception:
        spam_history_rows = 0

    long_maps = _long_map_sizes(bot)
    performance_batch_size = _safe_len(
        getattr(getattr(bot, "performance_monitor", None), "_batch", None)
    )
    circuit_breaker_tracked = 0
    try:
        from modules.cache_manager import PermissionCircuitBreaker
        breaker = getattr(PermissionCircuitBreaker, "_instance", None)
        if breaker is not None:
            circuit_breaker_tracked = _safe_len(getattr(breaker, "_breakers", None))
    except Exception:
        circuit_breaker_tracked = 0

    return {
        "uptime": uptime,
        "event_loop_lag_ms": 0.0,
        "pending_tasks": int(pending_tasks),
        "active_tasks": int(active_tasks),
        "event_handler_tasks": int(len(eh_tasks)),
        "task_kind_counts": _task_kind_counts(eh_tasks),
        "moderation_queue_pending": int(moderation_pending),
        "delete_queue_pending": int(delete_pending),
        "normal_queue_pending": int(_normal_queue_pending(bot)),
        "rpc_pending": int(rpc_pending),
        "rpc_governor_wait_ms": round(float(governor_wait_ms), 1),
        "sender_pending": int(sender_pending),
        "sender_pending_keepalive": int(sender_keepalive),
        "sender_pending_stale": int(sender_stale),
        "sender_pending_oldest_age_ms": round(sender_oldest, 1),
        "sender_pending_by_type": sender_by_type,
        "active_auto_notice_timers": int(_notice_timer_count(bot)),
        "username_directory_cache_size": int(_username_directory_size()),
        "economy_cache_size": int(_economy_cache_size()),
        "peer_cache_size": int(_safe_len(peer_cache)),
        "memory_mb": round(_rss_mb(), 1),
        "governor_waiting": int(governor_waiting),
        "governor_waiting_by_bucket": waiting_by_bucket,
        "governor_active_by_bucket": active_by_bucket,
        "inflight_rpc_count": int(inflight_rpc_count),
        "leftover_done_workers": int(leftover_done_workers),
        "dispatcher_jobs": int(dispatcher_jobs),
        "dispatcher_workers": int(dispatcher_workers),
        "sender_jobs": int(sender_jobs),
        "sender_workers": int(sender_workers),
        "delete_jobs": int(delete_jobs),
        "delete_workers": int(delete_workers),
        "moderation_jobs": int(moderation_jobs),
        "moderation_workers": int(moderation_workers),
        "bot_sent_messages_size": int(_safe_len(getattr(bot, "bot_sent_messages", None))),
        "performance_batch_size": int(performance_batch_size),
        "circuit_breaker_tracked": int(circuit_breaker_tracked),
        "tracker_rows": int(tracker_rows),
        "spam_history_rows": int(spam_history_rows),
        "long_map_sizes": long_maps,
        "long_map_total": int(sum(long_maps.values())),
        "pending_task_names": _pending_task_names(),
    }


async def collect(bot: Any, *, lag_probe_seconds: float = LAG_PROBE_SECONDS) -> Dict[str, Any]:
    snapshot = collect_sync(bot)
    snapshot["event_loop_lag_ms"] = round(await _event_loop_lag_ms(lag_probe_seconds), 1)
    pending_tasks, active_tasks = _task_counts()
    snapshot["pending_tasks"] = int(pending_tasks)
    snapshot["active_tasks"] = int(active_tasks)
    snapshot["pending_task_names"] = _pending_task_names()
    return snapshot


def format_snapshot(snapshot: Dict[str, Any], *, title: str = "PERFORMANCE SNAPSHOT") -> str:
    lines = [title, f"uptime={int(snapshot.get('uptime', 0))}s"]
    for key in METRIC_KEYS:
        lines.append(_fmt_value(key, snapshot.get(key, 0)))
    return "\n".join(lines)


def format_accumulation_report(
    snapshot: Dict[str, Any],
    previous: Optional[Dict[str, Any]] = None,
    *,
    title: str = "ACCUMULATION REPORT",
) -> str:
    lines = [title, f"uptime={int(snapshot.get('uptime', 0))}s"]
    for key in ACCUMULATION_KEYS:
        lines.append(_fmt_value(key, snapshot.get(key, 0)))
    long_maps = snapshot.get("long_map_sizes") or {}
    if isinstance(long_maps, dict) and long_maps:
        lines.append("long_maps=" + _fmt_map(long_maps))
    if previous:
        grown = []
        for key in ACCUMULATION_GROWTH_METRICS:
            try:
                delta = float(snapshot.get(key, 0) or 0) - float(previous.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if key == "memory_mb":
                if delta >= ACCUMULATION_MEMORY_GROWTH_MB:
                    grown.append(f"{key}={delta:+.1f}")
            elif delta > 0:
                grown.append(f"{key}={int(delta):+d}" if float(delta).is_integer() else f"{key}={delta:+.1f}")
        lines.append("delta_5m=" + (",".join(grown) if grown else "none"))
    else:
        lines.append("delta_5m=baseline")
    return "\n".join(lines)


def detect_issues(current: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> list[str]:
    """Compare snapshots. Never mutates bot state."""
    lines: list[str] = []
    lag = float(current.get("event_loop_lag_ms") or 0.0)
    if lag >= LAG_WARN_MS:
        lines.append(f"EVENT LOOP LAG DETECTED lag_ms={lag:.1f}")

    active = int(current.get("active_tasks") or 0)
    pending = int(current.get("pending_tasks") or 0)
    prev_active = int((previous or {}).get("active_tasks") or 0)
    if active >= TASK_GROWTH_ABS or (previous and active - prev_active >= TASK_GROWTH_DELTA):
        lines.append(f"TASK GROWTH DETECTED active={active} pending={pending}")
        # Diagnostic breakdown of what the live tasks actually are.
        raw_counts = current.get("task_kind_counts")
        counts = raw_counts if isinstance(raw_counts, dict) else {}

        def _ck(key: str) -> int:
            try:
                return int(counts.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        try:
            event_handler = int(current.get("event_handler_tasks") or 0)
        except (TypeError, ValueError):
            event_handler = 0
        lines.append(
            "TASK BREAKDOWN "
            f"active={active} "
            f"event_handler={event_handler} "
            f"reply={_ck('reply')} delete={_ck('delete')} warn={_ck('warn')} "
            f"owner_peer={_ck('owner_peer')} bg={_ck('bg')} web={_ck('web')} "
            f"queue={_ck('queue_worker')} snapshot={_ck('snapshot')} "
            f"other={_ck('other')}"
        )

    for queue_name in QUEUE_METRICS:
        value = int(current.get(queue_name) or 0)
        if value >= QUEUE_BACKLOG:
            lines.append(f"QUEUE BACKLOG DETECTED queue={queue_name} pending={value}")

    rpc_pending = int(current.get("rpc_pending") or 0)
    sender_pending = int(current.get("sender_pending") or 0)
    if rpc_pending >= RPC_BACKLOG or sender_pending >= RPC_BACKLOG:
        lines.append(
            f"RPC BACKLOG DETECTED pending={rpc_pending} sender_pending={sender_pending}"
        )
    prev_sender = int((previous or {}).get("sender_pending") or 0)
    for mark in (10, 20, 40, 60):
        if sender_pending >= mark > prev_sender:
            lines.append(
                f"SENDER PENDING GROWTH previous={prev_sender} current={sender_pending} crossed={mark}"
            )

    notices = int(current.get("active_auto_notice_timers") or 0)
    prev_notices = int((previous or {}).get("active_auto_notice_timers") or 0)
    if notices >= NOTICE_GROWTH_DELTA and (previous is None or notices > prev_notices):
        lines.append(f"AUTO NOTICE TIMER GROWTH DETECTED active={notices}")

    for cache_name in CACHE_METRICS:
        size = int(current.get(cache_name) or 0)
        prev = int((previous or {}).get(cache_name) or 0)
        if size - prev >= CACHE_GROWTH_DELTA or (previous is None and size >= CACHE_GROWTH_DELTA * 5):
            lines.append(f"CACHE GROWTH DETECTED cache={cache_name} size={size}")

    if previous:
        for metric in GROWTH_METRICS:
            before = previous.get(metric, 0)
            after = current.get(metric, 0)
            try:
                delta = float(after) - float(before)
            except (TypeError, ValueError):
                continue
            if metric == "memory_mb":
                if delta < MEMORY_GROWTH_MB:
                    continue
            elif metric.endswith("_ms"):
                if delta < LAG_WARN_MS:
                    continue
            elif delta < _growth_min_delta(metric):
                # One extra task or one newly seen user is normal traffic,
                # not a leak; only a real jump is worth an ERROR line.
                continue
            lines.append(
                f"GROWING STATE DETECTED metric={metric} "
                f"previous={before} current={after} delta={delta if metric == 'memory_mb' or metric.endswith('_ms') else int(delta)}"
            )
    return lines


def _growth_min_delta(metric: str) -> int:
    """Smallest increase between two snapshots that counts as growth."""
    if metric in ("pending_tasks", "active_tasks"):
        return TASK_GROWTH_DELTA
    if metric in CACHE_METRICS:
        return CACHE_GROWTH_DELTA
    return QUEUE_BACKLOG


def detect_accumulation(
    current: Dict[str, Any],
    previous: Optional[Dict[str, Any]] = None,
) -> list[str]:
    """Flag 5-minute RAM/task/cache growth. Read-only."""
    lines: list[str] = []
    leftover = int(current.get("leftover_done_workers") or 0)
    if leftover:
        lines.append(f"WORKER LEFTOVER DETECTED leftover_done_workers={leftover}")
    waiting = current.get("governor_waiting_by_bucket") or {}
    if isinstance(waiting, dict):
        busy = {name: int(value or 0) for name, value in waiting.items() if int(value or 0)}
        if sum(busy.values()) >= 3:
            lines.append("GOVERNOR WAITING PER BUCKET " + _fmt_map(busy))
    if previous is None:
        return lines
    for metric in ACCUMULATION_GROWTH_METRICS:
        try:
            before = float(previous.get(metric, 0) or 0)
            after = float(current.get(metric, 0) or 0)
            delta = after - before
        except (TypeError, ValueError):
            continue
        if metric == "memory_mb":
            if delta < ACCUMULATION_MEMORY_GROWTH_MB:
                continue
            lines.append(
                f"ACCUMULATION GROWTH metric={metric} previous={before:.1f} "
                f"current={after:.1f} delta={delta:.1f}"
            )
            continue
        if delta <= 0:
            continue
        if metric in CACHE_METRICS or metric.endswith("_size") or metric.endswith("_rows") or metric.endswith("_total"):
            # Caches and tracking tables fill up with ordinary traffic; only
            # a large 5-minute jump points at unbounded growth.
            if delta < ACCUMULATION_ROWS_DELTA:
                continue
        elif delta < _growth_min_delta(metric):
            continue
        lines.append(
            f"ACCUMULATION GROWTH metric={metric} previous={int(before)} "
            f"current={int(after)} delta={int(delta)}"
        )
    return lines


def vs_baseline_lines(current: Dict[str, Any], baseline: Dict[str, Any]) -> list[str]:
    lines = ["PERFORMANCE VS BASELINE"]
    unusual = []
    for key in METRIC_KEYS:
        before = baseline.get(key, 0)
        after = current.get(key, 0)
        try:
            delta = float(after) - float(before)
        except (TypeError, ValueError):
            continue
        lines.append(f"{key} baseline={before} current={after} delta={delta:.1f}")
        grown = False
        if key == "memory_mb":
            grown = delta >= MEMORY_GROWTH_MB * 2
        elif key == "event_loop_lag_ms":
            grown = float(after) >= LAG_WARN_MS * 2
        elif key in QUEUE_METRICS or key in ("rpc_pending", "sender_pending"):
            grown = float(after) >= QUEUE_BACKLOG
        elif key in CACHE_METRICS or key in ("active_tasks", "pending_tasks", "active_auto_notice_timers"):
            grown = delta >= max(CACHE_GROWTH_DELTA, float(before) * 0.5 if float(before) else CACHE_GROWTH_DELTA)
        if grown:
            unusual.append(key)
    if unusual:
        lines.append("UNUSUAL GROWTH SINCE START metrics=" + ",".join(unusual))
    else:
        lines.append("UNUSUAL GROWTH SINCE START metrics=none")
    return lines


class RuntimeSnapshotMonitor:
    """Background loop: baseline at t=0, then every 30–60s, plus a 5-minute accumulation report."""

    def __init__(
        self,
        bot: Any,
        logger: Any,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        lag_probe_seconds: float = LAG_PROBE_SECONDS,
        accumulation_interval_seconds: float = DEFAULT_ACCUMULATION_SECONDS,
    ):
        self.bot = bot
        self.logger = logger
        env_interval = os.environ.get("BOT_RUNTIME_SNAPSHOT_SECONDS", "").strip()
        if env_interval:
            try:
                interval_seconds = float(env_interval)
            except ValueError:
                pass
        self.interval_seconds = min(60.0, max(30.0, float(interval_seconds))) if interval_seconds >= 1 else max(0.01, float(interval_seconds))
        env_acc = os.environ.get("BOT_ACCUMULATION_SECONDS", "").strip()
        if env_acc:
            try:
                accumulation_interval_seconds = float(env_acc)
            except ValueError:
                pass
        if accumulation_interval_seconds >= 1:
            self.accumulation_interval_seconds = max(
                60.0, min(900.0, float(accumulation_interval_seconds))
            )
        else:
            self.accumulation_interval_seconds = max(0.01, float(accumulation_interval_seconds))
        self.lag_probe_seconds = max(0.0, float(lag_probe_seconds))
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self.previous: Optional[Dict[str, Any]] = None
        self.baseline: Optional[Dict[str, Any]] = None
        self.accumulation_previous: Optional[Dict[str, Any]] = None
        self._last_accumulation_mono = 0.0
        self._milestones_logged = set()
        self.snapshots: list[Dict[str, Any]] = []
        self._last_force = 0.0
        self._force_lock = False

    def _log(self, message: str, *, error: bool = False) -> None:
        if self.logger is None:
            return
        method = getattr(self.logger, "log_error" if error else "log_info", None)
        if callable(method):
            method(message)

    def _maybe_log_accumulation(self, snapshot: Dict[str, Any]) -> None:
        now = time.monotonic()
        due = (
            self.accumulation_previous is None
            or (now - self._last_accumulation_mono) >= self.accumulation_interval_seconds
        )
        if not due:
            return
        self._log(format_accumulation_report(snapshot, self.accumulation_previous))
        for line in detect_accumulation(snapshot, self.accumulation_previous):
            self._log(line, error=True)
        self.accumulation_previous = dict(snapshot)
        self._last_accumulation_mono = now

    async def emit(self, *, title: str = "PERFORMANCE SNAPSHOT") -> Dict[str, Any]:
        snapshot = await collect(self.bot, lag_probe_seconds=self.lag_probe_seconds)
        self._log(format_snapshot(snapshot, title=title))
        for line in detect_issues(snapshot, self.previous):
            self._log(line, error=True)
        if self.baseline is None:
            self.baseline = dict(snapshot)
            self._log("PERFORMANCE BASELINE RECORDED")
        uptime = int(snapshot.get("uptime") or 0)
        for mark in MILESTONES_SECONDS:
            if uptime >= mark and mark not in self._milestones_logged:
                self._milestones_logged.add(mark)
                self._log(f"PERFORMANCE MILESTONE elapsed={mark}s")
                for line in vs_baseline_lines(snapshot, self.baseline):
                    self._log(line)
        self._maybe_log_accumulation(snapshot)
        self.previous = dict(snapshot)
        self.snapshots.append(snapshot)
        if len(self.snapshots) > 200:
            self.snapshots = self.snapshots[-100:]
        return snapshot

    async def _run_loop(self) -> None:
        try:
            await self.emit(title="PERFORMANCE SNAPSHOT")
            while not self._closed:
                await asyncio.sleep(self.interval_seconds)
                if self._closed:
                    break
                await self.emit(title="PERFORMANCE SNAPSHOT")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log(f"RUNTIME SNAPSHOT LOOP FAILED error={error!r}", error=True)

    def start(self) -> bool:
        if self._closed:
            return False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run_loop(), name="runtime-performance-snapshot"
            )
        return True

    async def stop(self) -> None:
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
