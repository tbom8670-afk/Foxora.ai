"""Adaptive delete concurrency stays bounded and backs off after FloodWait."""
import asyncio

from modules.rpc_governor import P1_DELETE, RpcAdmission, RpcGovernor


def test_default_auto_delete_cap_and_floodwait_backoff(monkeypatch):
    monkeypatch.setenv("BOT_RPC_TOTAL_LIMIT", "3")
    monkeypatch.delenv("BOT_RPC_DELETE_LIMIT", raising=False)
    monkeypatch.setenv("BOT_RPC_SEND_LIMIT", "1")

    async def scenario():
        governor = RpcGovernor.from_environment(None)
        assert governor.auto_delete is True
        assert governor.total_limit == 3
        assert governor._delete_cap() == 2
        admission = lambda key: RpcAdmission(
            priority=P1_DELETE, bucket="delete",
            request_name="DeleteMessagesRequest", chat_key=key,
        )
        first, second = await asyncio.gather(
            governor.acquire(admission("group-a")),
            governor.acquire(admission("group-b")),
        )
        assert governor.active == 2
        third_task = asyncio.create_task(governor.acquire(admission("group-c")))
        await asyncio.sleep(0)
        assert not third_task.done()

        governor.note_flood_wait("delete", seconds=4)
        assert governor._delete_cap() == 1
        first.release()
        await asyncio.sleep(0)
        assert not third_task.done(), "FloodWait guard must keep one active delete from expanding"
        second.release()
        third = await third_task
        assert governor.active == 1
        third.release()
        assert governor.active == 0

    asyncio.run(scenario())
