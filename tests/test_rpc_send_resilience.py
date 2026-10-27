"""Regression tests for the send-path / FloodWait / orphan-await fix.

Run: python3 -m pytest tests/test_rpc_send_resilience.py -q
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from modules import connection_guard as cg
from modules import outgoing_profiler as op
from modules.outgoing_profiler import (
    instrument_client,
    start_orphan_watchdog,
    stop_orphan_watchdog,
)
from modules.outgoing_sender import (
    OutgoingSender,
    _SEND_CHANNEL,
    _wrap_call_with_gate,
    install as install_outgoing_sender,
)
from modules.rpc_governor import RpcGovernor, classify_request
from modules.rpc_policy import RpcPolicy
from modules.rpc_resilience import install_rpc_resilience


class Logger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))

    def text(self):
        return "\n".join(self.infos + self.errors)


class SendMessageRequest:
    def __init__(self, peer=1):
        self.peer = peer


class GetDifferenceRequest:
    pass


class FloodWaitError(Exception):
    """Same shape as splusthon.errors.FloodWaitError (has .seconds)."""

    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


def _trace(logger, event):
    return [line for line in logger.infos
            if line.startswith(f"CONN TRACE {event}")]


def _field(line, key):
    for part in line.split()[2:]:
        if part.startswith(key + "="):
            return part.split("=", 1)[1]
    return None


class BaseClient:
    def __init__(self):
        self._sender = SimpleNamespace(_pending_state={}, _ping=None)
        self.calls = []


# ---------------------------------------------------------------------------
# 1) Observability: EVERY AWAIT START gets exactly one terminal line
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["success", "error", "cancelled"])
def test_send_message_always_has_a_terminal_conn_trace(mode):
    class Client(BaseClient):
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            self.calls.append(type(request).__name__)
            if mode == "error":
                raise FloodWaitError(7)
            if mode == "cancelled":
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            return "ok"

    async def scenario():
        logger = Logger()
        client = Client()
        policy = RpcPolicy(send_timeout_s=0.05, watchdog_grace_s=0)
        instrument_client(client, logger, policy=policy)
        if mode == "cancelled":
            task = asyncio.create_task(
                client._call(client._sender, SendMessageRequest())
            )
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            try:
                await client._call(client._sender, SendMessageRequest())
            except FloodWaitError:
                pass
        return logger

    logger = asyncio.run(scenario())
    starts = _trace(logger, "AWAIT START")
    assert len(starts) == 1, logger.text()
    request_id = _field(starts[0], "request_id")
    terminals = [
        line for event in ("RESPONSE", "TIMEOUT", "ERROR", "CANCELLED")
        for line in _trace(logger, event)
    ]
    assert len(terminals) == 1, logger.text()
    assert _field(terminals[0], "request_id") == request_id
    # The old code logged a terminal for GetDifference only; SendMessage got
    # nothing unless it was slower than 2s or a reconnect had just happened.
    assert "request=SendMessageRequest" in terminals[0]
    assert _field(terminals[0], "result") is not None


def test_getdifference_still_traced_and_policy_is_per_class():
    class Client(BaseClient):
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            return "ok"

    async def scenario():
        logger = Logger()
        client = Client()
        instrument_client(client, logger, policy=RpcPolicy())
        await client._call(client._sender, GetDifferenceRequest())
        return logger

    logger = asyncio.run(scenario())
    assert _trace(logger, "AWAIT START")
    assert _trace(logger, "RESPONSE")
    policy = RpcPolicy(send_timeout_s=15, moderation_timeout_s=20,
                       read_timeout_s=21, default_timeout_s=60)
    assert policy.timeout_for(SendMessageRequest()) == 15
    assert policy.timeout_for(GetDifferenceRequest()) == 20


# ---------------------------------------------------------------------------
# 2) Execution: FloodWait is raised by the library, backoff happens OUTSIDE
#    the gate/governor, and the event loop keeps running.
# ---------------------------------------------------------------------------
def test_flood_wait_requeue_releases_governor_and_does_not_block_loop():
    class Client(BaseClient):
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            self.calls.append(flood_sleep_threshold)
            if len(self.calls) == 1:
                raise FloodWaitError(1)
            return "ok"

    async def scenario():
        logger = Logger()
        client = Client()
        policy = RpcPolicy(
            flood_sleep_threshold_s=0,
            flood_max_retries=1,
            flood_backoff_base_s=0,
            flood_jitter_s=0,
            flood_backoff_cap_s=30,
            flood_wait_cap_s=30,
        )
        bot = SimpleNamespace(
            logger=logger, rpc_policy=policy, rpc_flood_controller=None,
            rpc_governor=None, outgoing_sender=None,
        )
        install_outgoing_sender(client, bot, logger)
        governor = bot.rpc_governor
        ticks = [0]

        async def ticker():
            while True:
                await asyncio.sleep(0.005)
                ticks[0] += 1

        tick_task = asyncio.create_task(ticker())
        task = asyncio.create_task(
            client._call(client._sender, SendMessageRequest())
        )
        await asyncio.sleep(0.03)   # first attempt raised; now in backoff
        active_during_backoff = governor.snapshot()["active"]
        result = await asyncio.wait_for(task, timeout=2)
        tick_task.cancel()
        await asyncio.gather(tick_task, return_exceptions=True)
        return result, active_during_backoff, ticks[0], client.calls, logger

    result, active, ticks, calls, logger = asyncio.run(scenario())
    assert result == "ok"
    assert len(calls) == 2, calls
    # The library must never be allowed to sleep 60s inside _call.
    assert calls == [0, 0], calls
    assert active == 0, "governor permit was held during backoff"
    assert ticks >= 3, f"event loop blocked during backoff (ticks={ticks})"
    assert "RPC FLOOD WAIT " in logger.text()
    assert "OUTGOING SEND FAILED" not in logger.text()
    assert active == 0


def test_long_flood_wait_is_not_slept_and_surfaces_immediately():
    class Client(BaseClient):
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            self.calls.append(1)
            raise FloodWaitError(45)

    async def scenario():
        logger = Logger()
        client = Client()
        policy = RpcPolicy(flood_max_retries=2, flood_wait_cap_s=30,
                           flood_jitter_s=0)
        install_rpc_resilience(client, logger, policy=policy)
        raised = False
        started = asyncio.get_running_loop().time()
        try:
            await client._call(client._sender, SendMessageRequest())
        except FloodWaitError:
            raised = True
        return raised, len(client.calls), logger, \
            asyncio.get_running_loop().time() - started

    raised, calls, logger, elapsed = asyncio.run(scenario())
    assert raised and calls == 1
    assert elapsed < 0.2, elapsed
    assert "RPC FLOOD WAIT CAPPED" in logger.text()


def test_explicit_per_request_timeout_replaces_global_60s():
    class Client(BaseClient):
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            await asyncio.Event().wait()

    async def scenario():
        logger = Logger()
        client = Client()
        policy = RpcPolicy(send_timeout_s=0.05, moderation_timeout_s=0.4,
                           read_timeout_s=0.4, default_timeout_s=1.0)
        instrument_client(client, logger, policy=policy)
        cg.install_rpc_timeout(
            client, on_timeout=None, logger=logger, policy=policy
        )
        started = asyncio.get_running_loop().time()
        try:
            await client._call(client._sender, SendMessageRequest())
        except cg.RpcTimeout as error:
            return error, asyncio.get_running_loop().time() - started, logger
        raise AssertionError("RpcTimeout was not raised")

    error, elapsed, logger = asyncio.run(scenario())
    assert "0.05s" in str(error), str(error)
    assert elapsed < 0.3, elapsed
    assert _trace(logger, "AWAIT START")
    assert _trace(logger, "TIMEOUT")


# ---------------------------------------------------------------------------
# 3) Architecture: notices have their own queue / gate / governor bucket
# ---------------------------------------------------------------------------
def test_notice_lane_uses_its_own_queue_gate_and_bucket():
    assert classify_request(SendMessageRequest(), channel="notice").bucket \
        == "notice"
    assert classify_request(SendMessageRequest()).bucket == "send"

    async def scenario():
        logger = Logger()
        sender = OutgoingSender(client=None, logger=logger)
        event = SimpleNamespace(chat_id=77)
        assert sender.enqueue_reply_notice(event, "auto notice")
        assert sender.enqueue_send(77, "main reply", priority=1)
        keys = list(sender._queues)
        await asyncio.gather(*(q.join() for q in sender._queues.values()),
                             return_exceptions=True)
        await sender.close()
        return keys

    keys = asyncio.run(scenario())
    assert ("77", "notice") in keys or any(k[1] == "notice" for k in keys), keys
    assert any(k[1] != "notice" for k in keys), keys


def test_stuck_notice_does_not_consume_the_main_send_slot():
    class Client(BaseClient):
        def __init__(self):
            super().__init__()
            self.release = asyncio.Event()
            self.release_main = asyncio.Event()
            self.notice_started = asyncio.Event()
            self.main_started = asyncio.Event()

        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            if _SEND_CHANNEL.get() == "notice":
                self.notice_started.set()
                await self.release.wait()
                return "notice"
            self.main_started.set()
            await self.release_main.wait()
            return "main"

    async def scenario():
        logger = Logger()
        client = Client()
        governor = RpcGovernor(total_limit=2, noncritical_limit=2,
                               send_limit=1, notice_limit=1)
        assert _wrap_call_with_gate(client, logger, governor)
        notice_token = _SEND_CHANNEL.set("notice")
        notice = asyncio.create_task(
            client._call(client._sender, SendMessageRequest())
        )
        _SEND_CHANNEL.reset(notice_token)
        await client.notice_started.wait()
        main = asyncio.create_task(
            client._call(client._sender, SendMessageRequest())
        )
        await asyncio.wait_for(client.main_started.wait(), timeout=0.2)
        await asyncio.sleep(0)
        snapshot = governor.snapshot()
        client.release.set()
        client.release_main.set()
        await asyncio.gather(notice, main)
        return snapshot, governor.snapshot()

    during, after = asyncio.run(scenario())
    assert during["active_by_bucket"].get("notice") == 1
    assert during["active_by_bucket"].get("send") == 1
    assert after["active"] == 0


# ---------------------------------------------------------------------------
# 4) Watchdog: an orphan await is logged and its inner RPC task cancelled
# ---------------------------------------------------------------------------
def test_orphan_watchdog_logs_and_cancels_inner_task():
    class Client(BaseClient):
        async def _call(self, sender, request, ordered=False,
                        flood_sleep_threshold=None):
            await asyncio.Event().wait()

    async def scenario():
        logger = Logger()
        client = Client()
        policy = RpcPolicy(
            send_timeout_s=0.1,
            watchdog_interval_s=0.02,
            watchdog_grace_s=0.02,
            watchdog_cancel=True,
        )
        instrument_client(client, logger, policy=policy)
        # Deliberately longer than the policy deadline: only the watchdog can
        # unblock this await (simulates a swallowed CancelledError).
        cg.install_rpc_timeout(
            client, timeout=2.0, logger=logger, policy=policy
        )
        install_rpc_resilience(client, logger, policy=policy)
        start_orphan_watchdog(logger, policy)
        task = asyncio.create_task(
            client._call(client._sender, SendMessageRequest())
        )
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.03)
        stop_orphan_watchdog()
        return logger

    logger = asyncio.run(scenario())
    assert _trace(logger, "ORPHAN"), logger.text()
    assert _trace(logger, "ORPHAN CANCEL"), logger.text()
    starts = _trace(logger, "AWAIT START")
    terminals = [line for event in ("RESPONSE", "TIMEOUT", "ERROR", "CANCELLED")
                 for line in _trace(logger, event)]
    assert len(starts) == len(terminals) == 1, logger.text()
    assert "ORPHAN RPC AWAIT" in logger.text()


def test_auto_notice_persist_runs_off_the_event_loop(tmp_path):
    import threading
    from modules.notice_cleanup import NoticeCleanup

    async def scenario():
        cleanup = NoticeCleanup(str(tmp_path / "notices.json"))
        cleanup._started = True
        main_thread = threading.get_ident()
        seen = []

        def slow_writer(path, payload):
            seen.append(threading.get_ident())
            import time as _time
            _time.sleep(0.05)
            cleanup._write_persist_payload_original(path, payload)

        cleanup._write_persist_payload_original = cleanup._write_persist_payload
        cleanup._write_persist_payload = slow_writer
        ticks = [0]

        async def ticker():
            while True:
                await asyncio.sleep(0.005)
                ticks[0] += 1

        tick_task = asyncio.create_task(ticker())
        cleanup.schedule(77, 123)
        await cleanup._persist_async()
        tick_task.cancel()
        await asyncio.gather(tick_task, return_exceptions=True)
        return main_thread, seen[0], ticks[0], (tmp_path / "notices.json").exists()

    main_thread, writer_thread, ticks, written = asyncio.run(scenario())
    assert writer_thread != main_thread
    assert ticks >= 3, f"event loop blocked by notice persist (ticks={ticks})"
    assert written
