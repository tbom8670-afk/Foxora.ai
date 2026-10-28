"""Focused no-network regressions for fair global SPlusthon RPC admission."""
import asyncio
from types import SimpleNamespace

from modules.outgoing_sender import (
    _CHAT_GATES,
    _wrap_call_with_gate,
    install as install_outgoing_sender,
)
from modules.outgoing_profiler import _log_rpc_budget
from modules.rpc_governor import (
    P0_CRITICAL,
    P1_DELETE,
    P2_SEND,
    P3_HEAVY,
    RpcAdmission,
    RpcAdmissionTimeoutError,
    RpcGovernor,
    classify_request,
    is_keepalive_request,
)


def admission(priority, bucket, chat):
    return RpcAdmission(priority, bucket, bucket, str(chat))


def test_request_classification_matches_soroush_workloads():
    class EditBannedRequest:
        pass

    class DeleteMessagesRequest:
        pass

    class SendMessageRequest:
        pass

    class GetParticipantsRequest:
        pass

    assert classify_request(EditBannedRequest()).priority == P0_CRITICAL
    assert classify_request(DeleteMessagesRequest()).priority == P1_DELETE
    assert classify_request(SendMessageRequest()).priority == P2_SEND
    assert classify_request(GetParticipantsRequest()).priority == P3_HEAVY
    assert classify_request(SendMessageRequest(), urgent_send=True).priority == P2_SEND
    assert classify_request(GetParticipantsRequest(), critical_context=True).priority == P3_HEAVY
    assert classify_request(DeleteMessagesRequest(), critical_context=True).priority == P1_DELETE


def test_noncritical_cannot_consume_three_reserved_critical_slots():
    async def scenario():
        governor = RpcGovernor(
            total_limit=8, noncritical_limit=5,
            delete_limit=4, send_limit=3, heavy_limit=1,
        )
        permits = []
        for index in range(3):
            permits.append(await governor.acquire(admission(P2_SEND, "send", index)))
        for index in range(2):
            permits.append(await governor.acquire(admission(P1_DELETE, "delete", index)))

        blocked = asyncio.create_task(
            governor.acquire(admission(P1_DELETE, "delete", "blocked"))
        )
        await asyncio.sleep(0)
        assert not blocked.done()
        assert governor.snapshot()["active_noncritical"] == 5

        critical = await asyncio.wait_for(
            governor.acquire(admission(P0_CRITICAL, "critical", "admin")),
            timeout=0.05,
        )
        assert governor.snapshot()["active"] == 6
        critical.release()
        permits[0].release()
        extra = await asyncio.wait_for(blocked, timeout=0.05)
        extra.release()
        for permit in permits[1:]:
            permit.release()
        assert governor.snapshot()["active"] == 0

    asyncio.run(scenario())


def test_class_caps_limit_deletes_sends_and_heavy_reads_independently():
    async def scenario():
        governor = RpcGovernor(total_limit=8, noncritical_limit=8)
        deletes = [
            await governor.acquire(admission(P1_DELETE, "delete", index))
            for index in range(4)
        ]
        fifth_delete = asyncio.create_task(
            governor.acquire(admission(P1_DELETE, "delete", "fifth"))
        )
        sends = [
            await governor.acquire(admission(P2_SEND, "send", index))
            for index in range(3)
        ]
        fourth_send = asyncio.create_task(
            governor.acquire(admission(P2_SEND, "send", "fourth"))
        )
        heavy = await governor.acquire(admission(P3_HEAVY, "heavy", "one"))
        second_heavy = asyncio.create_task(
            governor.acquire(admission(P3_HEAVY, "heavy", "two"))
        )
        await asyncio.sleep(0)
        assert not fifth_delete.done()
        assert not fourth_send.done()
        assert not second_heavy.done()

        deletes[0].release()
        replacement_delete = await asyncio.wait_for(fifth_delete, timeout=0.05)
        sends[0].release()
        replacement_send = await asyncio.wait_for(fourth_send, timeout=0.05)
        heavy.release()
        replacement_heavy = await asyncio.wait_for(second_heavy, timeout=0.05)

        for permit in deletes[1:] + sends[1:]:
            permit.release()
        replacement_delete.release()
        replacement_send.release()
        replacement_heavy.release()
        assert governor.snapshot()["active"] == 0

    asyncio.run(scenario())


def test_round_robin_prevents_one_chat_from_monopolizing_a_class():
    async def scenario():
        governor = RpcGovernor(
            total_limit=1, noncritical_limit=1, send_limit=1
        )
        holder = await governor.acquire(admission(P2_SEND, "send", "holder"))
        order = []

        async def run(label, chat):
            permit = await governor.acquire(admission(P2_SEND, "send", chat))
            order.append(label)
            permit.release()

        tasks = [
            asyncio.create_task(run("A1", "A")),
            asyncio.create_task(run("A2", "A")),
            asyncio.create_task(run("B1", "B")),
        ]
        await asyncio.sleep(0)
        holder.release()
        await asyncio.gather(*tasks)
        return order, governor.snapshot()

    order, snapshot = asyncio.run(scenario())
    assert order == ["A1", "B1", "A2"]
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0


def test_cancelled_waiter_never_leaks_a_permit():
    async def scenario():
        governor = RpcGovernor(total_limit=1, noncritical_limit=1)
        holder = await governor.acquire(admission(P2_SEND, "other", "holder"))
        waiter = asyncio.create_task(
            governor.acquire(admission(P2_SEND, "other", "cancel"))
        )
        await asyncio.sleep(0)
        waiter.cancel()
        result = await asyncio.gather(waiter, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        holder.release()
        permit = await asyncio.wait_for(
            governor.acquire(admission(P2_SEND, "other", "after")),
            timeout=0.05,
        )
        permit.release()
        return governor.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0
    assert snapshot["stats"]["cancelled_waiters"] == 1


def test_shadow_mode_observes_without_blocking():
    async def scenario():
        governor = RpcGovernor(
            total_limit=1, noncritical_limit=1, shadow=True
        )
        first = await governor.acquire(admission(P2_SEND, "other", "A"))
        second = await asyncio.wait_for(
            governor.acquire(admission(P2_SEND, "other", "B")),
            timeout=0.05,
        )
        snapshot = governor.snapshot()
        second.release()
        first.release()
        return snapshot, governor.snapshot()

    during, after = asyncio.run(scenario())
    assert during["active"] == 2
    assert during["stats"]["shadow_would_wait"] == 1
    assert after["active"] == 0


def test_per_chat_low_gate_is_fifo_bounded_and_removed_when_idle():
    class SendMessageRequest:
        def __init__(self, chat_id):
            self.peer = SimpleNamespace(channel_id=chat_id)

    class Client:
        def __init__(self):
            self._sender = object()
            self.release = asyncio.Event()
            self.active = 0
            self.maximum = 0
            self.started = 0

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            self.active += 1
            self.started += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    async def scenario():
        _CHAT_GATES.clear()
        client = Client()
        assert _wrap_call_with_gate(client, None, None)
        tasks = [
            asyncio.create_task(
                client._call(client._sender, SendMessageRequest(99))
            )
            for _ in range(5)
        ]
        await asyncio.sleep(0)
        assert client.started == 2
        client.release.set()
        await asyncio.gather(*tasks)
        return client.maximum

    maximum = asyncio.run(scenario())
    assert maximum == 2
    assert _CHAT_GATES == {}


def test_rebuilt_client_reuses_the_same_bot_level_governor():
    class Client:
        def __init__(self):
            self._sender = object()

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            return request

        async def send_message(self, entity, text):
            return text

    bot = SimpleNamespace(logger=None)
    first = Client()
    first_sender = install_outgoing_sender(first, bot, None)
    governor = bot.rpc_governor
    second = Client()
    second_sender = install_outgoing_sender(second, bot, None)

    assert first_sender is not second_sender
    assert first._call._rpc_governor is governor
    assert second._call._rpc_governor is governor


def test_wrapped_exception_and_cancellation_release_all_slots():
    class SendMessageRequest:
        def __init__(self, chat_id):
            self.peer = SimpleNamespace(channel_id=chat_id)

    class Client:
        def __init__(self):
            self._sender = object()
            self.block = asyncio.Event()
            self.fail = True

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            if self.fail:
                raise RuntimeError("flood/retry boundary")
            await self.block.wait()

    async def scenario():
        _CHAT_GATES.clear()
        governor = RpcGovernor(total_limit=2, noncritical_limit=2)
        client = Client()
        assert _wrap_call_with_gate(client, None, governor)
        try:
            await client._call(client._sender, SendMessageRequest(1))
        except RuntimeError:
            pass
        else:
            raise AssertionError("inner error must propagate")
        assert governor.snapshot()["active"] == 0

        client.fail = False
        task = asyncio.create_task(
            client._call(client._sender, SendMessageRequest(2))
        )
        await asyncio.sleep(0)
        assert governor.snapshot()["active"] == 1
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        return governor.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0
    assert _CHAT_GATES == {}


def test_governor_wait_is_outside_existing_rpc_timeout_and_finally_releases():
    class SendMessageRequest:
        def __init__(self, chat_id):
            self.peer = SimpleNamespace(channel_id=chat_id)

    class Client:
        def __init__(self):
            self._sender = object()

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            # Models the already-installed inner network timeout. The RPC itself
            # fits; only the outer governor wait makes total wall time longer.
            return await asyncio.wait_for(asyncio.sleep(0.03, result="ok"), 0.05)

    async def scenario():
        _CHAT_GATES.clear()
        governor = RpcGovernor(total_limit=1, noncritical_limit=1)
        holder = await governor.acquire(admission(P2_SEND, "other", "holder"))
        client = Client()
        assert _wrap_call_with_gate(client, None, governor)
        task = asyncio.create_task(
            client._call(client._sender, SendMessageRequest(10))
        )
        await asyncio.sleep(0.07)
        assert not task.done()
        holder.release()
        result = await asyncio.wait_for(task, timeout=0.1)
        snapshot = governor.snapshot()
        return result, snapshot

    result, snapshot = asyncio.run(scenario())
    assert result == "ok"
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0
    assert _CHAT_GATES == {}


class _MemLogger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(str(message))

    def log_error(self, message):
        self.errors.append(str(message))


def test_ping_keepalive_occupies_critical_if_acquired_directly():
    class PingRequest:
        pass

    class SendMessageRequest:
        pass

    assert is_keepalive_request(PingRequest())
    assert not is_keepalive_request(SendMessageRequest())
    classified = classify_request(PingRequest())
    assert classified.priority == P0_CRITICAL
    assert classified.bucket == "critical"

    async def scenario():
        governor = RpcGovernor(total_limit=2, noncritical_limit=1)
        ping = await governor.acquire(classified)
        snap = governor.snapshot()
        ping.release()
        return snap, governor.snapshot()

    during, after = asyncio.run(scenario())
    assert during["active"] == 1
    assert during["active_by_bucket"].get("critical") == 1
    assert any("PingRequest" in item for item in during["holders"])
    assert after["active"] == 0
    assert after["holders"] == []


def test_governor_blocked_logs_active_limit_and_holders():
    import modules.rpc_governor as rg

    async def scenario():
        old = rg.GOVERNOR_BLOCKED_MS
        rg.GOVERNOR_BLOCKED_MS = 40.0
        logger = _MemLogger()
        try:
            governor = RpcGovernor(
                total_limit=1, noncritical_limit=1, logger=logger
            )
            holder = await governor.acquire(admission(P2_SEND, "send", "busy"))
            snap = governor.snapshot()
            waiter = asyncio.create_task(
                governor.acquire(admission(P1_DELETE, "delete", "blocked"))
            )
            await asyncio.sleep(0.06)
            assert not waiter.done()
            holder.release()
            permit = await asyncio.wait_for(waiter, timeout=0.2)
            permit.release()
            return snap, logger.errors, logger.infos, governor.snapshot()
        finally:
            rg.GOVERNOR_BLOCKED_MS = old

    snap, errors, infos, after = asyncio.run(scenario())
    blocked = [line for line in errors if line.startswith("GOVERNOR BLOCKED")]
    assert blocked, errors
    text = blocked[0]
    assert "bucket=delete" in text
    assert "request=delete" in text
    assert "wait_ms=" in text
    assert "active=" in text
    assert "limit=" in text
    assert "holders=[" in text
    assert "send:send" in text
    assert snap["holders"]
    assert any("GOVERNOR ACQUIRE" in line for line in infos)
    assert after["active"] == 0
    assert after["holders"] == []


def test_keepalive_ping_bypasses_governor_in_call_wrapper():
    class PingRequest:
        pass

    class SendMessageRequest:
        def __init__(self, chat_id):
            self.peer = SimpleNamespace(channel_id=chat_id)

    class Client:
        def __init__(self):
            self._sender = object()
            self.pings = 0

        async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
            if type(request).__name__ == "PingRequest":
                self.pings += 1
                return "pong"
            return "ok"

    async def scenario():
        _CHAT_GATES.clear()
        governor = RpcGovernor(total_limit=1, noncritical_limit=1)
        holder = await governor.acquire(admission(P2_SEND, "send", "busy"))
        client = Client()
        assert _wrap_call_with_gate(client, None, governor)
        ping = await asyncio.wait_for(
            client._call(client._sender, PingRequest()), timeout=0.05
        )
        snap_during = governor.snapshot()
        send_task = asyncio.create_task(
            client._call(client._sender, SendMessageRequest(1))
        )
        await asyncio.sleep(0)
        send_waiting = not send_task.done()
        holder.release()
        await asyncio.wait_for(send_task, timeout=0.1)
        return ping, snap_during, send_waiting, client.pings, governor.snapshot()

    ping, snap, waiting, pings, after = asyncio.run(scenario())
    assert ping == "pong"
    assert pings == 1
    assert snap["active"] == 1
    assert waiting
    assert after["active"] == 0
    assert after["holders"] == []


def test_send_and_delete_do_not_share_one_slot():
    async def scenario():
        governor = RpcGovernor(total_limit=2, delete_limit=1, send_limit=1)
        delete_permit = await governor.acquire(admission(P1_DELETE, "delete", "A"))
        send_permit = await asyncio.wait_for(
            governor.acquire(admission(P2_SEND, "send", "B")),
            timeout=0.05,
        )
        snap = governor.snapshot()
        extra_send = asyncio.create_task(
            governor.acquire(admission(P2_SEND, "send", "C"))
        )
        extra_delete = asyncio.create_task(
            governor.acquire(admission(P1_DELETE, "delete", "D"))
        )
        await asyncio.sleep(0)
        same_bucket_blocked = (not extra_send.done()) and (not extra_delete.done())
        delete_permit.release()
        extra_delete = await asyncio.wait_for(extra_delete, timeout=0.05)
        send_permit.release()
        extra_send = await asyncio.wait_for(extra_send, timeout=0.05)
        extra_delete.release()
        extra_send.release()
        return snap, same_bucket_blocked, governor.snapshot()

    snap, blocked, after = asyncio.run(scenario())
    assert snap["active"] == 2
    assert snap["active_by_bucket"].get("delete") == 1
    assert snap["active_by_bucket"].get("send") == 1
    assert blocked
    assert after["active"] == 0


def test_critical_still_beats_send_and_delete():
    async def scenario():
        governor = RpcGovernor(total_limit=2, delete_limit=1, send_limit=1)
        delete_permit = await governor.acquire(admission(P1_DELETE, "delete", "A"))
        send_permit = await governor.acquire(admission(P2_SEND, "send", "B"))
        extra_send = asyncio.create_task(
            governor.acquire(admission(P2_SEND, "send", "C"))
        )
        critical_task = asyncio.create_task(
            governor.acquire(admission(P0_CRITICAL, "critical", "admin"))
        )
        await asyncio.sleep(0)
        assert not extra_send.done()
        assert not critical_task.done()
        send_permit.release()
        critical = await asyncio.wait_for(critical_task, timeout=0.05)
        send_still_waiting = not extra_send.done()
        snap = governor.snapshot()
        delete_permit.release()
        extra = await asyncio.wait_for(extra_send, timeout=0.05)
        critical.release()
        extra.release()
        return snap, send_still_waiting, governor.snapshot()

    snap, send_waiting, after = asyncio.run(scenario())
    assert snap["active_by_bucket"].get("critical") == 1
    assert send_waiting
    assert after["active"] == 0


def test_send_delete_critical_fairness_no_starvation():
    async def scenario():
        governor = RpcGovernor(total_limit=2, delete_limit=1, send_limit=1, heavy_limit=1)
        holder_d = await governor.acquire(admission(P1_DELETE, "delete", "hold-d"))
        holder_s = await governor.acquire(admission(P2_SEND, "send", "hold-s"))
        order = []

        async def run(priority, bucket, label, chat):
            permit = await governor.acquire(admission(priority, bucket, chat))
            order.append(label)
            permit.release()

        tasks = [
            asyncio.create_task(run(P2_SEND, "send", "S1", "s1")),
            asyncio.create_task(run(P2_SEND, "send", "S2", "s2")),
            asyncio.create_task(run(P1_DELETE, "delete", "D1", "d1")),
            asyncio.create_task(run(P0_CRITICAL, "critical", "C1", "c1")),
            asyncio.create_task(run(P3_HEAVY, "heavy", "H1", "h1")),
        ]
        await asyncio.sleep(0)
        holder_s.release()
        holder_d.release()
        await asyncio.gather(*tasks)
        return order, governor.snapshot()

    order, snapshot = asyncio.run(scenario())
    assert order[0] == "C1"
    assert "S1" in order and "S2" in order
    assert "D1" in order
    assert "H1" in order
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0


def test_cross_bucket_governor_wait_is_not_seconds():
    async def scenario():
        governor = RpcGovernor(total_limit=2, delete_limit=1, send_limit=1)
        started = __import__("time").perf_counter()
        delete_permit = await governor.acquire(admission(P1_DELETE, "delete", "A"))
        send_permit = await asyncio.wait_for(
            governor.acquire(admission(P2_SEND, "send", "B")),
            timeout=0.05,
        )
        wait_ms = (__import__("time").perf_counter() - started) * 1000.0
        delete_permit.release()
        send_permit.release()
        return wait_ms

    wait_ms = asyncio.run(scenario())
    assert wait_ms < 80.0, wait_ms


def test_total_ms_matches_phase_sum_not_wall_clock():
    logger = _MemLogger()
    started = __import__("time").perf_counter() - 2.5
    phases = {
        "started": started,
        "queue_wait_ms": 10.0,
        "governor_wait_ms": 20.0,
        "sender_wait_ms": 5.0,
        "rpc_await_ms": 15.0,
        "operation": "send_message",
    }
    _log_rpc_budget(logger, None, "send_message", phases)
    assert abs(phases["total_ms"] - 50.0) < 0.01
    assert not any("OUTGOING RPC CRITICAL" in line for line in logger.errors)


def test_governor_hold_slow_splits_permit_from_ping_pending():
    import time as time_mod
    import modules.rpc_governor as rg

    class PingRequest:
        pass

    async def scenario():
        old = rg.GOVERNOR_BLOCKED_MS
        rg.GOVERNOR_BLOCKED_MS = 40.0
        logger = _MemLogger()
        loop = asyncio.get_running_loop()
        ping_future = loop.create_future()
        sender = SimpleNamespace(
            _pending_state={
                99: SimpleNamespace(future=ping_future, request=PingRequest()),
            },
            _guard_seen_at={99: time_mod.monotonic() - 2.0},
        )
        try:
            governor = RpcGovernor(
                total_limit=1, noncritical_limit=1, logger=logger
            )
            governor._observe_sender = sender
            holder = await governor.acquire(admission(P2_SEND, "send", "busy"))
            waiter = asyncio.create_task(
                governor.acquire(admission(P1_DELETE, "delete", "blocked"))
            )
            await asyncio.sleep(0.06)
            assert not waiter.done()
            snap = governor.snapshot()
            holder.release()
            permit = await asyncio.wait_for(waiter, timeout=0.2)
            permit.release()
            return snap, logger.errors, logger.infos, governor.snapshot()
        finally:
            if not ping_future.done():
                ping_future.cancel()
            rg.GOVERNOR_BLOCKED_MS = old

    snap, errors, infos, after = asyncio.run(scenario())
    holds = [line for line in errors if line.startswith("GOVERNOR HOLD SLOW")]
    blocked = [line for line in errors if line.startswith("GOVERNOR BLOCKED")]
    drains = [line for line in infos if line.startswith("GOVERNOR DRAIN")]
    releases = [line for line in infos if line.startswith("GOVERNOR RELEASE")]
    assert holds, errors
    hold = holds[0]
    assert "reason=rpc_in_flight" in hold
    assert "bucket=send" in hold
    assert "held_ms=" in hold
    assert "ping_age_ms=" in hold
    assert "pending_by_type=PingRequest:1" in hold
    ping_age = float(hold.split("ping_age_ms=")[1].split()[0])
    assert ping_age >= 1500.0, hold
    assert "waiting_by_bucket=delete:1" in hold
    assert blocked, errors
    assert "waiting_by_bucket=delete:1" in blocked[0]
    assert "pending_by_type=PingRequest:1" in blocked[0]
    assert "holders=[" in blocked[0]
    assert "send:send" in blocked[0]
    assert snap["waiting"] == 1
    assert any("PingRequest" not in item for item in snap["holders"])
    assert releases, infos
    assert "reason=rpc_in_flight" in releases[0]
    assert drains, infos
    assert "waiting=0" in drains[0]
    assert after["active"] == 0
    assert after["waiting"] == 0
    assert after["holders"] == []


def test_governor_drain_logs_after_multi_waiter_burst():
    import modules.rpc_governor as rg

    async def scenario():
        old = rg.GOVERNOR_BLOCKED_MS
        rg.GOVERNOR_BLOCKED_MS = 40.0
        logger = _MemLogger()
        try:
            governor = RpcGovernor(
                total_limit=1, noncritical_limit=1, logger=logger
            )
            holder = await governor.acquire(admission(P2_SEND, "send", "busy"))
            first = asyncio.create_task(
                governor.acquire(admission(P1_DELETE, "delete", "A"))
            )
            second = asyncio.create_task(
                governor.acquire(admission(P2_SEND, "send", "B"))
            )
            await asyncio.sleep(0)
            assert governor.snapshot()["waiting"] == 2
            holder.release()
            permit_a = await asyncio.wait_for(first, timeout=0.2)
            permit_a.release()
            permit_b = await asyncio.wait_for(second, timeout=0.2)
            permit_b.release()
            return logger.infos, governor.snapshot()
        finally:
            rg.GOVERNOR_BLOCKED_MS = old

    infos, after = asyncio.run(scenario())
    drains = [line for line in infos if line.startswith("GOVERNOR DRAIN")]
    assert drains, infos
    text = drains[-1]
    assert "waiting=0" in text
    assert "max_waiting=2" in text
    assert after["active"] == 0
    assert after["waiting"] == 0


def test_delete_is_admitted_before_waiting_heavy():
    """Prod 21:18: GetParticipants took the free slot ahead of DeleteMessages."""

    async def scenario():
        governor = RpcGovernor(
            total_limit=2, delete_limit=1, send_limit=1, heavy_limit=1
        )
        holder_a = await governor.acquire(admission(P0_CRITICAL, "critical", "hold-a"))
        holder_b = await governor.acquire(admission(P0_CRITICAL, "critical", "hold-b"))
        extra_delete = asyncio.create_task(
            governor.acquire(admission(P1_DELETE, "delete", "d1"))
        )
        extra_heavy = asyncio.create_task(
            governor.acquire(admission(P3_HEAVY, "heavy", "h1"))
        )
        await asyncio.sleep(0)
        holder_b.release()
        delete_permit = await asyncio.wait_for(extra_delete, timeout=0.05)
        heavy_still_waiting = not extra_heavy.done()
        snap = governor.snapshot()
        delete_permit.release()
        holder_a.release()
        extra_heavy = await asyncio.wait_for(extra_heavy, timeout=0.05)
        extra_heavy.release()
        return heavy_still_waiting, snap, governor.snapshot()

    heavy_waiting, snap, after = asyncio.run(scenario())
    assert heavy_waiting
    assert snap["active_by_bucket"].get("delete") == 1
    assert snap["active_by_bucket"].get("heavy") != 1
    assert after["active"] == 0
    assert after["waiting"] == 0


def test_heavy_not_admitted_while_delete_waiting_at_cap():
    """A free slot must not go to P3 while a P1 waiter exists, even at delete cap."""

    async def scenario():
        governor = RpcGovernor(
            total_limit=2, delete_limit=1, send_limit=1, heavy_limit=1
        )
        holder_d = await governor.acquire(admission(P1_DELETE, "delete", "hold-d"))
        holder_s = await governor.acquire(admission(P2_SEND, "send", "hold-s"))
        extra_delete = asyncio.create_task(
            governor.acquire(admission(P1_DELETE, "delete", "wait-d"))
        )
        extra_heavy = asyncio.create_task(
            governor.acquire(admission(P3_HEAVY, "heavy", "wait-h"))
        )
        await asyncio.sleep(0)
        holder_s.release()
        await asyncio.sleep(0)
        heavy_took_slot = extra_heavy.done()
        delete_still_waiting = not extra_delete.done()
        snap = governor.snapshot()
        holder_d.release()
        extra_delete = await asyncio.wait_for(extra_delete, timeout=0.05)
        extra_heavy = await asyncio.wait_for(extra_heavy, timeout=0.05)
        extra_delete.release()
        extra_heavy.release()
        return heavy_took_slot, delete_still_waiting, snap, governor.snapshot()

    heavy_took, delete_waiting, snap, after = asyncio.run(scenario())
    assert not heavy_took
    assert delete_waiting
    assert snap["active"] == 1
    assert snap["waiting_by_bucket"].get("delete") == 1
    assert snap["waiting_by_bucket"].get("heavy") == 1
    assert after["active"] == 0


def test_p1_and_send_are_not_blocked_by_heavy_read_when_global_slot_is_free():
    async def scenario():
        governor = RpcGovernor(
            total_limit=3,
            noncritical_limit=1,
            delete_limit=1,
            send_limit=1,
            heavy_limit=1,
        )
        heavy = await governor.acquire(admission(P3_HEAVY, "heavy", "read"))
        delete = await asyncio.wait_for(
            governor.acquire(admission(P1_DELETE, "delete", "antispam")),
            timeout=0.05,
        )
        send = await asyncio.wait_for(
            governor.acquire(admission(P2_SEND, "send", "reply")),
            timeout=0.05,
        )
        snap = governor.snapshot()
        for permit in (send, delete, heavy):
            permit.release()
        return snap, governor.snapshot()

    during, after = asyncio.run(scenario())
    assert during["active_by_bucket"].get("heavy") == 1
    assert during["active_by_bucket"].get("delete") == 1
    assert during["active_by_bucket"].get("send") == 1
    assert after["active"] == 0


def test_resolve_username_has_its_own_bucket_and_does_not_starve_important_lanes():
    class ResolveUsernameRequest:
        pass

    resolved = classify_request(ResolveUsernameRequest())
    assert resolved.priority == P3_HEAVY
    assert resolved.bucket == "resolve"

    async def scenario():
        governor = RpcGovernor(
            total_limit=3,
            # Even a minimal background budget must not make a held username
            # resolution block the distinct send/delete lanes.
            noncritical_limit=1,
            delete_limit=1,
            send_limit=1,
            heavy_limit=1,
            resolve_limit=1,
            critical_reserve=1,
            resolve_timeout_seconds=0,
            watchdog_timeout_seconds=0,
        )
        resolve = await governor.acquire(resolved)
        send = await governor.acquire(
            admission(P2_SEND, "send", "user-facing")
        )
        delete = await governor.acquire(
            admission(P1_DELETE, "delete", "cleanup")
        )
        critical = await governor.acquire(
            admission(P0_CRITICAL, "critical", "ban")
        )
        snapshot = governor.snapshot()
        assert snapshot["active_by_bucket"]["resolve"] == 1
        assert snapshot["active_by_bucket"]["send"] == 1
        assert snapshot["active_by_bucket"]["delete"] == 1
        assert snapshot["active_by_bucket"]["critical"] == 1
        for permit in (critical, delete, send, resolve):
            permit.release()
        return governor.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0


def test_full_resolve_bucket_does_not_head_block_heavy_in_same_priority_chat():
    async def scenario():
        governor = RpcGovernor(
            total_limit=3,
            noncritical_limit=3,
            resolve_limit=1,
            heavy_limit=1,
            resolve_timeout_seconds=0,
            watchdog_timeout_seconds=0,
        )
        held_resolve = await governor.acquire(
            admission(P3_HEAVY, "resolve", "global")
        )
        blocked_resolve = asyncio.create_task(
            governor.acquire(admission(P3_HEAVY, "resolve", "global"))
        )
        await asyncio.sleep(0)
        assert governor.snapshot()["waiting_by_bucket"]["resolve"] == 1

        # This is the same priority and chat-key but a different bucket. It
        # must pass the blocked resolve waiter instead of sitting behind it.
        heavy = await asyncio.wait_for(
            governor.acquire(admission(P3_HEAVY, "heavy", "global")),
            timeout=0.05,
        )
        assert heavy.bucket == "heavy"
        heavy.release()
        blocked_resolve.cancel()
        result = await asyncio.gather(blocked_resolve, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        held_resolve.release()
        return governor.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0


def test_resolve_waiter_expires_and_is_removed_from_governor_queue():
    async def scenario():
        governor = RpcGovernor(
            total_limit=1,
            noncritical_limit=1,
            send_limit=1,
            resolve_timeout_seconds=0.02,
            watchdog_timeout_seconds=0,
        )
        held_send = await governor.acquire(
            admission(P2_SEND, "send", "held")
        )
        try:
            await governor.acquire(
                admission(P3_HEAVY, "resolve", "global")
            )
        except RpcAdmissionTimeoutError:
            pass
        else:
            raise AssertionError("ResolveUsername should have expired in queue")
        snapshot = governor.snapshot()
        assert snapshot["waiting"] == 0
        assert snapshot["stats"]["expired_waiters"] == 1
        held_send.release()
        return governor.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0


def test_governor_watchdog_cancels_stuck_owner_and_force_releases_permit():
    async def scenario():
        governor = RpcGovernor(
            total_limit=1,
            noncritical_limit=1,
            resolve_timeout_seconds=0.02,
            watchdog_timeout_seconds=0,
            watchdog_grace_seconds=0.01,
        )
        let_stubborn_finish = asyncio.Event()

        async def stuck_resolve():
            permit = await governor.acquire(
                admission(P3_HEAVY, "resolve", "global")
            )
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Model a library call which does not finish promptly when
                    # cancelled. The watchdog must still free its accounting.
                    await let_stubborn_finish.wait()
            finally:
                permit.release()

        task = asyncio.create_task(stuck_resolve())
        await asyncio.sleep(0.07)
        after_watchdog = governor.snapshot()
        assert after_watchdog["active"] == 0
        assert after_watchdog["stats"]["watchdog_cancelled"] == 1
        assert after_watchdog["stats"]["watchdog_forced_releases"] == 1

        send = await asyncio.wait_for(
            governor.acquire(admission(P2_SEND, "send", "other-chat")),
            timeout=0.05,
        )
        let_stubborn_finish.set()
        await asyncio.wait_for(task, timeout=0.05)
        send.release()
        return governor.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["active"] == 0
    assert snapshot["waiting"] == 0
