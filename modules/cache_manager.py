"""Centralized Cache Layer and Permission Circuit Breaker for SPlusthon workloads.

Provides:
1. ``PermissionCircuitBreaker``: Trips on ``ChatAdminRequiredError`` / permission
   denials per group to prevent doomed RPC retries and save network roundtrips.
2. ``TtlCache``: High-performance, thread-safe / asyncio-safe in-memory cache
   with automatic TTL expiration and capacity management.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:
    from modules.group_id import normalize_group_id
    _HAS_NORMALIZE = True
except ImportError:
    _HAS_NORMALIZE = False
    def normalize_group_id(value):
        try:
            return str(int(value))
        except Exception:
            return str(value)


def _safe_chat_key(chat_id) -> str:
    """Extract a canonical string key for any chat_id or InputPeer object."""
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
    return str(chat_id)


class TtlCache:
    """In-memory key-value cache with per-item TTL and max size eviction."""

    def __init__(self, default_ttl: float = 300.0, max_size: int = 2000):
        self.default_ttl = float(default_ttl)
        self.max_size = int(max_size)
        self._data: OrderedDict[Any, Tuple[Any, float]] = OrderedDict()
        self.stats = {"hits": 0, "misses": 0, "sets": 0, "evictions": 0}

    def get(self, key: Any, default: Any = None) -> Any:
        now = time.monotonic()
        if key in self._data:
            val, expires_at = self._data[key]
            if expires_at > now:
                self._data.move_to_end(key)
                self.stats["hits"] += 1
                return val
            else:
                del self._data[key]
        self.stats["misses"] += 1
        return default

    def set(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        now = time.monotonic()
        effective_ttl = float(ttl) if ttl is not None else self.default_ttl
        expires_at = now + effective_ttl

        if key in self._data:
            del self._data[key]
        elif len(self._data) >= self.max_size:
            # Evict oldest entry
            self._data.popitem(last=False)
            self.stats["evictions"] += 1

        self._data[key] = (value, expires_at)
        self.stats["sets"] += 1

    def delete(self, key: Any) -> bool:
        if key in self._data:
            del self._data[key]
            return True
        return False

    def clear(self) -> None:
        self._data.clear()

    def cleanup_expired(self) -> int:
        now = time.monotonic()
        expired_keys = [k for k, (_v, exp) in self._data.items() if exp <= now]
        for k in expired_keys:
            del self._data[k]
        return len(expired_keys)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "size": len(self._data),
            "max_size": self.max_size,
            "default_ttl": self.default_ttl,
            "stats": dict(self.stats),
        }


STATE_CLOSED = "CLOSED"      # Normal operation: bot has admin rights
STATE_OPEN = "OPEN"          # Other confirmed permission failures; fail fast per chat
STATE_HALF_OPEN = "HALF_OPEN"  # Testing: allow 1 probe operation


def is_permission_error(error) -> bool:
    """Return True only for genuine admin / permission denial errors from MTProto.

    Entity resolution failures, invalid peer IDs, network timeouts, TypeError,
    KeyError, and unsupported requests MUST NOT be classified as permission errors.
    """
    if error is None:
        return False
    if isinstance(error, (ValueError, KeyError, IndexError, TypeError, AttributeError)):
        return False
    name = error.__class__.__name__.lower()
    text = str(error).lower()

    # Exclude entity / peer lookup errors and network timeouts
    if any(marker in name or marker in text for marker in (
        "peeridinvalid", "channelinvalid", "entity", "notfound",
        "chatidinvalid", "cannot find", "could not find", "user_id_invalid",
        "peer_id_invalid", "notsupported", "not_supported", "timeout",
    )):
        return False

    # Genuine MTProto permission errors
    if any(marker in name for marker in (
        "chatadminrequired", "useradmininvalid", "adminrankinvalid",
        "rightforbidden", "chatadminrightsneeded", "permission",
    )):
        return True

    # Checked string indicators in RPC error message
    if (
        "admin_required" in text
        or "admin required" in text
        or "chat_admin_required" in text
        or "chatadminrequired" in text
    ):
        return True

    return False


def is_chat_admin_required_error(error) -> bool:
    """True when an RPC says this bot lacks admin rights in the chat.

    This is an expected, operation-local failure (for example, a ban in a
    group where the bot is not an admin). It must be logged and returned to
    that caller, not used to open a chat-wide breaker that suppresses unrelated
    work such as sends, reads, or commands.
    """
    seen = set()
    current = error
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = current.__class__.__name__.lower()
        text = str(current).lower()
        if (
            "chatadminrequired" in name
            or "chat_admin_required" in name
            or "chat admin required" in text
            or "chat_admin_required" in text
            or "chatadminrequired" in text
        ):
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return False


@dataclass
class BreakerRecord:
    state: str = STATE_CLOSED
    tripped_at: float = 0.0
    cooldown: float = 180.0
    failure_count: int = 0
    last_error: str = ""
    probe_in_flight: bool = False


class PermissionCircuitBreaker:
    """Circuit breaker per chat to prevent continuous failed admin RPCs.

    Confirmed permission errors other than ``ChatAdminRequiredError`` may trip
    this breaker to ``OPEN`` for ``cooldown_seconds`` (default: 180s).
    ``ChatAdminRequiredError`` is deliberately operation-local and never opens
    this breaker.
    During this window, subsequent moderation and deletion RPCs for that group
    fail-fast locally (0ms) instead of wasting 150-400ms on doomed network calls.
    """

    _instance = None
    MAX_BREAKERS = 2000

    def __init__(self, default_cooldown: float = 180.0, logger=None):
        self.default_cooldown = float(default_cooldown)
        self.logger = logger
        self._breakers: Dict[str, BreakerRecord] = {}
        self.stats = {
            "tripped": 0,
            "blocked_calls": 0,
            "recovered": 0,
            "probes_allowed": 0,
        }

    @classmethod
    def get_default(cls, logger=None):
        if cls._instance is None:
            cls._instance = cls(logger=logger)
        return cls._instance

    def can_execute(self, chat_id, action: str = "admin_action") -> bool:
        """Check if an administrative operation is permitted for this chat."""
        key = _safe_chat_key(chat_id)
        record = self._breakers.get(key)
        if record is None or record.state == STATE_CLOSED:
            return True

        now = time.monotonic()
        # If cooldown has elapsed, transition from OPEN to HALF_OPEN
        if record.state == STATE_OPEN:
            if now - record.tripped_at >= record.cooldown:
                record.state = STATE_HALF_OPEN
                record.probe_in_flight = True
                self.stats["probes_allowed"] += 1
                if self.logger:
                    self.logger.log_info(
                        f"CIRCUIT BREAKER HALF_OPEN chat_id={key} "
                        f"action={action} probing permission status"
                    )
                return True
            else:
                self.stats["blocked_calls"] += 1
                return False

        if record.state == STATE_HALF_OPEN:
            # Allow only one probe at a time; others fail fast
            if not record.probe_in_flight:
                record.probe_in_flight = True
                self.stats["probes_allowed"] += 1
                return True
            self.stats["blocked_calls"] += 1
            return False

        return True

    def record_success(self, chat_id) -> None:
        """Record successful execution of an admin operation, resetting breaker."""
        key = _safe_chat_key(chat_id)
        record = self._breakers.get(key)
        if record is not None and record.state != STATE_CLOSED:
            self.stats["recovered"] += 1
            if self.logger:
                self.logger.log_info(
                    f"CIRCUIT BREAKER RECOVERED chat_id={key} "
                    "permissions confirmed, state reset to CLOSED"
                )
            # Recovered chats do not need a permanent CLOSED row.
            self._breakers.pop(key, None)

    def cleanup_expired(self, now=None) -> int:
        """Drop recovered/expired records so the map cannot grow without bound."""
        now = time.monotonic() if now is None else float(now)
        dropped = 0
        for key, record in list(self._breakers.items()):
            if record.state == STATE_CLOSED:
                self._breakers.pop(key, None)
                dropped += 1
                continue
            if record.state == STATE_OPEN and (now - record.tripped_at) >= (record.cooldown * 2):
                self._breakers.pop(key, None)
                dropped += 1
        while len(self._breakers) > self.MAX_BREAKERS:
            self._breakers.pop(next(iter(self._breakers)), None)
            dropped += 1
        return dropped

    def record_failure(
        self, chat_id, error=None, cooldown: Optional[float] = None
    ) -> None:
        """Trip only on permission failures that justify chat-wide suppression.

        ChatAdminRequiredError is deliberately excluded: the caller logs and
        cancels that one operation, while independent RPC buckets keep running.
        """
        if is_chat_admin_required_error(error):
            if self.logger:
                self.logger.log_info(
                    "CIRCUIT BREAKER SKIP CHAT_ADMIN_REQUIRED "
                    f"chat_id={_safe_chat_key(chat_id)} "
                    "scope=operation_only; unrelated_rpc_allowed=True"
                )
            return
        if error is not None and not is_permission_error(error):
            if self.logger:
                self.logger.log_info(
                    f"CIRCUIT BREAKER IGNORE NON-PERMISSION ERROR chat_id={_safe_chat_key(chat_id)} "
                    f"error={error!r}"
                )
            return

        key = _safe_chat_key(chat_id)
        record = self._breakers.get(key)
        now = time.monotonic()
        err_str = str(error or "ChatAdminRequiredError")
        effective_cooldown = (
            float(cooldown) if cooldown is not None else self.default_cooldown
        )

        if record is None:
            record = BreakerRecord()
            self._breakers[key] = record
            if len(self._breakers) > self.MAX_BREAKERS:
                self.cleanup_expired()

        record.state = STATE_OPEN
        record.tripped_at = now
        record.cooldown = effective_cooldown
        record.failure_count += 1
        record.last_error = err_str
        record.probe_in_flight = False
        self.stats["tripped"] += 1

        if self.logger:
            self.logger.log_error(
                f"CIRCUIT BREAKER TRIPPED chat_id={key} "
                f"cooldown={effective_cooldown:.0f}s error={err_str}"
            )

    def reset(self, chat_id=None) -> None:
        """Manually clear circuit breaker state for a chat or all chats."""
        if chat_id is None:
            self._breakers.clear()
        else:
            key = _safe_chat_key(chat_id)
            self._breakers.pop(key, None)

    def is_open(self, chat_id) -> bool:
        key = _safe_chat_key(chat_id)
        record = self._breakers.get(key)
        if record is None:
            return False
        if record.state == STATE_OPEN:
            return (time.monotonic() - record.tripped_at) < record.cooldown
        return False

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        active_tripped = {
            k: {
                "state": r.state,
                "remaining_s": max(0.0, r.cooldown - (now - r.tripped_at)),
                "failure_count": r.failure_count,
                "last_error": r.last_error,
            }
            for k, r in self._breakers.items()
            if r.state == STATE_OPEN and (now - r.tripped_at) < r.cooldown
        }
        return {
            "total_tracked": len(self._breakers),
            "active_open_breakers": len(active_tripped),
            "tripped_chats": active_tripped,
            "stats": dict(self.stats),
        }
