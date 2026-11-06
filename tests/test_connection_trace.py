"""Connection/GetDifference traces. Does not change governor or limits."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from modules import connection_guard as cg
from modules import outgoing_profiler as op
from modules.outgoing_profiler import (
    instrument_client,
    mark_rpc_on_wire,
    pending_rpc_snapshot,
)


class Logger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))


class GetDifferenceRequest:
    pass


class GetChannelDifferenceRequest:
    def __init__(self):
        self.channel = SimpleNamespace(channel_id=22824228)


class SendMessageRequest:
    def __init__(self, peer=1):
        self.peer = peer


class RequestState:
    def __init__(self, request, future, msg_id):
        self.request = request
        self.future = future
        self.msg_id = msg_id


class Sender:
    def __init__(self):
        self._pending_state = {}
        self._n = 0
        self._ping = None
        self._user_connected = True
        self._reconnecting = False
        self._connection = SimpleNamespace(
            disconnect=self._disconnect,
            _writer=None,
        )
        self.reconnects = []

    async def _disconnect(self):
        return None

    def put(self, request):
        self._n += 1
        future = asyncio.get_running_loop().create_future()
        state = RequestState(request, future, self._n)
        self._pending_state[self._n] = state
        cg.note_pending(self)
        return state

    def _keepalive_ping(self, rnd_id):
        if self._ping is None:
            self._ping = rnd_id
        else:
            self._start_reconnect("pong-timeout")
        return rnd_id

    def _handle_pong(self, message):
        self._ping = None

    def _start_reconnect(self, error):
        if self._user_connected and not self._reconnecting:
            self._reconnecting = True
            self.reconnects.append(error)

    async def _reconnect(self, last_error):
        await asyncio.sleep(0.02)
        self._user_connected = True
        self._reconnecting = False
        return "reconnected"


class Client:
    def __init__(self, rpc_ms=40, hang_until=None):
        self._sender = Sender()
        self.rpc_ms = rpc_ms
        self.hang_until = hang_until
        self.calls = []

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        self.calls.append(type(request).__name__)
        state = sender.put(request)
        mark_rpc_on_wire()
        if self.hang_until is not None:
            await self.hang_until.wait()
        else:
            await asyncio.sleep(self.rpc_ms / 1000.0)
        if not state.future.done():
            state.future.set_result("ok")
        sender._pending_state.pop(state.msg_id, None)
        return "ok"


def _trace(logger, event):
    return [line for line in logger.infos if line.startswith(f"CONN TRACE {event}")]


def test_getdifference_logs_await_send_response():
    async def scenario():
        logger = Logger()
        client = Client(rpc_ms=30)
        instrument_client(client, logger)
        result = await client._call(client._sender, GetDifferenceRequest())
        return result, logger

    result, logger = asyncio.run(scenario())
    assert result == "ok"
    assert _trace(logger, "AWAIT START")
    assert "request=GetDifferenceRequest" in _trace(logger, "AWAIT START")[0]
    assert _trace(logger, "SOCKET SEND")
    assert _trace(logger, "RESPONSE")
    response = _trace(logger, "RESPONSE")[0]
    assert "rpc_await_ms=" in response
    assert "reconnects=0" in response
    assert "socket_sent=1" in response


def test_reconnect_logs_inflight_getdifference_and_replay():
    async def scenario():
        op._LAST_RPC_OK_AT = None
        op._LAST_RPC_ACTIVITY_AT = None
        previous = op.PONG_RECONNECT_STUCK_SECONDS
        op.PONG_RECONNECT_STUCK_SECONDS = 0.001
        try:
            logger = Logger()
            client = Client()
            client.hang_until = asyncio.Event()
            instrument_client(client, logger)
            sender = client._sender
            task = asyncio.create_task(
                client._call(sender, GetChannelDifferenceRequest())
            )
            await asyncio.sleep(0.01)
            sender._keepalive_ping(11)
            sender._keepalive_ping(22)
            await sender._reconnect("net")
            client.hang_until.set()
            await task
            return logger, sender
        finally:
            op.PONG_RECONNECT_STUCK_SECONDS = previous

    logger, sender = asyncio.run(scenario())
    texts = "\n".join(logger.infos)
    assert "RECONNECT START" in texts
    assert "CONN TRACE RECONNECT START" in texts
    assert "GetChannelDifferenceRequest" in texts
    assert "CONN TRACE RECONNECT SUCCESS" in texts
    assert "replayed_count=" in texts
    assert "CONN TRACE SNAPSHOT" in texts
    assert "pending_tasks=" in texts
    assert "sender_pending=" in texts
    assert sender.reconnects == ["pong-timeout"]


def test_pong_timeout_skips_reconnect_for_young_rpc():
    async def scenario():
        op._LAST_RPC_OK_AT = None
        op._LAST_RPC_ACTIVITY_AT = None
        logger = Logger()
        client = Client()
        client.hang_until = asyncio.Event()
        instrument_client(client, logger)
        sender = client._sender
        task = asyncio.create_task(
            client._call(sender, GetChannelDifferenceRequest())
        )
        await asyncio.sleep(0.01)
        sender._keepalive_ping(11)
        sender._keepalive_ping(22)
        client.hang_until.set()
        await task
        return logger, sender

    logger, sender = asyncio.run(scenario())
    texts = "\n".join(logger.infos)
    assert "KEEPALIVE PONG TIMEOUT" in texts
    assert "KEEPALIVE PONG TIMEOUT IGNORED" in texts
    assert "RECONNECT START" not in texts
    assert sender.reconnects == []


def test_ws_close_logs_inflight():
    async def scenario():
        logger = Logger()
        client = Client()
        instrument_client(client, logger)
        await client._sender._connection.disconnect()
        return logger

    logger = asyncio.run(scenario())
    assert _trace(logger, "WS CLOSE")
    assert "source=connection.disconnect" in _trace(logger, "WS CLOSE")[0]


def test_timeout_emits_conn_trace():
    async def scenario():
        logger = Logger()
        client = Client()
        client.hang_until = asyncio.Event()
        instrument_client(client, logger)
        cg.install_rpc_timeout(client, timeout=0.03, logger=logger)
        timed = False
        try:
            await client._call(client._sender, GetDifferenceRequest())
        except cg.RpcTimeout:
            timed = True
        return timed, logger

    timed, logger = asyncio.run(scenario())
    assert timed
    assert any(line.startswith("CONN TRACE TIMEOUT") for line in logger.infos)
    assert any("GetDifferenceRequest" in line for line in logger.infos + logger.errors)


def test_unhooked_socket_is_reported_as_unmeasured():
    class BlindClient(Client):
        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            await asyncio.sleep(self.rpc_ms / 1000.0)
            return "ok"

    async def scenario():
        logger = Logger()
        client = BlindClient(rpc_ms=5)
        instrument_client(client, logger)
        await client._call(client._sender, SendMessageRequest())
        return logger

    logger = asyncio.run(scenario())
    response = _trace(logger, "RESPONSE")[0]
    assert "socket_wait_ms=unmeasured" in response
    assert "socket_observed=0" in response


def test_send_loop_context_is_correlated_to_pending_rpc():
    """SPlusthon sends from a long-lived task, not the caller's ContextVar."""
    class WireConnection:
        async def send(self, _payload):
            await asyncio.sleep(0.015)

    class WireSender:
        def __init__(self):
            self._pending_state = {}
            self._queued = []
            self._connection = WireConnection()
            self._send_ready = asyncio.Event()
            self._next_id = 0

        def send(self, request):
            self._next_id += 1
            future = asyncio.get_running_loop().create_future()
            state = RequestState(request, future, self._next_id)
            self._queued.append(state)
            self._send_ready.set()
            return future

        async def send_loop(self):
            await self._send_ready.wait()
            state = self._queued.pop(0)
            # SPlusthon adds only the current packed batch to _pending_state
            # immediately before connection.send; queued requests aren't there yet.
            self._pending_state[state.msg_id] = state
            assert op._CALL_STATE.get() is None
            await self._connection.send(b"encrypted")
            # Same pending RequestState is written again after reconnect.
            op._RECONNECT_GEN += 1
            await self._connection.send(b"encrypted replay")
            if not state.future.done():
                state.future.set_result("ok")
            self._pending_state.pop(state.msg_id, None)

    class WireClient:
        def __init__(self):
            self._sender = WireSender()

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            return await sender.send(request)

    async def scenario():
        logger = Logger()
        client = WireClient()
        previous_generation = op._RECONNECT_GEN
        send_loop = asyncio.create_task(client._sender.send_loop())
        instrument_client(client, logger)
        try:
            result = await client._call(client._sender, SendMessageRequest())
            await send_loop
            return result, logger
        finally:
            op._RECONNECT_GEN = previous_generation

    result, logger = asyncio.run(scenario())
    assert result == "ok"
    response = _trace(logger, "RESPONSE")[0]
    assert "socket_observed=1" in response
    assert "socket_wait_ms=unmeasured" not in response
    assert "resend_count=1" in response
    assert "reconnects=1" in response
    assert _trace(logger, "SOCKET SEND")
    assert _trace(logger, "SOCKET RESEND")


def test_unobserved_socket_wait_is_not_reported_as_zero():
    class UnobservedClient:
        def __init__(self):
            self._sender = SimpleNamespace(_pending_state={}, _connection=SimpleNamespace())

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            await asyncio.sleep(0.002)
            return "ok"

    async def scenario():
        logger = Logger()
        client = UnobservedClient()
        instrument_client(client, logger)
        await client._call(client._sender, GetDifferenceRequest())
        return logger

    previous_threshold = op._RPC_SLOW_MS
    op._RPC_SLOW_MS = 0
    try:
        logger = asyncio.run(scenario())
    finally:
        op._RPC_SLOW_MS = previous_threshold
    response = _trace(logger, "RESPONSE")[0]
    assert "socket_observed=0" in response
    assert "socket_wait_ms=unmeasured" in response
    budget = [line for line in logger.infos if "OUTGOING RPC" in line]
    assert budget and all("sender_wait_ms=unmeasured" in line for line in budget)
