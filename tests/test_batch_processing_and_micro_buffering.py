"""Test suite and comparative benchmarks for Stage 3: Batch Processing & Micro Buffering."""
import asyncio
import time
from types import SimpleNamespace

from modules.message_delete_queue import MessageDeleteQueue
from modules.admin_actions import AdminActions
from modules.cache_manager import PermissionCircuitBreaker


class FakeDeleteClient:
    def __init__(self, rpc_delay=0.005):
        self.rpc_delay = rpc_delay
        self.delete_calls = []
        self.edit_permission_calls = 0

    async def delete_messages(self, chat_id, ids):
        self.delete_calls.append((chat_id, list(ids)))
        if self.rpc_delay:
            await asyncio.sleep(self.rpc_delay)
        return True

    async def edit_permissions(self, chat, user, **kwargs):
        self.edit_permission_calls += 1
        if self.rpc_delay:
            await asyncio.sleep(self.rpc_delay)
        return True

    async def get_me(self):
        return SimpleNamespace(id=999999)

    async def get_input_entity(self, value):
        return SimpleNamespace(id=value, access_hash=123)

    async def get_entity(self, value):
        return await self.get_input_entity(value)

    async def __call__(self, request):
        if self.rpc_delay:
            await asyncio.sleep(self.rpc_delay)
        return True


class FakeLogger:
    def __init__(self):
        self.logs = []

    def log_info(self, msg):
        self.logs.append(("INFO", msg))

    def log_error(self, msg):
        self.logs.append(("ERROR", msg))

    def log_action(self, action, user_id, chat_id, reason=""):
        self.logs.append(("ACTION", f"{action} user={user_id} chat={chat_id}"))


def test_micro_buffering_batches_rapid_single_deletes():
    async def scenario():
        client = FakeDeleteClient(rpc_delay=0.002)
        logger = FakeLogger()
        queue = MessageDeleteQueue(client, logger, batch_size=15, micro_buffer_seconds=0.05)

        # Enqueue 10 spam messages rapidly (2ms apart)
        futures = []
        for i in range(1, 11):
            fut = queue.enqueue(chat_id=100, message_ids=[i], priority=1)
            futures.append(fut)
            await asyncio.sleep(0.002)

        results = await asyncio.gather(*futures)
        for deleted_count, remaining in results:
            assert deleted_count == 1
            assert remaining == []

        # Verify all 10 messages were sent in EXACTLY ONE DeleteMessagesRequest batch!
        assert len(client.delete_calls) == 1
        assert client.delete_calls[0] == (100, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        print(f"Micro-buffering test passed! 10 spam deletes aggregated into {len(client.delete_calls)} RPC batch.")

    asyncio.run(scenario())


def test_default_delete_buffer_is_shorter_and_keeps_burst_merging():
    async def scenario():
        client = FakeDeleteClient(rpc_delay=0.002)
        queue = MessageDeleteQueue(client, FakeLogger(), batch_size=15)
        assert queue.micro_buffer_seconds == 0.02
        futures = [
            queue.enqueue(chat_id=778, message_ids=[message_id], priority=1)
            for message_id in range(1, 7)
        ]
        await asyncio.gather(*futures)
        return client.delete_calls

    calls = asyncio.run(scenario())
    assert calls == [(778, [1, 2, 3, 4, 5, 6])]


def test_priority_0_admin_deletes_bypass_micro_buffer():
    async def scenario():
        client = FakeDeleteClient(rpc_delay=0.001)
        logger = FakeLogger()
        queue = MessageDeleteQueue(client, logger, batch_size=15, micro_buffer_seconds=0.1)

        t0 = time.perf_counter()
        fut = queue.enqueue(chat_id=200, message_ids=[999], priority=0)
        deleted_count, _ = await fut
        elapsed_ms = (time.perf_counter() - t0) * 1000

        assert deleted_count == 1
        # Priority 0 should execute immediately (around rpc_delay ~1-5ms, NOT waiting 100ms micro-buffer)
        assert elapsed_ms < 50.0
        print(f"Priority 0 bypass test passed! Admin delete completed in {elapsed_ms:.2f}ms.")

    asyncio.run(scenario())


def test_ban_mute_not_delayed_by_delete_micro_buffer():
    async def scenario():
        import modules.banned_storage as bs
        orig_add = bs.add_banned
        bs.add_banned = lambda *a, **k: True
        try:
            client = FakeDeleteClient(rpc_delay=0.002)
            logger = FakeLogger()
            config = {"send_warning": False, "action_on_threshold": "ban"}
            cb = PermissionCircuitBreaker(logger=logger)
            admin = AdminActions(client, logger, config, circuit_breaker=cb)
            queue = MessageDeleteQueue(client, logger, batch_size=15, micro_buffer_seconds=0.08)

            # Start a heavy burst of spam deletes in group 300
            for i in range(1, 15):
                queue.enqueue(chat_id=300, message_ids=[i], priority=1)

            # Concurrently perform Ban on user 777
            t0 = time.perf_counter()
            ban_ok = await admin.ban_user(300, 777, reason="Spam Wave")
            ban_ms = (time.perf_counter() - t0) * 1000

            assert ban_ok is True
            # Ban must be fast and completely unaffected by delete queue micro-buffering
            assert ban_ms < 50.0
            print(f"Ban latency isolation test passed! Ban executed in {ban_ms:.2f}ms during delete wave.")
        finally:
            bs.add_banned = orig_add

    asyncio.run(scenario())


def test_60_groups_concurrent_spam_waves_and_deletes():
    async def scenario():
        client = FakeDeleteClient(rpc_delay=0.001)
        logger = FakeLogger()
        queue = MessageDeleteQueue(client, logger, batch_size=15, micro_buffer_seconds=0.05)

        # 60 groups each sending 8 burst spam messages
        all_futs = []
        for group in range(1, 61):
            for msg_id in range(1, 9):
                all_futs.append(queue.enqueue(chat_id=group, message_ids=[msg_id], priority=1))

        await asyncio.gather(*all_futs)

        # Without buffering, 60 groups * 8 = 480 RPCs
        # With micro-buffering, each group batches its 8 messages into 1 RPC -> exactly 60 RPCs!
        assert len(client.delete_calls) == 60
        print(f"60 Groups concurrency test passed! 480 spam messages cleanly reduced to {len(client.delete_calls)} RPC calls across 60 groups.")

    asyncio.run(scenario())


def run_comparative_delete_benchmark():
    """Benchmark comparing unbuffered vs micro-buffered delete throughput and RPC counts."""
    async def run_scenario(buffer_seconds: float):
        client = FakeDeleteClient(rpc_delay=0.002)
        logger = FakeLogger()
        queue = MessageDeleteQueue(client, logger, batch_size=15, micro_buffer_seconds=buffer_seconds)

        t0 = time.perf_counter()
        futs = []
        # Simulate 10 groups, each receiving 10 burst spam messages over 30ms
        for g in range(1, 11):
            for m in range(1, 11):
                futs.append(queue.enqueue(chat_id=g, message_ids=[m], priority=1))
                await asyncio.sleep(0.001)

        await asyncio.gather(*futs)
        duration_ms = (time.perf_counter() - t0) * 1000
        return duration_ms, len(client.delete_calls)

    async def main():
        time_no_buf, rpc_no_buf = await run_scenario(buffer_seconds=0.0)
        time_with_buf, rpc_with_buf = await run_scenario(buffer_seconds=0.05)

        rpc_reduction = ((rpc_no_buf - rpc_with_buf) / rpc_no_buf) * 100

        print("\n================ COMPARATIVE BENCHMARK (100 Burst Spam Messages across 10 Groups) ================")
        print(f"Without Micro-Buffering : Time = {time_no_buf:.2f} ms | DeleteMessagesRequest RPCs = {rpc_no_buf}")
        print(f"With Micro-Buffering    : Time = {time_with_buf:.2f} ms | DeleteMessagesRequest RPCs = {rpc_with_buf}")
        print(f"RPC Reduction           : {rpc_reduction:.1f}% fewer Delete RPCs sent to Soroush server")
        print("===================================================================================================\n")

    asyncio.run(main())


if __name__ == "__main__":
    test_micro_buffering_batches_rapid_single_deletes()
    test_default_delete_buffer_is_shorter_and_keeps_burst_merging()
    test_priority_0_admin_deletes_bypass_micro_buffer()
    test_ban_mute_not_delayed_by_delete_micro_buffer()
    test_60_groups_concurrent_spam_waves_and_deletes()
    run_comparative_delete_benchmark()
