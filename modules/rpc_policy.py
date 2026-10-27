"""Single source of truth for RPC deadlines, FloodWait backoff and tracing.

Why this module exists
----------------------
Before this change, ``60.0`` (and 20/15/10/8/5 second deadlines) were scattered
across ``connection_guard``, ``moderation_queue``, ``admin_actions``,
``message_delete_queue`` and the SPlusthon client itself.  Worse, SPlusthon's
``UserMethods._call`` silently slept *inside* the RPC await for any
``FloodWaitError`` shorter than ``client.flood_sleep_threshold`` (default 60s),
and cached that wait per TL constructor id in ``_flood_waited_requests``.  One
rate-limited ``SendMessageRequest`` therefore made *every* later send in *every*
group sleep or raise before it ever reached the socket -- while
``GetDifferenceRequest`` kept answering in 60-80ms.

This module is deliberately dependency-light (no splusthon import) so every
layer -- profiler, timeout wrapper, governor, queues -- reads the same knobs.
All values come from the environment, so a deployment can tune them without a
code change.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import re
import time
from dataclasses import dataclass


# Propagated by the outermost resilience wrapper (see rpc_resilience.py) and
# inherited by every task created below it.  ``outgoing_profiler`` uses it to
# pair AWAIT START with its one terminal line; ``connection_guard`` uses it to
# attach the cancellable inner RPC task to the orphan watchdog.
RPC_REQUEST_ID = contextvars.ContextVar("rpc_request_id", default=None)
RPC_ATTEMPT = contextvars.ContextVar("rpc_attempt", default=0)


def _env_float(name, default, minimum=0.0, maximum=None):
    try:
        value = max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = float(default)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_int(name, default, minimum=0, maximum=None):
    try:
        value = max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = int(default)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return bool(default)
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


# Class names that mean "server asked us to wait".  Matched structurally (no
# splusthon import) so the fix keeps working across client versions/forks.
_WAIT_NAME_MARKERS = ("flood", "slowmode", "wait")
_WAIT_SECONDS_RE = re.compile(
    r"(?:wait(?:\s+of)?|flood_wait_|slow_mode_wait_)\s*(\d+(?:\.\d+)?)",
    re.I,
)


def flood_wait_seconds(error) -> float | None:
    """Return the server-requested wait, or ``None`` for a non-wait error.

    Handles ``FloodWaitError`` / ``FloodPremiumWaitError`` /
    ``SlowModeWaitError`` / ``FloodTestPhoneWaitError`` by class name and by
    message text, without importing the client library.
    """
    if error is None:
        return None
    name = error.__class__.__name__.lower()
    text = str(error)
    if not any(marker in name for marker in _WAIT_NAME_MARKERS) and \
            "wait of" not in text.lower():
        return None
    for attribute in ("seconds", "value", "wait_seconds", "capture"):
        value = getattr(error, attribute, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    match = _WAIT_SECONDS_RE.search(text)
    return float(match.group(1)) if match else None


_TIMEOUT_NAMES = frozenset({
    "TimeoutError", "RpcTimeout", "FuturesTimeoutError",
    "AsyncioTimeoutError", "SendTimeoutError",
})


def is_timeout_error(error) -> bool:
    if error is None:
        return False
    return isinstance(error, asyncio.TimeoutError) or \
        error.__class__.__name__ in _TIMEOUT_NAMES


def classify_outcome(error):
    """Map a caught exception to exactly one terminal CONN TRACE event."""
    if error is None:
        return "RESPONSE", "success"
    if isinstance(error, asyncio.CancelledError):
        return "CANCELLED", "cancelled"
    if is_timeout_error(error):
        return "TIMEOUT", f"timeout:{error.__class__.__name__}"
    return "ERROR", f"failed:{error.__class__.__name__}"


def new_request_id() -> str:
    """Stable, human-greppable id (same shape as the production log samples)."""
    task = asyncio.current_task() if _has_running_loop() else None
    return f"{id(task) if task is not None else 0:x}-{time.monotonic_ns():x}"


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@dataclass(frozen=True)
class RpcPolicy:
    # Per-class explicit deadlines.  The send deadline is intentionally much
    # shorter than the old global 60s: a normal Soroush send answers in
    # 60-400ms, so 15s is already ~40x the observed p100.
    send_timeout_s: float = 15.0
    moderation_timeout_s: float = 20.0
    read_timeout_s: float = 20.0
    default_timeout_s: float = 60.0

    # 0 => SPlusthon never sleeps inside _call; FloodWaitError is raised at
    # once and handled by rpc_resilience (backoff + retry outside the governor).
    flood_sleep_threshold_s: float = 0.0
    flood_max_retries: int = 2
    flood_backoff_base_s: float = 0.75
    flood_backoff_cap_s: float = 30.0
    flood_wait_cap_s: float = 30.0
    flood_jitter_s: float = 0.35

    # Orphan-await watchdog.  Fires at timeout_for(request) + grace.
    watchdog_interval_s: float = 2.0
    watchdog_grace_s: float = 5.0
    watchdog_cancel: bool = True

    # Every AWAIT START must get exactly one terminal line, not only the
    # GetDifference/GetChannelDifference pair or post-reconnect RPCs.
    trace_all: bool = True
    # Log slow-but-successful responses at ERROR level instead of INFO.
    trace_slow_ms: float = 2000.0

    @classmethod
    def from_environment(cls) -> "RpcPolicy":
        return cls(
            send_timeout_s=_env_float("BOT_RPC_SEND_TIMEOUT_S", 15.0, 1.0, 300.0),
            moderation_timeout_s=_env_float(
                "BOT_RPC_MODERATION_TIMEOUT_S", 20.0, 1.0, 300.0
            ),
            read_timeout_s=_env_float(
                "BOT_RPC_READ_TIMEOUT_S", 20.0, 1.0, 300.0
            ),
            default_timeout_s=_env_float(
                "BOT_RPC_TIMEOUT_S", 60.0, 1.0, 600.0
            ),
            flood_sleep_threshold_s=_env_float(
                "BOT_RPC_FLOOD_SLEEP_THRESHOLD_S", 0.0, 0.0, 60.0
            ),
            flood_max_retries=_env_int("BOT_RPC_FLOOD_MAX_RETRIES", 2, 0, 5),
            flood_backoff_base_s=_env_float(
                "BOT_RPC_FLOOD_BACKOFF_BASE_S", 0.75, 0.0, 30.0
            ),
            flood_backoff_cap_s=_env_float(
                "BOT_RPC_FLOOD_BACKOFF_CAP_S", 30.0, 1.0, 120.0
            ),
            flood_wait_cap_s=_env_float(
                "BOT_RPC_FLOOD_WAIT_CAP_S", 30.0, 1.0, 300.0
            ),
            flood_jitter_s=_env_float(
                "BOT_RPC_FLOOD_JITTER_S", 0.35, 0.0, 5.0
            ),
            watchdog_interval_s=_env_float(
                "BOT_RPC_WATCHDOG_INTERVAL_S", 2.0, 0.2, 60.0
            ),
            watchdog_grace_s=_env_float(
                "BOT_RPC_WATCHDOG_GRACE_S", 5.0, 0.0, 60.0
            ),
            watchdog_cancel=_env_bool("BOT_RPC_WATCHDOG_CANCEL", True),
            trace_all=_env_bool("BOT_RPC_TRACE_ALL", True),
            trace_slow_ms=_env_float("BOT_RPC_TRACE_SLOW_MS", 2000.0, 0.0),
        )

    def timeout_for(self, request) -> float:
        """Deadline for one TL request, based on the governor's own buckets.

        ``classify_request`` is the project's existing name-based classifier;
        reusing it means a newly-added request type cannot silently fall back
        to the 60s net.
        """
        try:
            from modules.rpc_governor import classify_request
            bucket = classify_request(request).bucket
        except Exception:
            bucket = "other"
        return {
            "send": self.send_timeout_s,
            "notice": self.send_timeout_s,
            "critical": self.moderation_timeout_s,
            "delete": self.moderation_timeout_s,
            "heavy": self.read_timeout_s,
        }.get(bucket, self.default_timeout_s)

    def describe(self) -> str:
        return (
            f"send={self.send_timeout_s:g}s "
            f"moderation={self.moderation_timeout_s:g}s "
            f"read={self.read_timeout_s:g}s "
            f"default={self.default_timeout_s:g}s "
            f"flood_threshold={self.flood_sleep_threshold_s:g}s "
            f"flood_retries={self.flood_max_retries} "
            f"flood_cap={self.flood_wait_cap_s:g}s "
            f"orphan_grace={self.watchdog_grace_s:g}s"
        )
