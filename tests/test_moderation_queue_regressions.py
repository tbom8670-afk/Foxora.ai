"""Offline regressions for ModerationQueue in both Foxora installations.

No Soroush connection is required: operations, failures and FloodWait are
simulated.  Keep this file identical in Foxora.ai and Foxora2.
"""
import asyncio

import pytest

from modules.moderation_queue import ModerationQueue


class Logger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(message)

    def log_error(self, message):
        self.errors.append(message)


class BrokenLogger:
    def log_info(self, _message):
        raise RuntimeError("broken info logger")

    def log_error(self, _message):
        raise RuntimeError("broken error logger")


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"wait of {seconds} seconds")
        self.seconds = seconds


async def wait_for_count(queue, count):
    async def wait():
        while queue._completed < count:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 1)


def test_missing_logger_does_not_strand_pending_or_skip_callbacks():
    async def scenario():
        q = ModerationQueue(None)
        callback = asyncio.Event()
        calls = []

        async def operation():
            calls.append("operation")
            return True

        async def success(value):
            calls.append(value)
            callback.set()

        assert q.enqueue(77, "mute", operation, user_id=42, on_success=success)
        assert not q.enqueue("77", "mute", operation, user_id="42")
        await asyncio.wait_for(callback.wait(), 1)
        await wait_for_count(q, 1)
        assert calls == ["operation", True]
        assert not q._pending_keys
        assert q.enqueue(77, "mute", operation, user_id="42")
        await wait_for_count(q, 2)
        await q.close()
        assert not q._pending_keys

    asyncio.run(scenario())


def test_broken_logger_does_not_break_worker_retries_or_failure_callback():
    async def scenario():
        q = ModerationQueue(BrokenLogger(), per_chat_limit=1)
        failures = []
        succeeded = []
        failed = asyncio.Event()
        success = asyncio.Event()

        async def fail():
            raise ValueError("operation failed")

        async def notify_failure(error):
            failures.append(error)
            failed.set()

        async def succeed():
            succeeded.append(True)
            return True

        async def notify_success(value):
            assert value is True
            success.set()

        assert q.enqueue(77, "ban", fail, user_id=5, on_failure=notify_failure)
        assert q.enqueue(77, "mute", succeed, user_id=6, on_success=notify_success)
        await asyncio.wait_for(asyncio.gather(failed.wait(), success.wait()), 1)
        await wait_for_count(q, 2)
        assert len(failures) == 1 and isinstance(failures[0], ValueError)
        assert succeeded == [True]
        assert not q._pending_keys
        await q.close()

    asyncio.run(scenario())


def test_numeric_and_string_user_ids_deduplicate_and_release_after_failure():
    async def scenario():
        q = ModerationQueue(Logger(), per_chat_limit=1)
        held = asyncio.Event()
        start = asyncio.Event()
        failed = asyncio.Event()

        async def operation():
            start.set()
            await held.wait()
            raise ValueError("failed RPC")

        async def on_failure(_error):
            failed.set()

        assert q.enqueue(123, "ban", operation, user_id=9, on_failure=on_failure)
        await asyncio.wait_for(start.wait(), 1)
        assert ("123", 9, "ban") in q._pending_keys  # used by incident monitor
        assert not q.enqueue("123", "ban", operation, user_id="009")
        assert len(q._pending_keys) == 1
        held.set()
        await asyncio.wait_for(failed.wait(), 1)
        await wait_for_count(q, 1)
        assert ("123", 9, "ban") not in q._pending_keys
        assert q.enqueue("123", "ban", lambda: asyncio.sleep(0, result=True), user_id="9")
        await wait_for_count(q, 2)
        await q.close()

    asyncio.run(scenario())


def test_synthetic_ids_and_missing_id_remain_distinct():
    async def scenario():
        q = ModerationQueue(Logger())
        gate = asyncio.Event()
        async def held():
            await gate.wait()
            return True

        assert q.enqueue(1, "mute", held, user_id=9)
        assert q.enqueue(1, "mute", held, user_id="reply:9")
        assert q.enqueue(1, "mute", held, user_id=None)
        assert q.enqueue(1, "mute", held, user_id="None")
        assert len(q._pending_keys) == 4
        gate.set()
        await wait_for_count(q, 4)
        await q.close()

    asyncio.run(scenario())


def test_fifo_per_user_even_when_later_job_has_higher_priority():
    async def scenario():
        q = ModerationQueue(Logger(), per_chat_limit=1)
        gate = asyncio.Event()
        started = asyncio.Event()
        order = []

        async def blocker():
            order.append("blocker")
            started.set()
            await gate.wait()
            return True

        def work(label):
            async def run():
                order.append(label)
                return True
            return run

        assert q.enqueue(1, "ban", blocker, user_id=1)
        await asyncio.wait_for(started.wait(), 1)
        assert q.enqueue(1, "ban", work("first user 2"), user_id=2)
        assert q.enqueue(1, "mute", work("second user 2"), user_id="2")
        assert q.enqueue(1, "mute", work("independent manual"), user_id=3)
        gate.set()
        await wait_for_count(q, 4)
        await q.close()
        return order

    assert asyncio.run(scenario()) == [
        "blocker", "independent manual", "first user 2", "second user 2"
    ]


def test_queued_same_user_does_not_occupy_other_users_worker():
    async def scenario():
        q = ModerationQueue(Logger(), per_chat_limit=2)
        first_started = asyncio.Event()
        release = asyncio.Event()
        other_started = asyncio.Event()
        trace = []

        async def first():
            trace.append("first")
            first_started.set()
            await release.wait()
            return True

        def later(index):
            async def operation():
                trace.append(f"later {index}")
                return True
            return operation

        async def other():
            trace.append("other")
            other_started.set()
            return True

        assert q.enqueue(1, "ban", first, user_id=7)
        await asyncio.wait_for(first_started.wait(), 1)
        for i in range(8):
            assert q.enqueue(1, f"action-{i}", later(i), user_id="7")
        assert q.enqueue(1, "mute", other, user_id=8)
        await asyncio.wait_for(other_started.wait(), 1)
        assert trace == ["first", "other"]
        assert sum(not w.done() for w in q._workers.get("1", ())) <= 2
        release.set()
        await wait_for_count(q, 10)
        await q.close()
        assert trace == ["first", "other"] + [f"later {i}" for i in range(8)]

    asyncio.run(scenario())


def test_flood_wait_one_bounded_retry_does_not_block_other_chat():
    async def scenario():
        q = ModerationQueue(Logger(), per_chat_limit=1)
        calls = 0
        success = asyncio.Event()
        other = asyncio.Event()

        async def retry():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FloodWaitError(0.02)
            return "ok"

        async def notify(value):
            assert value == "ok"
            success.set()

        assert q.enqueue(1, "ban", retry, user_id=1, on_success=notify)
        assert q.enqueue(2, "ban", lambda: asyncio.sleep(0, result=other.set()), user_id=2)
        await asyncio.wait_for(other.wait(), 1)
        await asyncio.wait_for(success.wait(), 1)
        assert calls == 2
        await wait_for_count(q, 2)
        assert not q._pending_keys
        await q.close()

    asyncio.run(scenario())


def test_flood_wait_above_cap_fails_without_retry_or_sleep():
    async def scenario():
        q = ModerationQueue(Logger())
        attempts = 0
        failure = asyncio.Event()

        async def too_long():
            nonlocal attempts
            attempts += 1
            raise FloodWaitError(300)

        assert q.enqueue(1, "ban", too_long, user_id=2,
                         on_failure=lambda _error: failure.set())
        await asyncio.wait_for(failure.wait(), 1)
        assert attempts == 1
        await wait_for_count(q, 1)
        assert not q._pending_keys
        await q.close()

    asyncio.run(scenario())


def test_close_cancels_active_operations_callbacks_and_discards_backlog():
    async def scenario():
        q = ModerationQueue(Logger(), per_chat_limit=1)
        first_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        callback_started = asyncio.Event()
        callback_cancelled = asyncio.Event()
        call_count = []

        async def slow():
            first_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                first_cancelled.set()

        async def notice(_value):
            callback_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                callback_cancelled.set()

        async def never():
            call_count.append("unexpected")
            return True

        assert q.enqueue(2, "mute", lambda: asyncio.sleep(0, result=True),
                         user_id=1, on_success=notice)
        await asyncio.wait_for(callback_started.wait(), 1)
        assert q.enqueue(1, "ban", slow, user_id=2)
        await asyncio.wait_for(first_started.wait(), 1)
        assert q.enqueue(1, "mute", never, user_id="2")
        assert q.enqueue(1, "ban", never, user_id=3)
        await asyncio.wait_for(q.close(), 1)
        await q.close()  # idempotent
        assert first_cancelled.is_set()
        assert callback_cancelled.is_set()
        assert not call_count
        assert not q._workers and not q._pending_keys
        assert not q._queues and not q._user_locks
        assert not q._callbacks and not q._user_backlog
        with pytest.raises(RuntimeError, match="closed"):
            q.enqueue(1, "ban", never, user_id=3)

    asyncio.run(scenario())


def test_cancelled_worker_hands_queued_jobs_to_another_worker():
    async def scenario():
        q = ModerationQueue(Logger(), per_chat_limit=1)
        running = asyncio.Event()
        cancelled = asyncio.Event()
        next_started = asyncio.Event()

        async def slow():
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        assert q.enqueue(4, "ban", slow, user_id=4)
        await asyncio.wait_for(running.wait(), 1)
        assert q.enqueue(4, "mute", lambda: asyncio.sleep(0, result=next_started.set()), user_id="4")
        worker = q._workers["4"][0]
        worker.cancel()
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)
        await asyncio.wait_for(next_started.wait(), 1)
        await wait_for_count(q, 2)
        assert cancelled.is_set() and not q._pending_keys
        await q.close()

    asyncio.run(scenario())


def test_invalid_timeout_or_missing_event_loop_never_registers_pending():
    q = ModerationQueue(Logger())
    with pytest.raises(ValueError):
        q.enqueue(4, "ban", lambda: None, user_id=4, timeout_seconds="invalid")
    with pytest.raises(RuntimeError, match="running event loop"):
        q.enqueue(4, "ban", lambda: None, user_id=4)
    assert not q._pending_keys and not q._queues and not q._workers
