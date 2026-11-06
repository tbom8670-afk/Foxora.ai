"""Split connection_wait vs rpc_wait without changing send order.

    python tests/test_outgoing_profiler.py
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import outgoing_profiler as op
from modules.outgoing_profiler import (
    instrument_client,
    instrument_event,
    mark_rpc_on_wire,
    response_rpc_ms,
    begin_response_measurement,
    end_response_measurement,
)

PASSED = FAILED = 0


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")


class Logger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(message)

    def log_error(self, message):
        self.errors.append(message)


class SendMessageRequest:
    def __init__(self, peer):
        self.peer = peer


class DeleteMessagesRequest:
    def __init__(self, peer):
        self.peer = peer


class EditBannedRequest:
    def __init__(self, channel):
        self.channel = channel


class FakeSender:
    pass


class FakeClient:
    def __init__(self, connection_ms=50, rpc_ms=400):
        self._sender = FakeSender()
        self.connection_ms = connection_ms
        self.rpc_ms = rpc_ms
        self.calls = []

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        self.calls.append(type(request).__name__)
        if self.connection_ms:
            await asyncio.sleep(self.connection_ms / 1000.0)
        mark_rpc_on_wire()
        if self.rpc_ms:
            await asyncio.sleep(self.rpc_ms / 1000.0)
        return "ok"

    async def send_message(self, entity, text):
        return await self._call(self._sender, SendMessageRequest(entity))

    async def delete_messages(self, entity, ids):
        return await self._call(self._sender, DeleteMessagesRequest(entity))

    async def edit_permissions(self, entity, user, **kwargs):
        return await self._call(self._sender, EditBannedRequest(entity))

    async def kick_participant(self, entity, user):
        return await self._call(self._sender, EditBannedRequest(entity))


class FakeEvent:
    def __init__(self, client, chat_id):
        self.client = client
        self.chat_id = chat_id

    async def reply(self, text):
        return await self.client._call(self.client._sender, SendMessageRequest(self.chat_id))

    async def delete(self):
        return await self.client._call(self.client._sender, DeleteMessagesRequest(self.chat_id))


def parse_traces(infos):
    rows = []
    for line in infos:
        if not line.startswith("RPC TRACE "):
            continue
        row = {}
        for part in line.split()[2:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.endswith("_ms"):
                row[key] = float(value)
            else:
                row[key] = value
        rows.append(row)
    return rows


def test_send_message_split():
    print("\n### send_message: connection_wait جدا از rpc_wait")

    async def scenario():
        logger = Logger()
        client = FakeClient(connection_ms=50, rpc_ms=400)
        instrument_client(client, logger)
        token = begin_response_measurement()
        result = await client.send_message(-100, "hi")
        rpc_ms = response_rpc_ms()
        end_response_measurement(token)
        return result, logger, rpc_ms, client.calls

    result, logger, rpc_ms, calls = asyncio.run(scenario())
    traces = parse_traces(logger.infos)
    print("    logs:")
    for line in logger.infos:
        if line.startswith("RPC TRACE") or line.startswith("RPC TIME"):
            print("   ", line)
    check("مقدار برگشتی عوض نشده", result == "ok")
    check("یک _call واقعی انجام شده", calls == ["SendMessageRequest"], f"-> {calls}")
    inner = [row for row in traces if row.get("request") == "SendMessageRequest"]
    outer = [row for row in traces if row.get("operation") == "send_message" and "request" not in row]
    check("TRACE داخلی send_message هست", bool(inner))
    if inner:
        check("connection_wait حدود 50ms است",
              35 <= inner[0]["connection_wait_ms"] <= 80,
              f"-> {inner[0]['connection_wait_ms']:.1f}")
        check("rpc_wait حدود 400ms است",
              370 <= inner[0]["rpc_wait_ms"] <= 450,
              f"-> {inner[0]['rpc_wait_ms']:.1f}")
        check("total تقریباً جمع دو بخش است",
              inner[0]["total_rpc_ms"] >= inner[0]["connection_wait_ms"] + inner[0]["rpc_wait_ms"] - 5)
    check("TRACE سطح بالا هم send_message است", bool(outer))
    check("response_rpc_ms هنوز جمع می‌شود", rpc_ms >= 400, f"-> {rpc_ms:.1f}")


def test_reply_and_delete_and_moderation():
    print("\n### reply / delete / ban / mute نام عملیات را حفظ می‌کنند")

    async def scenario():
        logger = Logger()
        client = FakeClient(connection_ms=40, rpc_ms=120)
        instrument_client(client, logger)
        event = FakeEvent(client, -7)
        instrument_event(event, logger)
        await event.reply("pong")
        await client.delete_messages(-7, [1])
        await client.kick_participant(-7, 9)
        await client.edit_permissions(-7, 9, send_messages=False)
        return logger

    logger = asyncio.run(scenario())
    traces = parse_traces(logger.infos)
    print("    operations:", [row.get("operation") for row in traces])
    ops = {row.get("operation") for row in traces}
    check("reply دیده می‌شود", "reply" in ops)
    check("delete_message دیده می‌شود", "delete_message" in ops)
    check("ban دیده می‌شود", "ban" in ops)
    check("moderation/mute دیده می‌شود", "moderation" in ops)
    for row in traces:
        if "connection_wait_ms" not in row:
            continue
        check(
            f"{row.get('operation')} connection_wait جدا است",
            20 <= row["connection_wait_ms"] <= 80,
            f"-> {row['connection_wait_ms']:.1f}",
        )
        check(
            f"{row.get('operation')} rpc_wait جدا است",
            90 <= row["rpc_wait_ms"] <= 180,
            f"-> {row['rpc_wait_ms']:.1f}",
        )


def test_missing_wire_hook_counts_as_rpc_wait():
    print("\n### اگر لحظهٔ send دیده نشود، کل زمان rpc_wait است")

    class BlindClient(FakeClient):
        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            await asyncio.sleep(0.08)
            return "ok"

    async def scenario():
        logger = Logger()
        client = BlindClient()
        instrument_client(client, logger)
        await client.send_message(1, "x")
        return logger

    traces = parse_traces(asyncio.run(scenario()).infos)
    inner = [row for row in traces if row.get("request") == "SendMessageRequest"]
    check("TRACE داخلی هست", bool(inner))
    if inner:
        check("socket time is explicitly unmeasured when no wire hook fires",
              inner[0].get("connection_wait_ms") is None
              and inner[0].get("socket_observed") == "0",
              f"-> {inner[0]}")
        check("rpc_wait کل await است",
              inner[0]["rpc_wait_ms"] >= 70,
              f"-> {inner[0]['rpc_wait_ms']:.1f}")


def test_no_behavior_change_and_idempotent():
    print("\n### نصب دوباره و ترتیب فراخوانی عوض نمی‌شود")

    async def scenario():
        logger = Logger()
        client = FakeClient(connection_ms=0, rpc_ms=0)
        instrument_client(client, logger)
        first_send = client.send_message
        first_call = client._call
        instrument_client(client, logger)
        return first_send is client.send_message and first_call is client._call

    check("instrument_client دوباره wrap نمی‌کند", asyncio.run(scenario()))


def test_send_path_hook_stamps_drain():
    print("\n### hook روی drain لحظهٔ سیم را ثبت می‌کند")

    class Writer:
        def __init__(self):
            self._pending = bytearray(b"x")

        async def drain(self):
            mark_check.append("drain")
            self._pending.clear()

    class Conn:
        def __init__(self):
            self._writer = Writer()

        async def send(self, data):
            mark_check.append("send")

    class Client(FakeClient):
        def __init__(self):
            super().__init__(connection_ms=0, rpc_ms=30)
            self._sender = type("S", (), {})()
            self._sender._connection = Conn()

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            await asyncio.sleep(0.04)
            await sender._connection._writer.drain()
            await asyncio.sleep(0.08)
            return "ok"

    mark_check = []

    async def scenario():
        logger = Logger()
        client = Client()
        instrument_client(client, logger)
        await client.send_message(3, "z")
        return logger

    traces = parse_traces(asyncio.run(scenario()).infos)
    inner = [row for row in traces if row.get("request") == "SendMessageRequest"]
    check("drain صدا شده", "drain" in mark_check)
    if inner:
        check("connection_wait از انتظار تا drain است",
              25 <= inner[0]["connection_wait_ms"] <= 70,
              f"-> {inner[0]['connection_wait_ms']:.1f}")
        check("rpc_wait بعد از drain است",
              60 <= inner[0]["rpc_wait_ms"] <= 120,
              f"-> {inner[0]['rpc_wait_ms']:.1f}")


class LiveSender:
    PONG = object()

    def __init__(self):
        self._ping = None
        self._user_connected = True
        self._reconnecting = False
        self._pending_state = {}
        self.pings = []
        self.reconnects = []
        self._handlers = {self.PONG: self._handle_pong}

    def _keepalive_ping(self, rnd_id):
        if self._ping is None:
            self._ping = rnd_id
            self.pings.append(("sent", rnd_id))
        else:
            self.pings.append(("timeout", self._ping, rnd_id))
            self._start_reconnect(None)

    async def _handle_pong(self, message):
        pong = getattr(message, "obj", message)
        if self._ping == getattr(pong, "ping_id", None):
            self._ping = None
        return "pong-ok"

    def _start_reconnect(self, error):
        if self._user_connected and not self._reconnecting:
            self._reconnecting = True
            self.reconnects.append(error)

    async def _reconnect(self, last_error):
        await asyncio.sleep(0.01)
        self._user_connected = True
        self._reconnecting = False
        return "reconnected"


class LiveClient(FakeClient):
    def __init__(self):
        super().__init__(connection_ms=0, rpc_ms=80)
        self._sender = LiveSender()

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        await asyncio.sleep(self.rpc_ms / 1000.0)
        return "ok"


def _reset_pong_gate():
    op._LAST_RPC_OK_AT = None
    op._LAST_RPC_ACTIVITY_AT = None
    op._INFLIGHT_RPCS.clear()


def test_keepalive_and_reconnect_logs():
    print("\n### ping / pong / timeout لاگ می‌شوند؛ RPC تازه reconnect نمی‌کند")

    async def scenario():
        _reset_pong_gate()
        logger = Logger()
        client = LiveClient()
        client.rpc_ms = 80
        instrument_client(client, logger)
        sender = client._sender
        task = asyncio.create_task(client.send_message(-5, "slow"))
        await asyncio.sleep(0.02)
        sender._keepalive_ping(11)
        await sender._handlers[LiveSender.PONG](type("Msg", (), {
            "obj": type("Pong", (), {"ping_id": 11, "msg_id": 99})()
        })())
        sender._keepalive_ping(22)
        sender._keepalive_ping(33)
        await task
        return logger, sender

    logger, sender = asyncio.run(scenario())
    print("    logs:")
    for line in logger.infos:
        if line.startswith((
            "KEEPALIVE", "RECONNECT", "WEBSOCKET",
        )):
            print("   ", line)
    texts = "\n".join(logger.infos)
    check("پینگ ارسال شد", "KEEPALIVE PING SENT" in texts)
    check("پونگ دریافت شد", "KEEPALIVE PONG RECEIVED" in texts)
    check("timeout پونگ ثبت شد", "KEEPALIVE PONG TIMEOUT" in texts)
    check("timeout جوان نادیده گرفته شد", "KEEPALIVE PONG TIMEOUT IGNORED" in texts)
    check("reconnect بی‌دلیل شروع نشد", "RECONNECT START" not in texts)
    check("request_id RPC در حال انتظار در timeout هست",
          "request_ids=" in texts and "pending_rpc=" in texts)
    check("پینگ بعدی به‌جای قطع اتصال ارسال شد",
          sender.pings[0][0] == "sent" and sender.pings[-1][0] == "sent",
          f"-> {sender.pings}")
    check("reconnect اصلی صدا نشد",
          sender.reconnects == [], f"-> {sender.reconnects}")
    check("kept=0 روی keepalive نیست", "kept=0" not in texts)


def test_pong_timeout_reconnects_only_when_rpc_stuck():
    print("\n### pong timeout فقط با RPC گیرکرده reconnect می‌کند")

    async def scenario():
        _reset_pong_gate()
        previous = op.PONG_RECONNECT_STUCK_SECONDS
        op.PONG_RECONNECT_STUCK_SECONDS = 0.01
        logger = Logger()
        client = LiveClient()
        client.rpc_ms = 80
        hang = asyncio.Event()

        async def _call(sender, request, ordered=False, flood_sleep_threshold=None):
            await hang.wait()
            return "ok"

        client._call = _call
        instrument_client(client, logger)
        sender = client._sender
        task = asyncio.create_task(client.send_message(-5, "stuck"))
        await asyncio.sleep(0.03)
        sender._keepalive_ping(11)
        sender._keepalive_ping(22)
        await sender._reconnect("net")
        hang.set()
        await task
        op.PONG_RECONNECT_STUCK_SECONDS = previous
        return logger, sender

    logger, sender = asyncio.run(scenario())
    texts = "\n".join(logger.infos)
    check("timeout پونگ ثبت شد", "KEEPALIVE PONG TIMEOUT" in texts)
    check("شروع reconnect ثبت شد", "RECONNECT START" in texts)
    check("موفقیت reconnect ثبت شد", "RECONNECT SUCCESS" in texts)
    check("reconnect به‌خاطر RPC گیرکرده است", "reason=pending_rpc_stuck" in texts)
    check("reconnect اصلی هنوز صدا می‌شود",
          sender.reconnects == [None], f"-> {sender.reconnects}")


def main():
    test_send_message_split()
    test_reply_and_delete_and_moderation()
    test_missing_wire_hook_counts_as_rpc_wait()
    test_no_behavior_change_and_idempotent()
    test_send_path_hook_stamps_drain()
    test_keepalive_and_reconnect_logs()
    test_pong_timeout_reconnects_only_when_rpc_stuck()
    print(f"\npassed={PASSED} failed={FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
