"""Fair bot-level admission control for SPlusthon application RPCs.

SPlusthon uses one shared WebSocket/sender for sends, deletes, moderation and
entity/history reads.  Per-chat queues keep handlers isolated, but without a
connection-wide budget dozens of independent chats can still create a large
``_pending_state`` at once.  ``RpcGovernor`` applies backpressure immediately
before the active low-level ``client._call`` and never drops a request/result.

The governor deliberately sits *outside* the existing 60-second RPC timeout:
time spent waiting for admission is reported separately and does not consume
network response time.  A permit is released by the caller's ``finally`` on
success, error, timeout or cancellation.  Retry/FloodWait sleeps performed by
our queues happen after ``_call`` has returned/raised and therefore hold no
permit.
"""

import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass

try:
    from modules.group_id import normalize_group_id
except ImportError:  # pragma: no cover - standalone compatibility
    def normalize_group_id(value):
        try:
            return str(int(value))
        except Exception:
            return str(value)


P0_CRITICAL = 0
P1_DELETE = 1
P2_SEND = 2
P3_HEAVY = 3

_PRIORITY_LABELS = {
    P0_CRITICAL: "P0",
    P1_DELETE: "P1",
    P2_SEND: "P2",
    P3_HEAVY: "P3",
}

# Native role checks and synchronization requests must remain responsive.
_CRITICAL_REQUESTS = frozenset({
    "EditBannedRequest",
    "EditAdminRequest",
    "EditChatDefaultBannedRightsRequest",
    "DeleteChatUserRequest",
    "GetParticipantRequest",
    "GetStateRequest",
    "GetDifferenceRequest",
    "GetChannelDifferenceRequest",
    "GetConfigRequest",
    "PingRequest",
    "DestroySessionRequest",
    "ExportAuthorizationRequest",
    "ImportAuthorizationRequest",
})
_DELETE_REQUESTS = frozenset({
    "DeleteMessagesRequest",
    "DeleteHistoryRequest",
})
_SEND_REQUESTS = frozenset({
    "SendMessageRequest",
    "SendMediaRequest",
    "SendMultiMediaRequest",
    "ForwardMessagesRequest",
    "SendInlineBotResultRequest",
    "SaveFilePartRequest",
    "SaveBigFilePartRequest",
})
_HEAVY_REQUESTS = frozenset({
    "GetHistoryRequest",
    "GetMessagesRequest",
    "GetParticipantsRequest",
    "GetFullChannelRequest",
    "GetFullChatRequest",
    "GetChannelsRequest",
    "GetChatsRequest",
    "GetUsersRequest",
})


@dataclass(frozen=True)
class RpcAdmission:
    priority: int
    bucket: str
    request_name: str
    chat_key: str


@dataclass
class _Waiter:
    priority: int
    bucket: str
    request_name: str
    chat_key: str
    future: object
    enqueued_at: float
    sequence: int
    admitted: bool = False


@dataclass
class _Holder:
    request_name: str
    bucket: str
    chat_key: str
    priority: int
    acquired_at: float
    sequence: int
    watch: object = None


KEEPALIVE_REQUESTS = frozenset({
    "PingRequest",
    "PingDelayDisconnectRequest",
    "MsgsAck",
    "HttpWait",
    "Pong",
})
GOVERNOR_BLOCKED_MS = 500.0


class RpcOverloadError(RuntimeError):
    """A disposable low-priority RPC was rejected before it could backlog."""


class RpcPermit:
    """One governor admission. ``release`` is idempotent."""

    __slots__ = (
        "_governor", "priority", "bucket", "shadow", "released", "holder",
    )

    def __init__(self, governor, priority, bucket, *, shadow=False, holder=None):
        self._governor = governor
        self.priority = int(priority)
        self.bucket = str(bucket)
        self.shadow = bool(shadow)
        self.released = False
        self.holder = holder

    def release(self):
        if self.released:
            return False
        self.released = True
        self._governor._release(self.priority, self.bucket, holder=self.holder)
        return True

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.release()


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return bool(default)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name, default, minimum=1, maximum=None):
    try:
        value = max(int(minimum), int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = int(default)
    return min(maximum, value) if maximum is not None else value


def _env_float(name, default, minimum=0.0):
    try:
        return max(float(minimum), float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return float(default)


def _unwrap_requests(request):
    """Yield real TL requests through list/Invoke wrappers without mutation."""
    pending = deque([request])
    seen = set()
    while pending:
        current = pending.popleft()
        if current is None:
            continue
        if isinstance(current, (list, tuple)):
            pending.extend(current)
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        inner = getattr(current, "query", None)
        if inner is not None:
            pending.appendleft(inner)
            continue
        yield current


def is_keepalive_request(request):
    names = [type(item).__name__ for item in _unwrap_requests(request)]
    if not names:
        names = [type(request).__name__]
    return bool(set(names).intersection(KEEPALIVE_REQUESTS))


def _request_chat(request):
    for current in _unwrap_requests(request):
        for attr in ("peer", "channel", "entity", "chat_id"):
            value = getattr(current, attr, None)
            if value is None:
                continue
            for nested in ("channel_id", "chat_id", "user_id", "id"):
                found = getattr(value, nested, None)
                if found is not None:
                    return found
            if isinstance(value, (int, str)):
                return value
    return None


def _stable_chat_key(value):
    if value is None:
        return "global"
    try:
        return normalize_group_id(value)
    except Exception:
        try:
            return str(value)
        except Exception:
            return "global"


def classify_request(request, *, urgent_send=False, critical_context=False,
                     channel="main"):
    """Classify a SPlusthon TL request using names stable across versions.

    ``channel="notice"`` puts cosmetic auto-notices in their own bucket: they
    are still sends on the same socket, but they can no longer consume the
    (single) ordinary ``send`` slot that user-facing replies need.
    """
    names = [type(item).__name__ for item in _unwrap_requests(request)]
    if not names:
        names = [type(request).__name__]

    name_set = set(names)
    if name_set.intersection(_CRITICAL_REQUESTS):
        priority, bucket = P0_CRITICAL, "critical"
    elif name_set.intersection(_DELETE_REQUESTS):
        # Even a manual/admin delete stays P1. Queue workers inherit the
        # context in which they were created; allowing that inherited context
        # to turn a long deletion wave into P0 would defeat the reserved slots.
        priority, bucket = P1_DELETE, "delete"
    elif name_set.intersection(_SEND_REQUESTS):
        # A delayed notice is preferable to starving EditBanned/connection
        # synchronization.  Even an admin reply is a send, never a P0 RPC.
        # Notices get a dedicated bucket so they cannot head-block the main
        # send lane (architecture decision: separate lanes, one socket).
        priority = P2_SEND
        bucket = "notice" if channel == "notice" else "send"
    # Reads are never promoted by a command's dispatch context.  A native
    # admin lookup (GetParticipants/GetFullChannel) is expensive and was
    # filling every P0 slot ahead of the actual mute/ban RPC.
    elif name_set.intersection(_HEAVY_REQUESTS) or any(
        name.startswith(("Get", "Search")) for name in names
    ):
        priority, bucket = P3_HEAVY, "heavy"
    elif critical_context:
        priority, bucket = P0_CRITICAL, "critical"
    else:
        # Unknown application calls are limited by the combined noncritical
        # budget, but do not inherit the strict heavy-read cap.
        priority, bucket = P2_SEND, "other"

    return RpcAdmission(
        priority=priority,
        bucket=bucket,
        request_name="+".join(names[:3]),
        chat_key=_stable_chat_key(_request_chat(request)),
    )


class RpcGovernor:
    """Fixed-limit, priority-aware and per-chat-fair RPC admission control."""

    # Strict order: P0, then P1 (delete/antispam), then P2, then P3.
    # A weighted mix previously let GetParticipants (P3) take a free slot
    # while DeleteMessages (P1) was already waiting.

    def __init__(
        self,
        *,
        total_limit=2,
        noncritical_limit=None,
        delete_limit=None,
        send_limit=None,
        heavy_limit=None,
        notice_limit=None,
        critical_reserve=0,
        enabled=True,
        shadow=False,
        logger=None,
        wait_log_ms=20.0,
        max_send_waiters=32,
        min_interval_seconds=0.0,
    ):
        self.total_limit = max(1, int(total_limit))
        isolated_default = noncritical_limit is None
        if delete_limit is None:
            delete_limit = 1 if isolated_default else max(1, int(noncritical_limit) // 2)
        if send_limit is None:
            if isolated_default:
                send_limit = 1
            else:
                send_limit = (
                    max(1, (int(noncritical_limit) // 2) - 1)
                    if int(noncritical_limit) > 3
                    else 1
                )
        if heavy_limit is None:
            heavy_limit = 1
        self.class_limits = {
            "delete": max(1, int(delete_limit)),
            "send": max(1, int(send_limit)),
            "heavy": max(1, int(heavy_limit)),
            "notice": max(1, int(notice_limit or 1)),
            "other": 1,
        }
        # Optional extra slot usable ONLY by P0 moderation/connection RPCs.
        # Default 0 keeps the historical peak in flight; set
        # BOT_RPC_CRITICAL_RESERVE=1 on very busy deployments so a slow send
        # never makes EditBanned wait for a permit.
        self.critical_reserve = max(0, int(critical_reserve))
        if isolated_default:
            # Send and delete each have their own cap. They share the
            # connection budget, not one mutex.
            noncritical_limit = min(
                self.total_limit,
                self.class_limits["delete"] + self.class_limits["send"],
            )
        self.noncritical_limit = max(
            1, min(int(noncritical_limit), self.total_limit)
        )
        self.enabled = bool(enabled)
        self.shadow = bool(shadow)
        self.logger = logger
        self.wait_log_ms = max(0.0, float(wait_log_ms))
        self.max_send_waiters = max(0, int(max_send_waiters))
        # Cosmetic notices are shed earlier than user-facing replies.
        self.max_notice_waiters = max(4, self.max_send_waiters // 2)
        # Preserved Foxora pacing feature (RPC_MIN_INTERVAL_SECONDS): optional
        # minimum spacing between RPC call starts. 0.0 keeps the B2 behavior
        # bit-for-bit; a positive value serializes starts like the Foxora
        # SerialRpcQueue did, without merging or dropping any request.
        self.min_interval_seconds = max(0.0, min(60.0, float(min_interval_seconds)))
        self._next_start_at = 0.0

        self._active_total = 0
        self._active_noncritical = 0
        self._active_by_bucket = defaultdict(int)
        self._queues = {priority: {} for priority in range(4)}
        self._chat_rounds = {priority: deque() for priority in range(4)}
        self._sequence = 0
        self._waiting = 0
        self._max_waiting = 0
        self._backlog_started = None
        self._burst_max_waiting = 0
        self._observe_sender = None
        self._holders = []
        self.stats = {
            "admitted": 0,
            "released": 0,
            "waited": 0,
            "cancelled_waiters": 0,
            "shadow_would_wait": 0,
        }

    @classmethod
    def from_environment(cls, logger=None):
        """Build conservative fixed limits after the project's .env is loaded."""
        total_limit = min(3, max(2, _env_int("BOT_RPC_TOTAL_LIMIT", 2)))
        delete_limit = min(2, max(1, _env_int("BOT_RPC_DELETE_LIMIT", 1)))
        send_limit = min(2, max(1, _env_int("BOT_RPC_SEND_LIMIT", 1)))
        heavy_limit = min(1, max(1, _env_int("BOT_RPC_HEAVY_LIMIT", 1)))
        isolated = min(total_limit, delete_limit + send_limit)
        noncritical_limit = min(
            total_limit,
            max(1, _env_int("BOT_RPC_NONCRITICAL_LIMIT", isolated)),
        )
        return cls(
            # Peak in-flight stays at total_limit. Send and delete no longer
            # share one noncritical mutex; P0 still wins the next free slot.
            total_limit=total_limit,
            noncritical_limit=noncritical_limit,
            delete_limit=delete_limit,
            send_limit=send_limit,
            heavy_limit=heavy_limit,
            enabled=_env_bool("BOT_RPC_GOVERNOR_ENABLED", True),
            shadow=_env_bool("BOT_RPC_GOVERNOR_SHADOW", False),
            logger=logger,
            wait_log_ms=_env_float("BOT_RPC_WAIT_LOG_MS", 20.0),
            # Bounded send queue allows public commands and help replies to wait
            # without letting cosmetic/game flooding starve moderation.
            notice_limit=min(2, max(1, _env_int("BOT_RPC_NOTICE_LIMIT", 1))),
            critical_reserve=_env_int("BOT_RPC_CRITICAL_RESERVE", 0, 0, 2),
            max_send_waiters=max(8, _env_int("BOT_RPC_MAX_SEND_WAITERS", 32)),
            # فاصلهٔ اختیاری بین شروع RPCها؛ knob محفوظ Foxora
            # (RPC_MIN_INTERVAL_SECONDS، پیش‌فرض off تا رفتار B2 حفظ شود).
            min_interval_seconds=min(
                60.0, _env_float("RPC_MIN_INTERVAL_SECONDS", 0.0)
            ),
        )

    @property
    def active(self):
        return self._active_total

    @property
    def waiting(self):
        return self._waiting

    def _urgent_waiting(self):
        """True when a P0/P1 waiter is queued (even if its own cap is full)."""
        for priority in (P0_CRITICAL, P1_DELETE):
            groups = self._queues.get(priority) or {}
            for group in groups.values():
                for waiter in group:
                    if not waiter.future.done():
                        return True
        return False

    def _eligible(self, priority, bucket):
        limit = self.total_limit
        if priority == P0_CRITICAL:
            limit += self.critical_reserve
        if self._active_total >= limit:
            return False
        if priority == P0_CRITICAL:
            return True
        cap = self.class_limits.get(bucket)
        if cap is not None and self._active_by_bucket[bucket] >= cap:
            return False
        if priority <= P1_DELETE:
            # Heavy occupancy must not stall delete/antispam. Send+delete
            # still share noncritical_limit; total_limit and class caps stay.
            occupied = self._active_noncritical - int(
                self._active_by_bucket.get("heavy") or 0
            )
            return occupied < self.noncritical_limit
        if self._active_noncritical >= self.noncritical_limit:
            return False
        if priority >= P3_HEAVY and self._urgent_waiting():
            return False
        return True

    def _increment_active(self, priority, bucket, *, request_name="?", chat_key="?", sequence=0):
        self._active_total += 1
        if priority != P0_CRITICAL:
            self._active_noncritical += 1
        self._active_by_bucket[bucket] += 1
        self.stats["admitted"] += 1
        holder = _Holder(
            request_name=str(request_name or "?"),
            bucket=str(bucket),
            chat_key=str(chat_key or "global"),
            priority=int(priority),
            acquired_at=time.perf_counter(),
            sequence=int(sequence or 0),
        )
        self._holders.append(holder)
        if self.logger is not None and self.enabled and not self.shadow:
            try:
                holder.watch = asyncio.get_running_loop().create_task(
                    self._emit_hold_if_slow(holder)
                )
            except RuntimeError:
                holder.watch = None
        return holder

    def format_holders(self, now=None):
        now = time.perf_counter() if now is None else float(now)
        parts = []
        for holder in self._holders:
            held_ms = max(0.0, (now - holder.acquired_at) * 1000.0)
            parts.append(
                f"{holder.bucket}:{holder.request_name}"
                f":chat={holder.chat_key}:held_ms={held_ms:.0f}"
            )
        return parts

    def format_active_by_bucket(self):
        buckets = {"critical": 0, "delete": 0, "send": 0, "heavy": 0,
                   "notice": 0, "other": 0}
        buckets.update(dict(self._active_by_bucket))
        return ",".join(f"{name}:{int(count)}" for name, count in buckets.items() if count)

    def _enqueue(self, waiter):
        groups = self._queues[waiter.priority]
        group = groups.get(waiter.chat_key)
        if group is None:
            group = groups[waiter.chat_key] = deque()
            self._chat_rounds[waiter.priority].append(waiter.chat_key)
        group.append(waiter)
        self._waiting += 1
        self._max_waiting = max(self._max_waiting, self._waiting)
        if self._waiting == 1:
            self._backlog_started = time.perf_counter()
            self._burst_max_waiting = 1
        else:
            self._burst_max_waiting = max(self._burst_max_waiting, self._waiting)

    def _pop_priority(self, priority):
        """Pop one eligible waiter, round-robin between chats of a class."""
        rounds = self._chat_rounds[priority]
        groups = self._queues[priority]
        checks = len(rounds)
        for _ in range(checks):
            chat_key = rounds.popleft()
            group = groups.get(chat_key)
            if group is None:
                continue
            while group and group[0].future.done():
                group.popleft()
                self._waiting = max(0, self._waiting - 1)
            if not group:
                groups.pop(chat_key, None)
                continue
            waiter = group[0]
            if not self._eligible(waiter.priority, waiter.bucket):
                rounds.append(chat_key)
                continue
            group.popleft()
            self._waiting = max(0, self._waiting - 1)
            if group:
                rounds.append(chat_key)
            else:
                groups.pop(chat_key, None)
            return waiter
        return None

    def _pop_next(self):
        """Admit the highest-priority eligible waiter. P1 always beats P3."""
        for priority in (P0_CRITICAL, P1_DELETE, P2_SEND, P3_HEAVY):
            waiter = self._pop_priority(priority)
            if waiter is not None:
                return waiter
        return None

    def _drain(self):
        previous_waiting = self._waiting
        try:
            if not self.enabled or self.shadow:
                return
            while True:
                # _pop_next -> _eligible already enforces total_limit (plus
                # the P0-only reserve).  Looping on eligibility instead of a
                # raw counter keeps the reserve usable after it is enabled.
                waiter = self._pop_next()
                if waiter is None:
                    return
                if waiter.future.done():
                    continue
                waiter.admitted = True
                holder = self._increment_active(
                    waiter.priority,
                    waiter.bucket,
                    request_name=waiter.request_name,
                    chat_key=waiter.chat_key,
                    sequence=waiter.sequence,
                )
                permit = RpcPermit(
                    self, waiter.priority, waiter.bucket, holder=holder
                )
                waiter.future.set_result(permit)
        finally:
            self._emit_drain_if_cleared(previous_waiting)

    async def acquire(self, admission):
        """Wait for admission, or only observe limits in shadow mode."""
        if not isinstance(admission, RpcAdmission):
            raise TypeError("admission must be RpcAdmission")
        # Sending a cosmetic/game response after it sat behind five RPCs is
        # worse than dropping it: it prolongs the backlog and delays mute/ban.
        # Only shed ordinary/background sends. Command and moderation replies
        # are P0 and must remain deliverable even while normal notifications
        # have already filled the send backlog.
        if (not self.shadow and admission.bucket in ("send", "notice")
                and admission.priority != P0_CRITICAL):
            cap = (
                self.max_notice_waiters
                if admission.bucket == "notice"
                else self.max_send_waiters
            )
            if self._waiting >= cap:
                raise RpcOverloadError("outgoing send backlog is full")

        if not self.enabled and not self.shadow:
            # Wrapper normally bypasses this mode. Keep direct use harmless.
            holder = self._increment_active(
                admission.priority,
                admission.bucket,
                request_name=admission.request_name,
                chat_key=admission.chat_key,
            )
            return RpcPermit(
                self, admission.priority, admission.bucket, holder=holder
            )

        if self.shadow:
            if not self._eligible(admission.priority, admission.bucket):
                self.stats["shadow_would_wait"] += 1
            holder = self._increment_active(
                admission.priority,
                admission.bucket,
                request_name=admission.request_name,
                chat_key=admission.chat_key,
            )
            return RpcPermit(
                self, admission.priority, admission.bucket, shadow=True, holder=holder
            )

        loop = asyncio.get_running_loop()
        self._sequence += 1
        waiter = _Waiter(
            priority=admission.priority,
            bucket=admission.bucket,
            request_name=admission.request_name,
            chat_key=admission.chat_key,
            future=loop.create_future(),
            enqueued_at=time.perf_counter(),
            sequence=self._sequence,
        )
        self._enqueue(waiter)
        self._drain()
        watch = None
        if self.logger is not None and not waiter.future.done():
            watch = asyncio.create_task(
                self._emit_blocked_if_waiting(admission, waiter)
            )
        try:
            permit = await waiter.future
        except asyncio.CancelledError:
            self.stats["cancelled_waiters"] += 1
            if waiter.admitted:
                # Cancellation can land after set_result but before this task
                # resumes. Return the transferred slot immediately.
                try:
                    waiter.future.result().release()
                except Exception:
                    pass
            # A cancelled future is lazily removed by the next drain.
            self._drain()
            raise
        finally:
            if watch is not None:
                watch.cancel()

        wait_ms = (time.perf_counter() - waiter.enqueued_at) * 1000
        if wait_ms >= self.wait_log_ms:
            self.stats["waited"] += 1
            if self.logger is not None:
                self.logger.log_info(
                    "RPC GOVERNOR WAIT "
                    f"request={admission.request_name} "
                    f"priority={_PRIORITY_LABELS[admission.priority]} "
                    f"bucket={admission.bucket} chat={admission.chat_key} "
                    f"wait_ms={wait_ms:.1f} active={self._active_total} "
                    f"waiting={self._waiting}"
                )
                self.logger.log_info(
                    "GOVERNOR ACQUIRE "
                    f"request={admission.request_name} "
                    f"bucket={admission.bucket} "
                    f"wait_ms={wait_ms:.1f} "
                    f"active={self._active_total} "
                    f"active_by_bucket={self.format_active_by_bucket() or '-'} "
                    f"waiting={self._waiting}"
                )
        await self._pace_start()
        return permit

    async def _pace_start(self):
        """Optional minimum spacing between RPC call starts.

        Preserved Foxora pacing feature: when ``min_interval_seconds`` is
        positive, every granted permit waits until the pacing chain allows a
        new start. The pacing delay happens while holding the permit, so
        backpressure stays honest: queued requests are still independent
        network operations (nothing is merged or dropped by this pacing —
        the Soroush Plus server simply sees them slightly later).
        """
        interval = self.min_interval_seconds
        if interval <= 0.0 or not self.enabled or self.shadow:
            return
        now = time.monotonic()
        base = max(self._next_start_at, now)
        wait_for = base - now
        if wait_for > 0.0:
            await asyncio.sleep(wait_for)
        self._next_start_at = max(base, time.monotonic()) + interval

    def format_waiting_by_bucket(self):
        counts = {
            "critical": 0, "delete": 0, "send": 0, "heavy": 0,
            "notice": 0, "other": 0,
        }
        for groups in self._queues.values():
            for group in groups.values():
                for waiter in group:
                    if waiter.future.done():
                        continue
                    bucket = str(getattr(waiter, "bucket", "other") or "other")
                    counts[bucket] = counts.get(bucket, 0) + 1
        return ",".join(
            f"{name}:{counts[name]}"
            for name in ("critical", "delete", "send", "heavy", "notice",
                         "other")
            if counts.get(name)
        )

    def _pending_brief(self):
        """Read-only sender pending view. Never mutates RPC/queue state."""
        sender = getattr(self, "_observe_sender", None)
        ping_age = 0.0
        sender_pending = 0
        by_type = "-"
        try:
            from modules.outgoing_profiler import pending_rpc_snapshot
            snapshot = pending_rpc_snapshot(sender)
            sender_pending = int(snapshot.get("sender_pending") or 0)
            type_map = snapshot.get("sender_pending_by_type") or {}
            by_type = ",".join(
                f"{key}:{value}" for key, value in type_map.items() if value
            ) or "-"
            for row in snapshot.get("sender_pending_rows") or ():
                name = str(row.get("request_type") or "")
                if row.get("is_keepalive") or "Ping" in name:
                    ping_age = max(ping_age, float(row.get("age_ms") or 0.0))
        except Exception:
            pass
        return ping_age, sender_pending, by_type

    def _bucket_limit(self, priority, bucket):
        if priority == P0_CRITICAL:
            return self.total_limit
        return self.class_limits.get(bucket, self.noncritical_limit)

    def _cancel_holder_watch(self, holder):
        watch = getattr(holder, "watch", None) if holder is not None else None
        if watch is None:
            return
        holder.watch = None
        if not watch.done():
            watch.cancel()

    def _emit_drain_if_cleared(self, previous_waiting):
        if self._waiting != 0:
            return
        started = self._backlog_started
        burst_max = self._burst_max_waiting
        self._backlog_started = None
        self._burst_max_waiting = 0
        if previous_waiting <= 0 or started is None or self.logger is None:
            return
        burst_ms = (time.perf_counter() - started) * 1000.0
        if burst_ms < GOVERNOR_BLOCKED_MS and burst_max < 2:
            return
        ping_age, sender_pending, by_type = self._pending_brief()
        try:
            self.logger.log_info(
                "GOVERNOR DRAIN "
                f"waiting=0 burst_ms={burst_ms:.1f} "
                f"max_waiting={burst_max} active={self._active_total} "
                f"active_by_bucket={self.format_active_by_bucket() or '-'} "
                f"sender_pending={sender_pending} ping_age_ms={ping_age:.0f} "
                f"pending_by_type={by_type}"
            )
        except Exception:
            pass

    async def _emit_hold_if_slow(self, holder):
        try:
            await asyncio.sleep(max(0.001, GOVERNOR_BLOCKED_MS / 1000.0))
        except asyncio.CancelledError:
            return
        if self.logger is None or holder not in self._holders:
            return
        held_ms = max(0.0, (time.perf_counter() - holder.acquired_at) * 1000.0)
        ping_age, sender_pending, by_type = self._pending_brief()
        holders = self.format_holders()
        try:
            self.logger.log_error(
                "GOVERNOR HOLD SLOW "
                f"bucket={holder.bucket} request={holder.request_name} "
                f"chat={holder.chat_key} held_ms={held_ms:.1f} "
                f"reason=rpc_in_flight "
                f"active={self._active_total} waiting={self._waiting} "
                f"active_by_bucket={self.format_active_by_bucket() or '-'} "
                f"waiting_by_bucket={self.format_waiting_by_bucket() or '-'} "
                f"limit={self._bucket_limit(holder.priority, holder.bucket)} "
                f"total_limit={self.total_limit} "
                f"sender_pending={sender_pending} ping_age_ms={ping_age:.0f} "
                f"pending_by_type={by_type} "
                f"holders=[{','.join(holders) if holders else '-'}]"
            )
        except Exception:
            pass

    async def _emit_blocked_if_waiting(self, admission, waiter):
        try:
            await asyncio.sleep(GOVERNOR_BLOCKED_MS / 1000.0)
        except asyncio.CancelledError:
            return
        if waiter.future.done() or waiter.admitted or self.logger is None:
            return
        wait_ms = (time.perf_counter() - waiter.enqueued_at) * 1000.0
        bucket_limit = self._bucket_limit(admission.priority, admission.bucket)
        holders = self.format_holders()
        ping_age, sender_pending, by_type = self._pending_brief()
        self.logger.log_error(
            "GOVERNOR BLOCKED "
            f"bucket={admission.bucket} "
            f"request={admission.request_name} "
            f"wait_ms={wait_ms:.1f} "
            f"active={self._active_total} "
            f"limit={bucket_limit} "
            f"total_limit={self.total_limit} "
            f"noncritical_limit={self.noncritical_limit} "
            f"active_by_bucket={self.format_active_by_bucket() or '-'} "
            f"waiting={self._waiting} "
            f"waiting_by_bucket={self.format_waiting_by_bucket() or '-'} "
            f"sender_pending={sender_pending} ping_age_ms={ping_age:.0f} "
            f"pending_by_type={by_type} "
            f"holders=[{','.join(holders) if holders else '-'}]"
        )

    def _release(self, priority, bucket, holder=None):
        hold_ms = 0.0
        if holder is not None:
            hold_ms = max(0.0, (time.perf_counter() - holder.acquired_at) * 1000.0)
            self._cancel_holder_watch(holder)
            try:
                self._holders.remove(holder)
            except ValueError:
                holder = None
        if holder is None:
            for item in list(self._holders):
                if item.priority == priority and item.bucket == bucket:
                    hold_ms = max(
                        0.0, (time.perf_counter() - item.acquired_at) * 1000.0
                    )
                    self._cancel_holder_watch(item)
                    self._holders.remove(item)
                    holder = item
                    break
        if self._active_total > 0:
            self._active_total -= 1
        if priority != P0_CRITICAL and self._active_noncritical > 0:
            self._active_noncritical -= 1
        if self._active_by_bucket.get(bucket, 0) > 0:
            self._active_by_bucket[bucket] -= 1
        self.stats["released"] += 1
        if hold_ms >= GOVERNOR_BLOCKED_MS and self.logger is not None:
            name = holder.request_name if holder is not None else "?"
            ping_age, sender_pending, by_type = self._pending_brief()
            self.logger.log_info(
                "GOVERNOR RELEASE "
                f"request={name} bucket={bucket} "
                f"hold_ms={hold_ms:.1f} reason=rpc_in_flight "
                f"active={self._active_total} waiting={self._waiting} "
                f"waiting_by_bucket={self.format_waiting_by_bucket() or '-'} "
                f"sender_pending={sender_pending} ping_age_ms={ping_age:.0f} "
                f"pending_by_type={by_type}"
            )
        self._drain()

    def snapshot(self):
        waiting_by_priority = {}
        waiting_by_bucket = {
            "critical": 0, "delete": 0, "send": 0, "heavy": 0,
            "notice": 0, "other": 0,
        }
        for priority, groups in self._queues.items():
            waiting_by_priority[_PRIORITY_LABELS[priority]] = sum(
                sum(1 for waiter in group if not waiter.future.done())
                for group in groups.values()
            )
            for group in groups.values():
                for waiter in group:
                    if waiter.future.done():
                        continue
                    bucket = str(getattr(waiter, "bucket", "other") or "other")
                    waiting_by_bucket[bucket] = waiting_by_bucket.get(bucket, 0) + 1
        return {
            "enabled": self.enabled,
            "shadow": self.shadow,
            "total_limit": self.total_limit,
            "noncritical_limit": self.noncritical_limit,
            "active": self._active_total,
            "active_noncritical": self._active_noncritical,
            "active_by_bucket": dict(self._active_by_bucket),
            "waiting": sum(waiting_by_priority.values()),
            "waiting_by_priority": waiting_by_priority,
            "waiting_by_bucket": waiting_by_bucket,
            "max_waiting": self._max_waiting,
            "holders": self.format_holders(),
            "stats": dict(self.stats),
        }

    def mode_label(self):
        if self.shadow:
            return "shadow"
        return "enabled" if self.enabled else "disabled"
