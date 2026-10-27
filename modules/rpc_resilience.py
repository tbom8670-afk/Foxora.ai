"""FloodWait backoff + retry for every outgoing RPC (the *outermost* `_call`).

Architecture decision
---------------------
SPlusthon (a Telethon fork) owns the FloodWait policy *inside* its
``UserMethods._call``::

    if e.seconds <= self.flood_sleep_threshold:   # client-wide, default 60
        await asyncio.sleep(e.seconds)            # INSIDE the caller's await
    ...
    if r.CONSTRUCTOR_ID in self._flood_waited_requests:
        await asyncio.sleep(diff)                 # early, per request TYPE

That is the wrong layer for a multi-group bot.  The sleep happens while the
caller holds its per-chat gate slot, its governor permit and its queue worker;
and the cached wait is keyed by TL constructor id, so one rate-limited
``SendMessageRequest`` in group A stalls notices in group B while
``GetDifferenceRequest`` keeps replying in 70ms.  It also produces *zero* log
lines in the bot logger -- exactly the "orphan AWAIT START" symptom.

We set ``client.flood_sleep_threshold = 0`` (see core._make_client) so the
library raises ``FloodWaitError`` immediately, and this wrapper -- installed
OUTSIDE the gate/governor wrapper -- owns the wait:

* each attempt re-enters ``orig`` => re-acquires gate + governor => no slot is
  held while sleeping (a real requeue with backoff, not a lock held across a
  sleep);
* ``asyncio.sleep`` never blocks the event loop;
* a shared per-request-type (or per-chat for SlowModeWait) cooldown stops
  dozens of groups from retrying at the same instant;
* waits longer than ``flood_wait_cap_s`` are *not* slept -- they are surfaced
  to the caller/log immediately, so a moderation worker is never held for
  minutes;
* bounded retries, after which the original error propagates (callers such as
  ModerationQueue already have their own final retry/failure handling).

Every attempt gets its own ``request_id`` (``<root>#<attempt>``) via a
contextvar; ``outgoing_profiler`` uses it to guarantee exactly one terminal
CONN TRACE line per AWAIT START.
"""
from __future__ import annotations

import asyncio
import functools
import random
import time

from modules.rpc_policy import (
    RPC_ATTEMPT,
    RPC_REQUEST_ID,
    RpcPolicy,
    flood_wait_seconds,
    new_request_id,
)


def _request_name(request) -> str:
    return type(request).__name__ if request is not None else "unknown"


def _request_chat(request):
    for attr in ("peer", "channel", "entity", "chat_id"):
        value = getattr(request, attr, None)
        if value is None:
            continue
        for nested in ("channel_id", "chat_id", "user_id", "id"):
            found = getattr(value, nested, None)
            if found is not None:
                return found
        if isinstance(value, (int, str)):
            return value
    return "global"


class FloodController:
    """Bounded, shared cooldown clock.  Pure asyncio, no locks needed."""

    def __init__(self, policy: RpcPolicy, logger=None):
        self.policy = policy
        self.logger = logger
        self._cooldowns = {}          # key -> monotonic deadline
        self._last_log = {}           # key -> monotonic, rate-limit our logs
        self.stats = {"floods": 0, "retries": 0, "capped": 0}

    @staticmethod
    def key_for(request_name: str, chat_id, error):
        # SLOW_MODE_WAIT is chat-specific; a normal FLOOD_WAIT is account-wide
        # in SPlusthon's own _flood_waited_requests (keyed by constructor id),
        # so a per-type key mirrors the server's real constraint.
        if "slowmode" in error.__class__.__name__.lower():
            return ("chat", request_name, str(chat_id))
        return ("type", request_name)

    def remaining(self, key) -> float:
        return max(0.0, self._cooldowns.get(key, 0.0) - time.monotonic())

    def plan_retry(self, key, seconds: float, attempt: int):
        """Return delay in seconds, or ``None`` when the wait is too long.

        All callers hitting the same key converge on one shared deadline; a
        small per-caller jitter avoids a synchronised burst when it expires.
        """
        wait = max(0.0, float(seconds or 0.0))
        if wait > self.policy.flood_wait_cap_s:
            self.stats["capped"] += 1
            return None
        backoff = min(
            self.policy.flood_backoff_cap_s,
            self.policy.flood_backoff_base_s * (2 ** max(0, attempt)),
        )
        now = time.monotonic()
        existing = self._cooldowns.get(key, 0.0)
        candidate = now + max(wait, backoff)
        # Jitter only when we actually move the deadline forward.  Otherwise
        # dozens of groups arriving during one cooldown would each add another
        # jitter slice and push the shared deadline further and further out.
        if candidate > existing:
            candidate += random.uniform(0.0, self.policy.flood_jitter_s)
        deadline = max(existing, candidate)
        self._cooldowns[key] = deadline
        self.stats["floods"] += 1
        self.stats["retries"] += 1
        return max(0.0, deadline - now)

    def snapshot(self):
        now = time.monotonic()
        return {
            "active_cooldowns": sum(
                1 for deadline in self._cooldowns.values() if deadline > now
            ),
            "stats": dict(self.stats),
        }


def install_rpc_resilience(client, logger=None, policy=None, controller=None):
    """Wrap ``client._call`` once; returns the process-wide FloodController."""
    original = getattr(client, "_call", None)
    if original is None or getattr(original, "_rpc_resilience", False):
        return getattr(original, "_rpc_flood_controller", None) or controller

    policy = policy or RpcPolicy.from_environment()
    controller = controller or FloodController(policy, logger)
    log_info = getattr(logger, "log_info", None) if logger is not None else None
    log_error = getattr(logger, "log_error", None) if logger is not None else None

    def _log_info(line):
        if log_info is not None:
            try:
                log_info(line)
            except Exception:
                pass

    def _log_error(line):
        if log_error is not None:
            try:
                log_error(line)
            except Exception:
                pass

    @functools.wraps(original)
    async def wrapped(sender, request, ordered=False,
                      flood_sleep_threshold=None):
        name = _request_name(request)
        chat_id = _request_chat(request)
        root_id = RPC_REQUEST_ID.get() or new_request_id()
        threshold = (
            policy.flood_sleep_threshold_s
            if flood_sleep_threshold is None
            else flood_sleep_threshold
        )
        max_attempts = policy.flood_max_retries + 1
        for attempt in range(max_attempts):
            request_id = (
                root_id if max_attempts == 1 else f"{root_id}#{attempt}"
            )
            id_token = RPC_REQUEST_ID.set(request_id)
            attempt_token = RPC_ATTEMPT.set(attempt)
            try:
                return await original(
                    sender,
                    request,
                    ordered=ordered,
                    flood_sleep_threshold=threshold,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                seconds = flood_wait_seconds(error)
                if seconds is None or attempt >= policy.flood_max_retries:
                    raise
                key = controller.key_for(name, chat_id, error)
                delay = controller.plan_retry(key, seconds, attempt)
                if delay is None:
                    _log_error(
                        "RPC FLOOD WAIT CAPPED "
                        f"request={name} request_id={request_id} "
                        f"chat_id={chat_id} wait_s={seconds:.1f} "
                        f"cap_s={policy.flood_wait_cap_s:g} action=raise"
                    )
                    raise
                _log_info(
                    "RPC FLOOD WAIT "
                    f"request={name} request_id={request_id} "
                    f"chat_id={chat_id} wait_s={seconds:.1f} "
                    f"backoff_s={delay:.2f} attempt={attempt + 1} "
                    f"max_attempts={max_attempts} "
                    f"error={error.__class__.__name__} "
                    f"cooldowns={controller.snapshot()['active_cooldowns']}"
                )
                # Non-blocking.  Gate/governor permits were released by the
                # inner wrappers' finally blocks before we get here.
                await asyncio.sleep(delay)
            finally:
                RPC_REQUEST_ID.reset(id_token)
                RPC_ATTEMPT.reset(attempt_token)

    wrapped._rpc_resilience = True
    wrapped._rpc_flood_controller = controller
    wrapped._rpc_policy = policy
    try:
        client._call = wrapped
    except (AttributeError, TypeError):
        return controller
    _log_info(f"RPC RESILIENCE READY {policy.describe()}")
    return controller
